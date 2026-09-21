"""
Tests for the trace store: what the paid tiers believed, kept for scoring.
"""

from __future__ import annotations

import json
import os

import pytest

os.environ.setdefault("TYPESAFE_API_KEY", "test-key")

import traces  # noqa: E402
from schemas import (  # noqa: E402
    GatherRole,
    MarketFactSummary,
    PipelineResult,
    Side,
    SizedOrder,
    StageCost,
    TradeSignal,
    TriageVerdict,
)

MARKET = {"question": "Over 35.5 total points", "outcome": "Over",
          "event_title": "NYG vs LAR", "description": "Resolves YES if 36+.",
          "tags": ["nfl", "sports"]}
BBO = {"bid": 0.81, "ask": 0.82}


def _result(with_order=True):
    sig = TradeSignal(market_slug="m1", side=Side.YES, probability=0.93,
                      confidence=0.8, reasoning="pace and weather",
                      disqualifiers=[], abstain=False)
    order = SizedOrder(market_slug="m1", side=Side.YES, limit_price=0.82,
                       quantity=3, notional_usd=2.46, raw_kelly=0.6,
                       applied_fraction=0.02, edge=0.11,
                       bankroll_at_size=100.0, signal=sig) if with_order else None
    fact = MarketFactSummary(role=GatherRole.SLEUTH,
                             identified_catalyst="clear weather, both QBs active",
                             historical_precedent="totals this low hit 80%",
                             key_facts=["dome"], data_confidence=0.7,
                             sources=["https://a.example", "https://b.example"])
    return PipelineResult(
        market_slug="m1",
        triage=TriageVerdict(market_slug="m1", escalate=True, gate_score=0.9),
        facts=[fact], signal=sig, order=order,
        costs=[StageCost(stage="gather", model="h", usd=0.02),
               StageCost(stage="synthesize", model="o", usd=0.05)],
    )


def test_record_keeps_what_the_paid_tiers_believed(tmp_path):
    """
    Before this store, a paper or live cycle remembered only its cost.
    Facts, Tier 3's probability and the sized order were all discarded.
    """
    conn = traces.connect(str(tmp_path / "t.db"))
    traces.record(conn, "paper", _result(), MARKET, BBO)
    r = conn.execute("SELECT * FROM evaluations").fetchone()
    conn.close()
    assert r["mode"] == "paper" and r["market_slug"] == "m1"
    assert r["signal_side"] == "YES" and r["signal_prob"] == pytest.approx(0.93)
    assert r["order_qty"] == 3 and r["order_edge"] == pytest.approx(0.11)
    assert r["cost_usd"] == pytest.approx(0.07)
    assert r["n_sources"] == 2
    assert json.loads(r["facts_json"])[0]["role"] == "sleuth"
    assert r["description"].startswith("Resolves YES"), "kept for replay"
    assert r["resolved_outcome"] is None, "filled by a backfill, never here"


def test_record_handles_an_unsized_evaluation(tmp_path):
    conn = traces.connect(str(tmp_path / "t.db"))
    traces.record(conn, "paper", _result(with_order=False), MARKET, BBO)
    r = conn.execute("SELECT * FROM evaluations").fetchone()
    conn.close()
    assert r["signal_prob"] is not None and r["order_qty"] is None


def test_record_never_raises_into_the_trading_loop(tmp_path):
    """A trace that cannot be written must not stop an evaluation."""
    conn = traces.connect(str(tmp_path / "t.db"))
    conn.close()                                   # closed: every write fails
    traces.record(conn, "paper", _result(), MARKET, BBO)   # must not raise


def test_score_compares_tier3_to_the_price_it_was_shown(tmp_path, capsys):
    path = str(tmp_path / "t.db")
    conn = traces.connect(path)
    traces.record(conn, "paper", _result(), MARKET, BBO)
    conn.execute("UPDATE evaluations SET resolved_outcome='1'")
    conn.commit(); conn.close()
    traces.score(path)
    out = capsys.readouterr().out
    assert "1 resolved evaluations" in out
    # tier3 said 0.93, market mid 0.815, outcome YES -> tier3 closer
    assert "tier3 better" in out


def test_open_event_exposure_sums_unresolved_orders_by_event(tmp_path):
    """Seeds the per-event cap so it survives a restart."""
    conn = traces.connect(str(tmp_path / "t.db"))
    game = {**MARKET, "event_slug": "nfl-nyg-lar"}
    traces.record(conn, "paper", _result(), game, BBO)          # $2.46
    traces.record(conn, "paper", _result(), game, BBO)          # $2.46, same event
    traces.record(conn, "live", _result(), game, BBO)           # other mode
    traces.record(conn, "paper", _result(with_order=False), game, BBO)
    conn.execute("UPDATE evaluations SET resolved_outcome='1' WHERE id=2")
    conn.commit()
    assert traces.open_event_exposure(conn, "paper") == {"nfl-nyg-lar": pytest.approx(2.46)}
    conn.close()


def test_connect_migrates_a_database_made_before_event_slug(tmp_path):
    import sqlite3
    path = str(tmp_path / "old.db")
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE evaluations (id INTEGER PRIMARY KEY, at TEXT NOT NULL,"
              " mode TEXT NOT NULL, market_slug TEXT NOT NULL, order_notional REAL,"
              " resolved_outcome TEXT)")
    c.commit(); c.close()
    conn = traces.connect(path)
    assert "event_slug" in {r["name"] for r in conn.execute("PRAGMA table_info(evaluations)")}
    conn.close()
