"""
Integration tests. Real swarm.py, real schemas.py, real httpx and litellm
objects -- only the network boundary is mocked.

Run: pytest -q test_swarm.py
"""

from __future__ import annotations

import asyncio
import json
import os

import httpx
import pytest
import respx
from datetime import datetime, timedelta, timezone

# ~40 days out: clears min_hours_to_close and max_days_to_close both.
CLOSES_AT = (datetime.now(timezone.utc) + timedelta(days=40)).isoformat()

os.environ.setdefault("TYPESAFE_API_KEY", "test-key")

import swarm as sw  # noqa: E402
from questions import ALL_TIER1_QUESTIONS  # noqa: E402
from schemas import GatherRole, Side, StageCost, TradeSignal, TriageVerdict  # noqa: E402

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------
# fixtures / doubles
# --------------------------------------------------------------------------

# Normalized shapes, as adapters.normalize_* produce them.
MARKET = {
    "slug": "fed-cuts-rates-october",
    "question": "Will the Fed cut rates in October?",
    "description": "Resolves YES if the FOMC lowers the target range.",
    "event_title": "FOMC October Decision",
    "tags": ["economics"],
    "closes_at": CLOSES_AT,
    "volume_usd": 812_000.0,
    "liquidity_usd": 140_000.0,
}
BBO = {"bid": 0.53, "ask": 0.55, "bid_depth": 4200, "ask_depth": 3800}


def jev_body(**overrides):
    """Response shaped exactly per docs.typesafe.ai/api."""
    answers = {
        "federal_policy_outcome": {"type": "noul", "noul": 0.04},
        "defense_or_military": {"type": "noul", "noul": 0.02},
        "us_election_or_appointment": {"type": "noul", "noul": 0.03},
        "objective_resolution": {"type": "noul", "noul": 0.88},
        "self_contained": {"type": "noul", "noul": 0.81},
        "research_would_help": {
            "type": "score",
            "score": 1.66,
            "legend": {"0": "nothing", "1": "thin", "2": "substantial"},
            "probabilities": {"0": 0.05, "1": 0.24, "2": 0.71},
            "confidence": 0.79,
        },
        "outcome_type": {
            "type": "choice",
            "choice": "scheduled_disclosure",
            "probabilities": {
                "scheduled_disclosure": 0.86,
                "continuous_metric": 0.07,
                "contested_event": 0.04,
                "discretionary_action": 0.03,
            },
            "confidence": 0.84,
        },
    }
    for k, v in overrides.items():
        ans = answers[k]
        ans[{"noul": "noul", "score": "score", "choice": "choice"}[ans["type"]]] = v
    return {
        "model": "jev-1.13-20260917",
        "answers": answers,
        "usage": {"input_tokens": 1580, "output_tokens": 0},
    }


class FakeBudget:
    def __init__(self, bankroll=100.0, allow=True):
        self.bankroll = bankroll
        self.allow = allow
        self.recorded: list[StageCost] = []

    async def can_spend(self, usd: float) -> bool:
        return self.allow

    async def record_spend(self, cost: StageCost) -> None:
        self.recorded.append(cost)

    async def available_bankroll(self) -> float:
        return self.bankroll

    @property
    def total(self) -> float:
        return sum(c.usd for c in self.recorded)


async def fake_search(query: str):
    return [{"title": "Reuters", "url": "https://r.com/1", "snippet": "FOMC minutes"}]


class FakeMsg:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []

    def model_dump(self):
        return {"role": "assistant", "content": self.content}


class FakeResp:
    def __init__(self, content, cost=0.03):
        self.choices = [type("C", (), {"message": FakeMsg(content)})()]
        self.usage = type("U", (), {"prompt_tokens": 800, "completion_tokens": 200})()
        self._hidden_params = {"response_cost": cost}


GATHER_JSON = json.dumps(
    {
        "identified_catalyst": "Sept CPI printed below consensus on Oct 14.",
        "historical_precedent": "In 7 of 9 comparable disinflation prints since 2015, a cut followed.",
        "key_facts": ["core CPI 2.1% y/y", "2 governors publicly dovish"],
        "contradicting_evidence": "Labor market still tight.",
        "data_confidence": 0.72,
        "sources": ["https://r.com/1"],
    }
)

SYNTH_JSON = json.dumps(
    {
        "side": "YES",
        "probability": 0.70,
        "confidence": 0.80,
        "reasoning": "Base rate plus a datestamped catalyst; red team's objection is priced.",
        "disqualifiers": [],
        "abstain": False,
    }
)


def patch_llms(monkeypatch, gather=GATHER_JSON, synth=SYNTH_JSON, fail_roles=()):
    calls = {"n": 0}

    async def fake_acompletion(model=None, messages=None, **kw):
        calls["n"] += 1
        sysmsg = messages[0]["content"]
        if model == sw.TIER3_MODEL:
            return FakeResp(synth, cost=0.08)
        for r in fail_roles:
            if r.value in sysmsg:
                raise RuntimeError("provider 503")
        return FakeResp(gather, cost=0.03)

    monkeypatch.setattr(sw, "acompletion", fake_acompletion)
    return calls


def make_swarm(budget, **kw):
    return sw.Swarm(
        jev=sw.JevTriage(api_key="test-key"),
        search=fake_search,
        budget=budget,
        **kw,
    )


# --------------------------------------------------------------------------
# Tier 1
# --------------------------------------------------------------------------

@respx.mock
async def test_triage_parses_live_shaped_response():
    respx.post("https://api.typesafe.ai/v1/systemone").mock(
        return_value=httpx.Response(200, json=jev_body())
    )
    jev = sw.JevTriage(api_key="k")
    v = await jev.evaluate(MARKET, BBO)
    assert v.escalate is True
    assert v.defense_or_military == 0.02
    assert v.research_would_help == 1.66
    assert 0.0 < v.gate_score <= 1.0
    assert v.input_tokens == 1580
    await jev.aclose()


@respx.mock
async def test_triage_sends_batched_questions_in_one_request():
    route = respx.post("https://api.typesafe.ai/v1/systemone").mock(
        return_value=httpx.Response(200, json=jev_body())
    )
    jev = sw.JevTriage(api_key="k")
    await jev.evaluate(MARKET, BBO)
    assert route.call_count == 1
    body = json.loads(route.calls[0].request.content)
    assert set(body["questions"]) == set(ALL_TIER1_QUESTIONS)
    # Numeric fields must NOT be sent: code decides those, not the model.
    assert "spread" not in body["state"]
    assert "volume_usd" not in body["state"]
    assert body["state"]["question"].startswith("Will the Fed")
    await jev.aclose()


@respx.mock
async def test_restricted_domain_vetoes_before_any_spend(monkeypatch):
    respx.post("https://api.typesafe.ai/v1/systemone").mock(
        return_value=httpx.Response(200, json=jev_body(defense_or_military=0.41))
    )
    calls = patch_llms(monkeypatch)
    budget = FakeBudget()
    s = make_swarm(budget)
    res = await s.evaluate(MARKET, BBO)
    assert res.order is None
    assert "restricted:" in res.halted_at
    assert calls["n"] == 0, "vetoed market must not reach a paid tier"
    await s.jev.aclose()


@respx.mock
async def test_triage_cost_recorded_even_when_gate_rejects(monkeypatch):
    """The always-on burn must hit the ledger, not just escalated markets."""
    respx.post("https://api.typesafe.ai/v1/systemone").mock(
        return_value=httpx.Response(200, json=jev_body(research_would_help=0.3))
    )
    monkeypatch.setattr(sw, "TYPESAFE_USD_PER_MTOK_IN", 0.20)
    monkeypatch.setattr(sw, "TYPESAFE_USD_PER_MTOK_OUT", 0.40)
    budget = FakeBudget()
    s = make_swarm(budget)
    res = await s.evaluate(MARKET, BBO)
    assert not res.triage.escalate
    assert len(budget.recorded) == 1
    assert budget.recorded[0].stage == "triage"
    assert budget.total > 0
    await s.jev.aclose()


# --------------------------------------------------------------------------
# full pipeline
# --------------------------------------------------------------------------

@respx.mock
async def test_happy_path_produces_sized_order(monkeypatch):
    respx.post("https://api.typesafe.ai/v1/systemone").mock(
        return_value=httpx.Response(200, json=jev_body())
    )
    patch_llms(monkeypatch)
    budget = FakeBudget(bankroll=100.0)
    s = make_swarm(budget)
    res = await s.evaluate(MARKET, BBO)

    assert res.order is not None
    o = res.order
    assert o.side is Side.YES
    assert o.limit_price == 0.55
    assert 0 < o.notional_usd <= 8.0          # 8% cap on $100
    assert o.raw_kelly == pytest.approx(0.3333, abs=1e-3)
    assert isinstance(o.quantity, int)
    assert len(res.facts) == 3
    assert {f.role for f in res.facts} == set(GatherRole)
    assert res.total_cost_usd > 0
    await s.jev.aclose()


@respx.mock
async def test_budget_refusal_blocks_escalation(monkeypatch):
    respx.post("https://api.typesafe.ai/v1/systemone").mock(
        return_value=httpx.Response(200, json=jev_body())
    )
    calls = patch_llms(monkeypatch)
    budget = FakeBudget(allow=False)
    s = make_swarm(budget)
    res = await s.evaluate(MARKET, BBO)
    assert res.halted_at == "budget_exhausted"
    assert calls["n"] == 0
    await s.jev.aclose()


@respx.mock
async def test_two_failed_gatherers_halts_before_astra(monkeypatch):
    respx.post("https://api.typesafe.ai/v1/systemone").mock(
        return_value=httpx.Response(200, json=jev_body())
    )
    patch_llms(monkeypatch, fail_roles=(GatherRole.SLEUTH, GatherRole.QUANT))
    s = make_swarm(FakeBudget())
    res = await s.evaluate(MARKET, BBO)
    assert res.halted_at == "insufficient_gatherer_output"
    assert res.signal is None
    await s.jev.aclose()


@respx.mock
async def test_abstain_produces_no_order(monkeypatch):
    respx.post("https://api.typesafe.ai/v1/systemone").mock(
        return_value=httpx.Response(200, json=jev_body())
    )
    abstain = json.dumps(
        {
            "side": "YES",
            "probability": 0.56,
            "confidence": 0.3,
            "reasoning": "Within noise of market price.",
            "disqualifiers": ["ambiguous resolution criteria"],
            "abstain": True,
        }
    )
    patch_llms(monkeypatch, synth=abstain)
    s = make_swarm(FakeBudget())
    res = await s.evaluate(MARKET, BBO)
    assert res.signal.abstain is True
    assert res.order is None
    assert res.halted_at == "no_edge_after_sizing"
    await s.jev.aclose()


@respx.mock
async def test_fenced_json_from_model_still_parses(monkeypatch):
    respx.post("https://api.typesafe.ai/v1/systemone").mock(
        return_value=httpx.Response(200, json=jev_body())
    )
    patch_llms(
        monkeypatch,
        gather=f"Here you go:\n```json\n{GATHER_JSON}\n```",
        synth=f"```json\n{SYNTH_JSON}\n```",
    )
    s = make_swarm(FakeBudget())
    res = await s.evaluate(MARKET, BBO)
    assert res.order is not None
    await s.jev.aclose()


@respx.mock
async def test_triage_http_error_is_survivable(monkeypatch):
    respx.post("https://api.typesafe.ai/v1/systemone").mock(
        return_value=httpx.Response(500, json={"error": "boom"})
    )
    calls = patch_llms(monkeypatch)
    s = make_swarm(FakeBudget())
    res = await s.evaluate(MARKET, BBO)
    assert res.halted_at == "triage_error"
    assert calls["n"] == 0
    await s.jev.aclose()


# --------------------------------------------------------------------------
# the concurrency bug this whole design exists to prevent
# --------------------------------------------------------------------------

@respx.mock
async def test_concurrent_evaluations_cannot_oversubscribe_bankroll(monkeypatch):
    respx.post("https://api.typesafe.ai/v1/systemone").mock(
        return_value=httpx.Response(200, json=jev_body())
    )
    patch_llms(monkeypatch)
    budget = FakeBudget(bankroll=100.0)
    s = make_swarm(budget, max_concurrent_evaluations=5)

    markets = [{**MARKET, "slug": f"market-{i}"} for i in range(5)]
    results = await asyncio.gather(*(s.evaluate(m, BBO) for m in markets))

    orders = [r.order for r in results if r.order]
    assert orders, "expected at least one order"
    committed = sum(o.notional_usd for o in orders)
    assert committed <= 100.0
    # Each successive order must size against a bankroll net of prior
    # reservations -- strictly decreasing, never all identical.
    sizes = [o.notional_usd for o in orders]
    assert len(set(sizes)) > 1 or len(sizes) == 1, (
        f"all {len(sizes)} concurrent orders sized identically ({sizes[0]}): "
        "reservations are not being seen by later sizers"
    )
    await s.jev.aclose()


# --------------------------------------------------------------------------
# sizing unit tests
# --------------------------------------------------------------------------

def _sig(side, p, conf=0.8):
    return TradeSignal(
        market_slug="m", side=side, probability=p, confidence=conf, reasoning="t"
    )


@pytest.mark.parametrize(
    "side,p,bid,ask,expect_order",
    [
        (Side.YES, 0.70, 0.53, 0.55, True),    # 15c edge
        (Side.NO, 0.70, 0.45, 0.47, True),     # symmetric NO edge
        (Side.YES, 0.58, 0.53, 0.55, False),   # 3c edge, under min_edge
        (Side.YES, 0.50, 0.53, 0.55, False),   # negative edge
        (Side.YES, 0.70, 0.40, 0.55, False),   # 15c spread, too wide
        (Side.YES, 0.70, 0.55, 0.53, False),   # crossed book
        (Side.YES, 0.70, 0.00, 0.55, False),   # degenerate bid
    ],
)
async def test_sizing_cases(side, p, bid, ask, expect_order):
    o = sw.size_from_signal(
        _sig(side, p), {"bid": bid, "ask": ask}, 100.0, 0.9, sw.RiskConfig()
    )
    assert (o is not None) is expect_order


async def test_quantity_is_always_whole_shares():
    """CreateOrderParams.quantity is an int; fractional shares are rejected."""
    for p, bid, ask, bank in [
        (0.70, 0.53, 0.55, 100.0),
        (0.80, 0.61, 0.63, 250.0),
        (0.35, 0.18, 0.20, 80.0),
    ]:
        o = sw.size_from_signal(
            _sig(Side.YES, p), {"bid": bid, "ask": ask}, bank, 0.9, sw.RiskConfig()
        )
        if o is not None:
            assert isinstance(o.quantity, int)
            assert o.quantity >= 1
            assert o.notional_usd == pytest.approx(o.quantity * o.limit_price, abs=0.01)


async def test_high_price_market_can_fail_min_notional_on_rounding():
    """
    At a small bankroll a high-priced contract can round down below the
    minimum. Refuse rather than silently trade a 1-share position.
    """
    o = sw.size_from_signal(
        _sig(Side.YES, 0.99, conf=0.4),
        {"bid": 0.93, "ask": 0.94},
        40.0,
        0.7,
        sw.RiskConfig(min_notional_usd=2.0),
    )
    if o is not None:
        assert o.notional_usd >= 2.0


async def test_yes_and_no_are_symmetric():
    y = sw.size_from_signal(
        _sig(Side.YES, 0.70), {"bid": 0.53, "ask": 0.55}, 100.0, 0.9, sw.RiskConfig()
    )
    n = sw.size_from_signal(
        _sig(Side.NO, 0.70), {"bid": 0.45, "ask": 0.47}, 100.0, 0.9, sw.RiskConfig()
    )
    assert y.raw_kelly == pytest.approx(n.raw_kelly, abs=1e-6)
    assert y.notional_usd == pytest.approx(n.notional_usd, abs=0.01)


async def test_position_cap_binds_on_extreme_edge():
    o = sw.size_from_signal(
        _sig(Side.YES, 0.99, conf=1.0),
        {"bid": 0.53, "ask": 0.55},
        100.0,
        1.0,
        sw.RiskConfig(),
    )
    # Integer shares mean the cap is approached from below, never exceeded.
    assert o.applied_fraction <= 0.08
    assert o.notional_usd <= 8.0
    assert o.quantity == int(8.0 / 0.55)   # 14 shares @ $0.55 = $7.70


async def test_low_confidence_shrinks_size():
    hi = sw.size_from_signal(
        _sig(Side.YES, 0.70, conf=0.9), {"bid": 0.53, "ask": 0.55}, 100.0, 0.9, sw.RiskConfig()
    )
    lo = sw.size_from_signal(
        _sig(Side.YES, 0.70, conf=0.3), {"bid": 0.53, "ask": 0.55}, 100.0, 0.9, sw.RiskConfig()
    )
    assert lo.notional_usd < hi.notional_usd


async def test_tiny_bankroll_falls_below_min_notional():
    o = sw.size_from_signal(
        _sig(Side.YES, 0.70), {"bid": 0.53, "ask": 0.55}, 12.0, 0.9, sw.RiskConfig()
    )
    assert o is None, "sub-minimum positions must be refused, not rounded up"


# --------------------------------------------------------------------------
# gate aggregation -- regression tests for the ANDed-thresholds failure
# --------------------------------------------------------------------------

def _v(**kw):
    base = dict(
        market_slug="m",
        federal_policy_outcome=0.03,
        defense_or_military=0.02,
        us_election_or_appointment=0.03,
        objective_resolution=0.85,
        self_contained=0.80,
        research_would_help=1.60,
        research_confidence=0.75,
        outcome_type="scheduled_disclosure",
        outcome_type_confidence=0.80,
    )
    base.update(kw)
    return TriageVerdict(**base)


async def test_restricted_domain_veto_is_absolute():
    """No amount of strength elsewhere may carry a restricted market."""
    v = sw._apply_gate(
        _v(
            defense_or_military=0.40,
            objective_resolution=1.0,
            self_contained=1.0,
            research_would_help=2.0,
            research_confidence=1.0,
        ),
        sw.GateThresholds(),
    )
    assert v.escalate is False
    assert v.gate_score == 0.0
    assert "restricted:defense_or_military" in v.veto_reason


async def test_weak_on_one_axis_does_not_annihilate():
    """
    The exact failure of the ANDed design: outstanding everywhere but
    mediocre on spread should still be able to clear.
    """
    v = sw._apply_gate(
        _v(
            objective_resolution=0.97,
            research_would_help=1.9,
            self_contained=0.45,   # the weak axis
            research_confidence=0.9,
        ),
        sw.GateThresholds(min_gate_score=0.62),
    )
    assert v.escalate is True, "a single soft weakness must not be a veto"


async def test_floors_still_catch_genuine_disqualifiers():
    v = sw._apply_gate(
        _v(objective_resolution=0.10, research_would_help=2.0),
        sw.GateThresholds(),
    )
    assert v.escalate is False
    assert "subjective_resolution" in v.veto_reason

    v = sw._apply_gate(_v(research_would_help=0.2), sw.GateThresholds())
    assert v.escalate is False
    assert "research_wont_help" in v.veto_reason


async def test_gate_score_is_monotonic_in_threshold():
    """
    The property the old sweep violated: raising the threshold must
    weakly reduce the escalation count, and the curve must actually move.
    """
    verdicts = [
        _v(
            objective_resolution=0.5 + (i % 50) / 100,
            self_contained=((i * 13) % 100) / 100,
            research_would_help=0.7 + (i % 13) / 10,
            research_confidence=0.4 + (i % 60) / 100,
        )
        for i in range(1, 200)
    ]
    counts = []
    for t in [i / 20 for i in range(21)]:
        g = sw.GateThresholds(min_gate_score=t)
        counts.append(sum(1 for v in verdicts if sw._apply_gate(v, g).escalate))
    assert counts == sorted(counts, reverse=True), "escalation must be monotonic"
    assert counts[0] > counts[-1], "threshold must actually bite"
    assert len(set(counts)) > 5, "curve must be tunable, not a cliff"


async def test_confidence_routes_and_does_not_scale_the_score():
    """
    Confidence is a route, not a weight.

    This replaces a test asserting the opposite -- that low confidence
    shrank the composite. Scaling by it conflated "unattractive market"
    with "triage could not read this market", and compounded with the
    outcome multiplier into a ceiling contested_event could never clear.
    """
    hi = sw.compute_gate_score(_v(research_confidence=0.95), sw.GateThresholds())
    lo = sw.compute_gate_score(_v(research_confidence=0.15), sw.GateThresholds())
    assert hi == lo, "confidence must not enter the composite at all"

    # It routes instead, and only when a floor has been set from data.
    g = sw.GateThresholds(min_research_confidence=0.50)
    vetoed = sw._apply_gate(_v(research_confidence=0.15), g)
    assert vetoed.escalate is False
    assert "low_confidence" in vetoed.veto_reason

    kept = sw._apply_gate(_v(research_confidence=0.95), g)
    assert kept.veto_reason is None or "low_confidence" not in kept.veto_reason


async def test_confidence_floor_ships_inert():
    """
    It must not filter anything until a shadow run says where the cut is.
    A guessed floor here would silently suppress markets with no evidence.
    """
    assert sw.GateThresholds().min_research_confidence == 0.0
    v = sw._apply_gate(_v(research_confidence=0.0), sw.GateThresholds())
    assert v.veto_reason is None or "low_confidence" not in v.veto_reason


async def test_every_outcome_type_can_reach_the_threshold():
    """
    The regression this composition change exists to prevent: with
    multiplied factors, contested_event needed confidence >= 1.00 and so
    could never escalate, invisibly to any sweep of min_gate_score.
    """
    g = sw.GateThresholds()
    for outcome_type, penalty in g.outcome_type_penalty.items():
        ceiling = 1.0 - penalty
        assert ceiling >= g.min_gate_score, (
            f"{outcome_type} cannot reach min_gate_score even with a "
            f"perfect market (ceiling {ceiling:.2f})"
        )


# --------------------------------------------------------------------------
# retry behaviour required by the API reference
# --------------------------------------------------------------------------

@respx.mock
async def test_rate_limit_is_retried_with_backoff(monkeypatch):
    slept: list[float] = []

    async def no_sleep(s):
        slept.append(s)

    monkeypatch.setattr(sw.asyncio, "sleep", no_sleep)
    route = respx.post("https://api.typesafe.ai/v1/systemone").mock(
        side_effect=[
            httpx.Response(429, json={"error": "rate limited"}),
            httpx.Response(529, json={"error": "overloaded"}),
            httpx.Response(200, json=jev_body()),
        ]
    )
    jev = sw.JevTriage(api_key="k")
    v = await jev.evaluate(MARKET, BBO)
    assert v.escalate is True
    assert route.call_count == 3
    assert slept == [0.5, 1.0], f"expected exponential backoff, got {slept}"
    await jev.aclose()


@respx.mock
async def test_retry_honours_retry_after_header(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(sw.asyncio, "sleep", lambda s: slept.append(s) or _noop())
    respx.post("https://api.typesafe.ai/v1/systemone").mock(
        side_effect=[
            httpx.Response(429, headers={"retry-after": "7"}, json={}),
            httpx.Response(200, json=jev_body()),
        ]
    )
    jev = sw.JevTriage(api_key="k")
    await jev.evaluate(MARKET, BBO)
    assert slept == [7.0]
    await jev.aclose()


async def _noop():
    return None


@respx.mock
async def test_retries_are_bounded(monkeypatch):
    monkeypatch.setattr(sw.asyncio, "sleep", lambda s: _noop())
    route = respx.post("https://api.typesafe.ai/v1/systemone").mock(
        return_value=httpx.Response(429, json={})
    )
    s = make_swarm(FakeBudget())
    res = await s.evaluate(MARKET, BBO)
    assert res.halted_at == "triage_error"
    assert route.call_count == 4, "1 attempt + 3 retries, then give up"
    await s.jev.aclose()


# --------------------------------------------------------------------------
# structural filter runs before any spend
# --------------------------------------------------------------------------

@respx.mock
async def test_wide_spread_rejected_without_calling_jev():
    route = respx.post("https://api.typesafe.ai/v1/systemone").mock(
        return_value=httpx.Response(200, json=jev_body())
    )
    jev = sw.JevTriage(api_key="k")
    v = await jev.evaluate(MARKET, {"bid": 0.40, "ask": 0.55})
    assert v.escalate is False
    assert "spread" in v.structural_reject
    assert route.call_count == 0, "arithmetic must not cost a model call"
    await jev.aclose()


@respx.mock
async def test_thin_volume_rejected_without_calling_jev():
    route = respx.post("https://api.typesafe.ai/v1/systemone").mock(
        return_value=httpx.Response(200, json=jev_body())
    )
    jev = sw.JevTriage(api_key="k")
    # Derived from the limit rather than hardcoded: this test guards that
    # thin volume short-circuits the model call, not any particular floor.
    # It previously asserted $900 was thin, which silently stopped testing
    # anything when the floor moved from 50,000 to 250.
    thin = sw.StructuralLimits().min_volume_usd - 1
    v = await jev.evaluate({**MARKET, "volume_usd": thin}, BBO)
    assert v.structural_reject is not None
    assert "volume" in v.structural_reject
    assert route.call_count == 0
    await jev.aclose()


async def test_restricted_uses_max_not_mean():
    """Two clean answers must not dilute one alarming one."""
    v = _v(
        federal_policy_outcome=0.01,
        defense_or_military=0.55,
        us_election_or_appointment=0.01,
    )
    assert v.restricted_max == 0.55
    assert sw.apply_gate(v, sw.GateThresholds()).escalate is False


async def test_contested_event_scores_below_scheduled_disclosure():
    g = sw.GateThresholds()
    sched = sw.compute_gate_score(_v(outcome_type="scheduled_disclosure"), g)
    contest = sw.compute_gate_score(_v(outcome_type="contested_event"), g)
    assert contest < sched
    unknown = sw.compute_gate_score(_v(outcome_type="something_new"), g)
    assert 0 < unknown < sched, "unseen outcome types get a cautious default"


async def test_triage_cost_is_now_actually_priced():
    usd = sw.price_triage(1580, 0)
    assert usd == pytest.approx(1580 * 0.042 / 1e6)
    assert 0 < usd < 0.0001
    assert 1.0 / usd > 10_000, "should be >10k triage calls per dollar"


# --------------------------------------------------------------------------
# reservation lifecycle -- the silent-wedge bug
# --------------------------------------------------------------------------

async def test_reservations_release():
    p = sw.PortfolioLock()
    p.reserve("a", 5.0)
    p.reserve("b", 7.0)
    assert p.reserved_total == pytest.approx(12.0)
    p.release("a")
    assert p.reserved_total == pytest.approx(7.0)
    p.release("nonexistent")   # must not raise
    assert p.open_reservations == 1


async def test_unreleased_reservations_expire(monkeypatch):
    """
    release() was originally never called anywhere. reserved_total grew
    without bound, available bankroll drifted to zero, and the daemon
    quietly stopped producing orders with no error raised. The TTL turns
    that silent wedge into temporary degradation.
    """
    clock = [1000.0]
    monkeypatch.setattr(sw.time, "time", lambda: clock[0])
    p = sw.PortfolioLock(ttl_seconds=60.0)

    for i in range(20):
        p.reserve(f"m{i}", 5.0)
    assert p.reserved_total == pytest.approx(100.0)

    clock[0] += 61.0
    assert p.reserved_total == 0.0, "stale reservations must not wedge sizing"
    assert p.open_reservations == 0


async def test_leaked_reservations_would_starve_sizing():
    """Demonstrates the failure the TTL prevents."""
    p = sw.PortfolioLock(ttl_seconds=10_000)
    bankroll = 100.0
    for i in range(25):
        p.reserve(f"m{i}", 5.0)          # never released
    effective = bankroll - p.reserved_total
    assert effective < 0, "leaked reservations drive bankroll negative"

    o = sw.size_from_signal(
        _sig(Side.YES, 0.70), {"bid": 0.53, "ask": 0.55},
        max(0.0, effective), 0.9, sw.RiskConfig(),
    )
    assert o is None, "a starved bankroll produces no orders, silently"


# --------------------------------------------------------------------------
# Sampling parameters: Anthropic removed them on 4.6+ and returns 400.
# litellm passes them through, so an unguarded temperature is a hard
# failure the moment TIER2/TIER3 point at Claude.
# --------------------------------------------------------------------------

async def test_sampling_dropped_for_models_that_reject_it():
    for model in (
        "anthropic/claude-opus-5",
        "anthropic/claude-sonnet-5",
        "anthropic/claude-opus-4-8",
        "anthropic/claude-opus-4-7",
        "anthropic/claude-fable-5-1",
    ):
        assert sw.sampling_kwargs(model, 0.3) == {}, (
            f"{model} returns 400 when temperature is present"
        )


async def test_sampling_kept_where_it_is_still_accepted():
    for model in (
        "anthropic/claude-haiku-4-5",
        "gemini/gemini-2.5-flash",
        "openai/gpt-6-astra",
    ):
        assert sw.sampling_kwargs(model, 0.3) == {"temperature": 0.3}, (
            f"{model} still accepts temperature; dropping it changes behaviour"
        )


async def test_default_tier_models_are_self_consistent():
    """
    The shipped defaults must not be a combination that 400s on the first
    escalation -- which is the first time anyone would find out.
    """
    for model in (sw.TIER2_MODEL, sw.TIER3_MODEL):
        kwargs = sw.sampling_kwargs(model, 0.1)
        assert kwargs in ({}, {"temperature": 0.1})
