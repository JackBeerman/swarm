"""
tools/bench_roles.py: the dry run spends nothing, the cap holds, and a
replay goes through the real swarm code path. No network, no keys.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("TYPESAFE_API_KEY", "test-key")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import bench_roles as br  # noqa: E402
import swarm as sw  # noqa: E402
import traces  # noqa: E402
from schemas import MarketFactSummary, PipelineResult, TradeSignal  # noqa: E402

pytestmark = pytest.mark.asyncio

FACT = {"role": "sleuth", "identified_catalyst": "QB ruled out", "historical_precedent": "p",
        "key_facts": ["k1", "k2"], "contradicting_evidence": None,
        "data_confidence": 0.6, "sources": ["https://a", "https://b"]}


@pytest.fixture(autouse=True)
def _no_routing_env(monkeypatch):
    for k in list(os.environ):
        if k.startswith("SWARM_ROUTE") or k == "SWARM_ROUTING_FILE":
            monkeypatch.delenv(k, raising=False)


def _traces_db(tmp_path: Path) -> str:
    path = str(tmp_path / "traces.db")
    conn = traces.connect(path)
    facts = [MarketFactSummary.model_validate(FACT),
             MarketFactSummary.model_validate({**FACT, "role": "quant"})]
    result = PipelineResult(
        market_slug="m-1", facts=facts,
        signal=TradeSignal(market_slug="m-1", side="NO", probability=0.8,
                           confidence=0.5, reasoning="r"))
    traces.record(conn, "paper", result,
                  {"question": "Q?", "outcome": "Yes", "tags": ["nfl"]},
                  {"bid": 0.30, "ask": 0.32})
    conn.execute("UPDATE evaluations SET resolved_outcome='0'")
    conn.commit()
    conn.close()
    return path


def _args(tmp_path, **kw):
    base = dict(roles="synthesis,gatherer,brief_writer,lesson_drafter,question_drafter",
                models=None, limit=2, max_usd=0.25, traces=_traces_db(tmp_path),
                fastlane=str(tmp_path / "none.db"), env=".env", out=None, dry_run=True)
    base.update(kw)
    return argparse.Namespace(**base)


async def test_dry_run_makes_no_calls(tmp_path, monkeypatch, capsys):
    import litellm

    async def boom(*a, **k):
        raise AssertionError("dry run called a model")

    monkeypatch.setattr(litellm, "acompletion", boom)
    monkeypatch.setattr(sw, "acompletion", boom)
    assert await br.main_async(_args(tmp_path)) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "stored Tier 3 Brier" in out


async def test_budget_refuses_unpriced_and_over_cap():
    b = br.Budget(0.001)
    assert not b.allow("somewhere/unknown-model", 1000, 100)
    assert not b.allow("anthropic/claude-opus-5-5", 30_000, 2500)   # ~$0.06 > cap
    assert br.Budget(1.0).allow("anthropic/claude-opus-5-5", 30_000, 2500)


async def test_frozen_corpus_is_query_independent():
    corpus = br.frozen_corpus([FACT])
    assert len(corpus) == 4
    assert all(c["url"] in FACT["sources"] for c in corpus)


async def test_synthesis_replay_uses_swarm_and_scores_brier(tmp_path, monkeypatch):
    seen = []

    async def fake(model=None, messages=None, **kw):
        seen.append(model)
        content = json.dumps({"side": "YES", "probability": 0.25, "confidence": 0.5,
                              "reasoning": "r", "disqualifiers": [], "abstain": False})
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=10),
            _hidden_params={"response_cost": 0.002})

    monkeypatch.setattr(sw, "acompletion", fake)
    item = [t for t in br.load_traces(_traces_db(tmp_path))][0]
    res = await br.run_synthesis("anthropic/claude-sonnet-5", item, br.Budget(1.0))
    assert seen == ["anthropic/claude-sonnet-5"]
    assert res.valid and res.cost_usd == 0.002
    assert res.extra["p_yes"] == 0.25 and res.extra["resolved"] == "0"
    # The route override is scoped to the call.
    assert "SWARM_ROUTE_SYNTHESIS" not in os.environ


async def test_drafter_schemas_carry_no_size():
    for schema in (br.LessonDraft, br.QuestionDraft):
        names = set(schema.model_fields)
        assert not names & {"size", "quantity", "notional", "kelly", "usd", "dollars"}
