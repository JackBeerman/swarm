"""
Tests for the in-play arithmetic. None of this touches a socket: the
parsing and the fair-value model are pure functions, and they are the
parts that would silently produce wrong numbers on a live game.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("TYPESAFE_API_KEY", "test-key")

import inplay  # noqa: E402
from inplay import (  # noqa: E402
    NFL_PERIOD_S,
    GameState,
    _parse_clock,
    _parse_score,
    total_fair_value,
    total_line_from_slug,
)


def _gs(period, score="0-0", elapsed=None, live=True):
    return GameState({"slug": "g", "period": period, "score": score,
                      "elapsed": elapsed, "live": live})


def test_parse_score_and_clock():
    assert _parse_score("21-17") == (21, 17)
    assert _parse_score("0-0") == (0, 0)
    assert _parse_score(None) is None
    assert _parse_score("abc") is None
    assert _parse_clock("14:45") == 885
    assert _parse_clock("0:07") == 7
    assert _parse_clock(None) is None
    assert _parse_clock("bad") is None


def test_seconds_left_in_regulation():
    assert _gs("Q1", elapsed="15:00").seconds_left == 4 * NFL_PERIOD_S
    assert _gs("Q1", elapsed="14:45").seconds_left == 3 * NFL_PERIOD_S + 885
    assert _gs("Q4", elapsed="2:00").seconds_left == 120
    assert _gs("Q3", elapsed="7:30").seconds_left == NFL_PERIOD_S + 450


def test_break_periods_map_to_start_of_next_quarter():
    """
    "End Q1" was the first period string the live loop saw. Returning
    None for it went quiet for the whole quarter break.
    """
    assert _gs("End Q1").seconds_left == 3 * NFL_PERIOD_S
    assert _gs("End Q2").seconds_left == 2 * NFL_PERIOD_S
    assert _gs("Halftime").seconds_left == 2 * NFL_PERIOD_S
    assert _gs("End Q3").seconds_left == 1 * NFL_PERIOD_S


def test_finished_and_overtime():
    assert _gs("FT", score="24-20").seconds_left == 0
    assert _gs("FT").state == "finished"
    assert _gs("OT", elapsed="9:12").seconds_left is None, "no OT model yet"
    assert _gs("NS").state == "not_started"


def test_points_and_fraction():
    g = _gs("Q2", score="10-7", elapsed="15:00")
    assert g.points == 17
    assert g.fraction_played == pytest.approx(0.25)


def test_total_line_from_slug_only_game_totals():
    assert total_line_from_slug("tsc-nfl-phi-ten-2026-09-20-total-39pt5") == 39.5
    assert total_line_from_slug("tsc-nfl-phi-ten-2026-09-20-total-21pt5") == 21.5
    assert total_line_from_slug("tsc-nfl-cle-tb-2026-09-20-1h-8pt5") is None
    assert total_line_from_slug("tsc-nfl-no-bal-2026-09-20-4q-1pt5") is None
    assert total_line_from_slug("asc-nfl-phi-ten-2026-09-20-pos-17pt5") is None
    assert total_line_from_slug("tsc-nfl-x-total-bad") is None


def test_total_fair_value_sanity():
    # already over: certainty
    assert total_fair_value(20.5, _gs("Q3", score="14-10", elapsed="5:00"), 44.0) == 1.0
    # needs 30 more points with 2 minutes left: ~0
    assert total_fair_value(50.5, _gs("Q4", score="10-10", elapsed="2:00"), 44.0) < 0.01
    # more points so far -> higher probability, same clock
    lo = total_fair_value(44.5, _gs("Q2", score="7-3", elapsed="10:00"), 44.0)
    hi = total_fair_value(44.5, _gs("Q2", score="14-10", elapsed="10:00"), 44.0)
    assert 0.0 < lo < hi < 1.0
    # no clock -> no number, never a guess
    assert total_fair_value(44.5, _gs("OT", score="20-20", elapsed="9:00"), 44.0) is None


def test_fair_value_agrees_with_the_book_at_kickoff():
    """
    At kickoff, the pre-game total IS the market's fair value: a line set
    at 40.5 must price near 0.50. The Poisson version failed this live
    (0.27 against a 0.50 book at Q2); a model the book would laugh at
    cannot be allowed to report an "edge".
    """
    fv = total_fair_value(40.5, _gs("Q1", score="0-0", elapsed="15:00"), 40.5)
    assert 0.45 < fv < 0.55, fv


def test_fair_value_dispersion_is_realistic_not_poisson():
    """
    Live at Q2 890s, 7 points scored, line 40.5, anchor 40.5: the book
    sat at 0.50/0.51. The model must land in the same neighbourhood, not
    at 0.27. Mean 7 + 40.5*0.747 = 37.3, need 40.5, sd 13.5*sqrt(0.747)
    = 11.7 -> z 0.27 -> p_over ~0.39. Within a dime of the book, with the
    remaining gap being the book's own view of pace.
    """
    fv = total_fair_value(40.5, _gs("Q2", score="0-7", elapsed="14:50"), 40.5)
    assert 0.33 < fv < 0.48, fv


def test_fair_value_uses_break_period_clock():
    """During a break the model must still produce a number."""
    fv = total_fair_value(40.5, _gs("End Q1", score="0-7"), 40.5)
    assert fv is not None and 0.0 < fv < 1.0


def test_calibration_log_records_fair_value_beside_the_book(tmp_path):
    """
    A fair value is only worth something if it can be scored later --
    against the book now, against settlement once the market resolves.
    Same discipline as shadow.db.
    """
    conn = inplay.connect_db(str(tmp_path / "fv.db"))
    gs = _gs("Q2", score="0-7", elapsed="13:29")
    inplay.record_fv(conn, "nfl-phi-ten", "tsc-nfl-phi-ten-total-40pt5", 40.5,
                     gs, 40.5, 0.39, {"bid": 0.57, "ask": 0.59}, 0.90)
    r = conn.execute("SELECT * FROM fair_values").fetchone()
    conn.close()
    assert r["line"] == 40.5 and r["points"] == 7
    assert r["seconds_left"] == 2 * NFL_PERIOD_S + 809
    assert r["fv"] == pytest.approx(0.39)
    assert r["bid"] == 0.57 and r["ask"] == 0.59
    assert r["gate_score"] == 0.90
    assert r["model"] == inplay.FV_MODEL
    assert r["resolved_outcome"] is None, "filled by a backfill, never here"
