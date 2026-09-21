"""
Tests for the shadow collector's bookkeeping -- the parts that decide
whether tomorrow's analysis is reading what today's run actually did.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("TYPESAFE_API_KEY", "test-key")

import shadow  # noqa: E402
from questions import GateThresholds, StructuralLimits  # noqa: E402
from schemas import TriageVerdict  # noqa: E402


@pytest.fixture
def db(tmp_path):
    conn = shadow.connect(str(tmp_path / "t.db"))
    yield conn
    conn.close()


def _sports_verdict(slug="nfl-x", escalate=True, structural=None):
    return TriageVerdict(
        market_slug=slug, is_sports=True, objective_resolution=0.95,
        stat_aggregation=1.8, pregame_information_edge=1.5,
        sports_market_type="team_aggregate_stat", escalate=escalate,
        gate_score=0.9 if escalate else 0.0, model="jev-1.13.0",
        structural_reject=structural,
    )


MARKET = {
    "question": "Team Total First Downs: Over 21.5", "outcome": "Over",
    "event_title": "PHI vs TEN", "period": "NS",
    "event_at": (datetime.now(timezone.utc) + timedelta(hours=4)).isoformat(),
}
BBO = {
    "bid": 0.48, "ask": 0.50, "bid_shares": 5000.0, "ask_shares": 6000.0,
    "shares_traded": 20000.0, "open_interest": 30000.0,
}


def test_prescreen_rejects_only_clear_extremes():
    """
    Correct under either reading of outcomePrices ([bid, ask] or [yes, no]):
    reject only when BOTH prices sit outside the band on the same side.
    """
    lim = StructuralLimits()
    f = shadow._price_band_reject
    assert f({"outcomePrices": '["0.9850","0.9900"]'}, lim) is True
    assert f({"outcomePrices": '["0.0200","0.0300"]'}, lim) is True
    assert f({"outcomePrices": '["0.4800","0.5000"]'}, lim) is False
    assert f({"outcomePrices": '["0.0300","0.0600"]'}, lim) is False, \
        "ambiguous -> quote it"
    assert f({"outcomePrices": "garbage"}, lim) is False
    assert f({}, lim) is False


def test_store_writes_derived_volume_and_provenance(db):
    """
    volume_usd was NULL on every row: it was read off the normalized
    market, which carries None by design. It must come from the quote.
    """
    shadow.store(db, _sports_verdict(), MARKET, BBO)
    r = db.execute("SELECT * FROM verdicts").fetchone()
    assert r["volume_usd"] == pytest.approx(20000.0 * 0.49)
    assert r["liquidity_usd"] == pytest.approx(30000.0 * 0.49)
    assert r["bid_shares"] == 5000.0
    assert r["model"] == "jev-1.13.0", "which Jev served it; the id floats"
    assert r["outcome"] == "Over" and r["period"] == "NS"
    assert 3.9 < r["hours_to_event"] < 4.1


def test_rescore_carries_sports_fields(db):
    """
    Without these, every stored sports row re-scored as non-sports:
    research_would_help sat at 0.0, hit the floor, and --analyze reported
    ~0% escalation. The database was right; the analysis was wrong.
    """
    shadow.store(db, _sports_verdict(), MARKET, BBO)
    rows = db.execute("SELECT * FROM verdicts").fetchall()
    v = shadow._rescore(rows, GateThresholds())[0]
    assert v.is_sports is True
    assert v.stat_aggregation == 1.8
    assert v.escalate is True, v.veto_reason


def test_cooldown_excludes_structural_rejects(db):
    """
    A structural reject never reached Jev; re-checking it costs a quote.
    A Saturday-night book rejected as spread=0.06 is exactly the one that
    tightens by Sunday morning.
    """
    shadow.store(db, _sports_verdict("nfl-judged"), MARKET, BBO)
    shadow.store(
        db,
        _sports_verdict("nfl-thin", escalate=False,
                        structural="spread=0.06 > 0.04"),
        MARKET, BBO,
    )
    seen = shadow.recently_seen(db, hours=20)
    assert "nfl-judged" in seen
    assert "nfl-thin" not in seen


def test_migration_adds_columns_to_an_old_database(tmp_path):
    path = str(tmp_path / "old.db")
    c = sqlite3.connect(path)
    c.execute(
        "CREATE TABLE verdicts (id INTEGER PRIMARY KEY, seen_at TEXT NOT NULL, "
        "market_slug TEXT NOT NULL, escalate INTEGER NOT NULL)"
    )
    c.commit()
    c.close()
    conn = shadow.connect(path)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(verdicts)")}
    conn.close()
    assert set(shadow._MIGRATIONS) <= cols


def test_store_keeps_what_jev_saw_for_replay(db):
    """A learning loop that rewrites questions needs the original text."""
    m = {**MARKET, "description": "Resolves YES if PHI records 22+ first downs.",
         "tags": ["nfl", "sports"]}
    shadow.store(db, _sports_verdict(), m, BBO)
    r = db.execute("SELECT description, tags FROM verdicts").fetchone()
    assert r["description"].startswith("Resolves YES")
    assert r["tags"] == '["nfl", "sports"]'


def test_event_key_groups_markets_that_resolve_together():
    """
    "over 21.5" and "over 27.5" in one game are not independent draws --
    a 9-3 final loses both. The event count is the honest sample size.
    """
    a = shadow._event_key("tsc-nfl-min-chi-2026-09-20-total-21pt5")
    b = shadow._event_key("tsc-nfl-min-chi-2026-09-20-total-27pt5")
    c = shadow._event_key("tsc-nfl-pit-ne-2026-09-20-total-24pt5")
    assert a == b != c


def test_calibrate_reports_events_not_just_markets(tmp_path, capsys):
    path = str(tmp_path / "cal.db")
    conn = shadow.connect(path)
    for i, (slug, outcome) in enumerate([
        ("tsc-nfl-min-chi-2026-09-20-total-21pt5", "0"),
        ("tsc-nfl-min-chi-2026-09-20-total-24pt5", "0"),
        ("tsc-nfl-car-atl-2026-09-20-total-26pt5", "1"),
    ]):
        conn.execute(
            "INSERT INTO verdicts (seen_at, market_slug, bid, ask, escalate, "
            "resolved_outcome) VALUES (?,?,?,?,?,?)",
            ("t", slug, 0.93, 0.94, 1, outcome))
    conn.commit(); conn.close()
    shadow.calibrate(path)
    out = capsys.readouterr().out
    assert "3 settled markets (2 events)" in out
    assert "0.92-0.96" in out
