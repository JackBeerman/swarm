"""
models.py: the role -> model registry, and the hook swarm.py calls.

The load-bearing claim is that the hook changes nothing by default: with no
routing configuration, every Tier 2/3 request carries the same model and
the same keyword arguments as before the registry existed. No network, no
keys; litellm is monkeypatched as in test_swarm.py.
"""

from __future__ import annotations

import json
import os

import pytest

os.environ.setdefault("TYPESAFE_API_KEY", "test-key")

import models  # noqa: E402
import swarm as sw  # noqa: E402
from schemas import GatherRole, MarketFactSummary  # noqa: E402

pytestmark = pytest.mark.asyncio

_ENV = ("SWARM_ROUTING_PROFILE", "SWARM_ROUTING_FILE",
        *(f"SWARM_ROUTE_{r.upper()}" for r in models.ROLES))

STATE = {"question": "Will it happen?", "outcome": "Yes", "slug": "m-1",
         "best_bid": 0.40, "best_ask": 0.42}

GATHER = json.dumps({
    "identified_catalyst": "c", "historical_precedent": "h", "key_facts": ["f"],
    "contradicting_evidence": None, "data_confidence": 0.5, "sources": ["s"]})
SYNTH = json.dumps({"side": "YES", "probability": 0.6, "confidence": 0.5,
                    "reasoning": "r", "disqualifiers": [], "abstain": False})


@pytest.fixture(autouse=True)
def _no_routing_env(monkeypatch):
    for k in _ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("TIER2_MODEL", raising=False)
    monkeypatch.delenv("TIER3_MODEL", raising=False)


class _Msg:
    def __init__(self, content):
        self.content = content
        self.tool_calls = []


class _Resp:
    def __init__(self, content, cost=0.01, tok_in=1000, tok_out=100):
        self.choices = [type("C", (), {"message": _Msg(content)})()]
        self.usage = type("U", (), {"prompt_tokens": tok_in,
                                    "completion_tokens": tok_out})()
        self._hidden_params = {"response_cost": cost}


def _capture(monkeypatch, content, cost=0.01):
    calls: list[dict] = []

    async def fake(model=None, messages=None, **kw):
        calls.append({"model": model, **kw})
        return _Resp(content, cost=cost)

    monkeypatch.setattr(sw, "acompletion", fake)
    return calls


# --------------------------------------------------------------------------
# Defaults are today's behaviour
# --------------------------------------------------------------------------

async def test_unconfigured_resolve_returns_the_callers_default():
    assert not models.is_configured()
    for role in models.ROLES:
        assert models.resolve_model(role, "x/sentinel") == "x/sentinel"


async def test_default_profile_matches_swarm_constants():
    r = models.routes()
    for role in ("gatherer_sleuth", "gatherer_historian", "gatherer_red_team"):
        assert r[role].primary == sw.TIER2_MODEL == "anthropic/claude-haiku-4-5"
        assert r[role].fallbacks == ()
    assert r["synthesis"].primary == sw.TIER3_MODEL == "anthropic/claude-opus-5-5"
    assert r["synthesis"].fallbacks == ()


async def test_gatherer_request_unchanged_by_default(monkeypatch):
    calls = _capture(monkeypatch, GATHER)
    costs: list = []
    out = await sw._run_gatherer(GatherRole.SLEUTH, STATE, None, costs)
    assert isinstance(out, MarketFactSummary)
    assert len(calls) == 1
    # Exactly the keys the pre-registry code sent, nothing added.
    assert set(calls[0]) == {"model", "tools", "temperature", "max_tokens"}
    assert calls[0]["model"] == sw.TIER2_MODEL
    assert costs[0].model == sw.TIER2_MODEL and costs[0].usd == 0.01


async def test_synthesis_request_unchanged_by_default(monkeypatch):
    calls = _capture(monkeypatch, SYNTH)
    costs: list = []
    fact = MarketFactSummary.model_validate({**json.loads(GATHER), "role": "sleuth"})
    sig = await sw._synthesize(STATE, [fact, fact], costs)
    assert sig is not None
    # Opus 5.5 rejects temperature; nothing else was ever sent.
    assert set(calls[0]) == {"model", "max_tokens"}
    assert calls[0]["model"] == sw.TIER3_MODEL


# --------------------------------------------------------------------------
# Overrides
# --------------------------------------------------------------------------

async def test_env_override_routes_one_role_and_passes_provider_routing(monkeypatch):
    monkeypatch.setenv("SWARM_ROUTE_SYNTHESIS",
                       "openrouter/z-ai/glm-5.3, anthropic/claude-opus-5-5")
    calls = _capture(monkeypatch, SYNTH)
    costs: list = []
    fact = MarketFactSummary.model_validate({**json.loads(GATHER), "role": "quant"})
    await sw._synthesize(STATE, [fact, fact], costs)
    assert calls[0]["model"] == "openrouter/z-ai/glm-5.3"
    assert calls[0]["temperature"] == 0.1
    assert "extra_body" in calls[0]          # fp8+ pin for OpenRouter
    assert costs[0].model == "openrouter/z-ai/glm-5.3"
    assert models.route("synthesis").fallbacks == ("anthropic/claude-opus-5-5",)
    # Only the overridden role moved.
    assert models.resolve_model("gatherer_sleuth", sw.TIER2_MODEL) == sw.TIER2_MODEL


async def test_per_gatherer_role_routing(monkeypatch):
    monkeypatch.setenv("SWARM_ROUTE_GATHERER_RED_TEAM", "openrouter/openai/gpt-oss-120b")
    calls = _capture(monkeypatch, GATHER)
    for role in GatherRole:
        await sw._run_gatherer(role, STATE, None, [])
    assert [c["model"] for c in calls] == [
        sw.TIER2_MODEL, sw.TIER2_MODEL, "openrouter/openai/gpt-oss-120b"]


async def test_bad_override_spends_nothing(monkeypatch):
    monkeypatch.setenv("SWARM_ROUTING_PROFILE", "no-such-profile")
    calls = _capture(monkeypatch, SYNTH)
    fact = MarketFactSummary.model_validate({**json.loads(GATHER), "role": "quant"})
    assert await sw._synthesize(STATE, [fact, fact], []) is None
    assert await sw._run_gatherer(GatherRole.QUANT, STATE, None, []) is None
    assert calls == []


async def test_routing_file(monkeypatch, tmp_path):
    f = tmp_path / "routing.toml"
    f.write_text('[roles.lesson_drafter]\nprimary = "anthropic/claude-sonnet-5"\n'
                 'fallbacks = ["anthropic/claude-haiku-4-5"]\n')
    monkeypatch.setenv("SWARM_ROUTING_FILE", str(f))
    r = models.route("lesson_drafter")
    assert r.chain == ("anthropic/claude-sonnet-5", "anthropic/claude-haiku-4-5")
    f.write_text('[roles.not_a_role]\nprimary = "x"\n')
    with pytest.raises(ValueError):
        models.routes()


async def test_example_routing_file_parses_and_is_priced(monkeypatch):
    from pathlib import Path

    f = Path(__file__).resolve().parent.parent / "routing.example.toml"
    monkeypatch.setenv("SWARM_ROUTING_FILE", str(f))
    table = models.routes()
    assert table["synthesis"].primary == "anthropic/claude-opus-5-5"
    for route in table.values():
        for m in route.chain:
            assert models.spec(m) is not None, m


async def test_recommended_profile_keeps_synthesis_on_opus(monkeypatch):
    """No evidence yet that anything cheaper is as calibrated."""
    monkeypatch.setenv("SWARM_ROUTING_PROFILE", "recommended")
    assert models.route("synthesis").primary == "anthropic/claude-opus-5-5"


# --------------------------------------------------------------------------
# Registry consistency
# --------------------------------------------------------------------------

async def test_every_profile_covers_every_role_with_listed_models(monkeypatch):
    for profile in ("default", "recommended"):
        monkeypatch.setenv("SWARM_ROUTING_PROFILE", profile)
        table = models.routes()
        assert set(table) == set(models.ROLES)
        for route in table.values():
            for m in route.chain:
                assert models.spec(m) is not None, f"{profile}: {m} not in registry"


async def test_registry_temperature_agrees_with_swarm_on_claude():
    """A disagreement here is a 400 on the first escalation."""
    for m, s in models.MODELS.items():
        if "claude" in m:
            expect = {"temperature": 0.3} if s.accepts_temperature else {}
            assert sw.sampling_kwargs(m, 0.3) == expect, m


async def test_gatherer_models_can_call_tools():
    for m, s in models.MODELS.items():
        if s.provider != "ollama":
            assert s.tool_calling, m


# --------------------------------------------------------------------------
# Cost: an unpriced response must not reach the ledger as $0.00
# --------------------------------------------------------------------------

async def test_cost_falls_back_to_registry_when_litellm_returns_zero():
    resp = _Resp("{}", cost=0.0, tok_in=10_000, tok_out=1_000)
    c = sw._cost_of(resp, "gather", "openrouter/z-ai/glm-5.3-flash")
    assert c.usd == pytest.approx((10_000 * 0.15 + 1_000 * 0.50) / 1e6)


async def test_cost_from_litellm_is_left_alone():
    resp = _Resp("{}", cost=0.0123)
    assert sw._cost_of(resp, "gather", "openrouter/z-ai/glm-5.3-flash").usd == 0.0123


async def test_unlisted_model_still_prices_zero():
    resp = _Resp("{}", cost=0.0)
    assert sw._cost_of(resp, "gather", "somewhere/unknown").usd == 0.0


# --------------------------------------------------------------------------
# Fallback chains (bench only; not wired into swarm)
# --------------------------------------------------------------------------

async def test_complete_walks_the_chain(monkeypatch):
    import litellm

    seen: list[str] = []

    async def fake(model=None, messages=None, **kw):
        seen.append(model)
        if model == "anthropic/claude-opus-5-5":
            raise RuntimeError("529 overloaded")
        return _Resp(SYNTH)

    monkeypatch.setattr(litellm, "acompletion", fake)
    monkeypatch.setenv("SWARM_ROUTE_SYNTHESIS",
                       "anthropic/claude-opus-5-5,anthropic/claude-sonnet-5")
    _, used = await models.complete("synthesis", [{"role": "user", "content": "x"}])
    assert used == "anthropic/claude-sonnet-5"
    assert seen == ["anthropic/claude-opus-5-5", "anthropic/claude-sonnet-5"]


async def test_missing_keys_names_only(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert models.missing_keys(["openrouter/openai/gpt-oss-120b"]) == ["OPENROUTER_API_KEY"]
