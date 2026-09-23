"""
Closing line value: sign conventions, and that an in-play price never
becomes a closing line or gets averaged into CLV.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import closer
from adapters import normalize_market
from closer import Bet

T0 = datetime(2026, 9, 27, 17, 0, tzinfo=timezone.utc)      # kickoff


def _bet(side, entry, at=T0 - timedelta(hours=3), ev="nfl-a-b-2026-09-27", slug="m1",
         lane="fast", source="acted"):
    return Bet(lane, source, slug, ev, side, entry, at)


def _close(bid, ask, status="pre_start", closed_at=T0):
    return {"status": status, "bid": bid, "ask": ask, "closed_at": closed_at.isoformat()}


# --- sign conventions ------------------------------------------------------

def test_yes_bought_below_the_close_is_positive():
    assert closer.clv("YES", 0.40, 0.45) == pytest.approx(0.05)
    assert closer.clv("YES", 0.50, 0.45) == pytest.approx(-0.05)


def test_no_is_scored_on_the_yes_scale_entry_at_the_bid():
    # NO bought at bid 0.60 costs 0.40; the close has YES at 0.50, so NO at
    # 0.50: we paid 0.40 for something now worth 0.50.
    assert closer.clv("NO", 0.60, 0.50) == pytest.approx(0.10)
    # The same numbers as a YES bet would be the mirror image.
    assert closer.clv("YES", 0.60, 0.50) == pytest.approx(-0.10)


def test_score_bet_uses_the_closing_mid():
    label, v = closer.score_bet(_bet("YES", 0.40), _close(0.44, 0.46))
    assert (label, v) == ("ok", pytest.approx(0.05))


# --- in-play exclusion -----------------------------------------------------

def test_an_entry_after_the_start_has_no_closing_line():
    late = _bet("YES", 0.40, at=T0 + timedelta(minutes=18))
    assert closer.score_bet(late, _close(0.44, 0.46)) == ("entry_after_start", None)


def test_a_missed_close_carries_no_price_and_scores_nothing():
    c = _close(None, None, status="missed")
    assert closer.score_bet(_bet("YES", 0.40), c) == ("close_missed", None)
    assert closer.score_bet(_bet("YES", 0.40), None) == ("no_close", None)


def test_a_quote_that_lands_after_the_start_is_stored_as_a_miss_without_price():
    bbo = {"bid": 0.30, "ask": 0.31}
    row = closer.close_row("m1", "ev", T0, bbo, T0 + timedelta(seconds=2), ["fastlane"])
    assert row["status"] == "missed"
    assert row["bid"] is None and row["ask"] is None and row["minutes_before_start"] is None
    ok = closer.close_row("m1", "ev", T0, bbo, T0 - timedelta(minutes=7), ["fastlane"])
    assert ok["status"] == "pre_start" and ok["bid"] == 0.30
    assert ok["minutes_before_start"] == pytest.approx(7.0)


def test_a_miss_can_be_upgraded_by_a_real_close_but_a_close_is_never_overwritten(tmp_path):
    db = str(tmp_path / "closes.db")
    with closer.connect(db) as conn:
        closer.store_close(conn, closer.close_row("m1", "ev", T0, None, T0, ["shadow"]))
        good = closer.close_row("m1", "ev", T0 + timedelta(days=1), {"bid": 0.3, "ask": 0.32},
                                T0 + timedelta(days=1, minutes=-5), ["shadow"])
        closer.store_close(conn, good)                       # postponed game, real close
        later_miss = closer.close_row("m1", "ev", T0, None, T0 + timedelta(days=2), ["shadow"])
        closer.store_close(conn, later_miss)
        other = dict(good, bid=0.9, ask=0.92)
        closer.store_close(conn, other)
    row = closer.load_closes(db)["m1"]
    assert row["status"] == "pre_start" and row["bid"] == 0.3


@pytest.mark.parametrize("close_at, state, expect", [
    (T0 + timedelta(minutes=5), "not_started", "quote"),
    (T0 + timedelta(minutes=5), "in_play", "missed"),     # game already live: no price
    (T0 + timedelta(minutes=5), "finished", "missed"),
    (T0 - timedelta(minutes=1), "not_started", "missed"),
    (T0 + timedelta(hours=2), "not_started", "wait"),
    (None, "unknown", "wait"),
])
def test_classify_capture(close_at, state, expect):
    assert closer.classify_capture(close_at, T0, state, window_min=12) == expect


def test_weather_line_closes_when_the_observation_day_starts_not_ends():
    ev = {"slug": "temp-nychigh-2026-09-24", "category": "climate",
          "startTime": "2026-09-24T04:00:00Z", "endDate": "2026-09-25T04:00:00Z"}
    norm = normalize_market({"slug": "tc-temp-nychigh-2026-09-24-gte66lt67f"}, ev)
    assert norm["event_at"] == "2026-09-25T04:00:00Z"          # adapters: the window END
    assert closer.line_close_time(norm) == datetime(2026, 9, 24, 4, tzinfo=timezone.utc)
    game = normalize_market({"slug": "x"}, {"startTime": "2026-09-27T17:00:00Z", "category": "sports"})
    assert closer.line_close_time(game) == T0


def test_missed_rows_are_excluded_from_the_mean_and_counted():
    bets = [_bet("YES", 0.40, slug="a"), _bet("YES", 0.40, slug="b"),
            _bet("YES", 0.40, slug="c", at=T0 + timedelta(minutes=1))]
    closes = {"a": _close(0.49, 0.51), "b": _close(None, None, "missed"), "c": _close(0.9, 0.92)}
    g, = closer.summarize(bets, closes)
    assert g["scored_bets"] == 1 and g["mean_clv"] == pytest.approx(0.10)
    assert g["excluded"] == {"close_missed": 1, "entry_after_start": 1}


# --- counted by event ------------------------------------------------------

def test_markets_in_one_event_average_to_one_point():
    bets = [_bet("YES", 0.40, slug="a", ev="e1"), _bet("YES", 0.40, slug="b", ev="e1"),
            _bet("YES", 0.40, slug="c", ev="e1"), _bet("YES", 0.40, slug="d", ev="e2")]
    closes = {s: _close(0.49, 0.51) for s in "abc"} | {"d": _close(0.29, 0.31)}
    g, = closer.summarize(bets, closes)
    # Three markets at +0.10 and one at -0.10 are two events: mean 0.0, not +0.05.
    assert g["scored_events"] == 2
    assert g["mean_clv"] == pytest.approx(0.0)


def test_bootstrap_ci_needs_two_events_and_brackets_the_mean():
    assert closer.bootstrap_ci([0.05]) is None
    vals = [0.01, 0.03, -0.02, 0.04, 0.02, 0.00]
    lo, hi = closer.bootstrap_ci(vals)
    assert lo <= sum(vals) / len(vals) <= hi
    assert closer.bootstrap_ci(vals) == (lo, hi), "seeded: the report must be reproducible"


# --- the capture pass, against a fake exchange ----------------------------

class _FakeMarkets:
    def __init__(self):
        self.calls = []

    async def bbo(self, slug):
        self.calls.append(slug)
        return {"marketData": {"bestBid": {"value": "0.40"}, "bestAsk": {"value": "0.42"}}}


class _FakeEvents:
    def __init__(self, events):
        self._events = events
        self.calls = 0

    async def list(self, params):
        self.calls += 1
        return {"events": self._events}


class _FakePM:
    def __init__(self, events):
        self.events = _FakeEvents(events)
        self.markets = _FakeMarkets()


def _iso(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


async def test_capture_quotes_upcoming_marks_live_as_missed_and_ignores_untracked(
        tmp_path, monkeypatch):
    monkeypatch.setattr(closer, "CALL_SPACING_S", 0.0)
    now = datetime.now(timezone.utc)
    fl = tmp_path / "fastlane.db"
    with sqlite3.connect(fl) as c:
        c.execute("CREATE TABLE signals (market_slug TEXT, event_slug TEXT)")
        c.executemany("INSERT INTO signals VALUES (?, ?)",
                      [("soon-1", "ev-soon"), ("live-1", "ev-live"), ("soon-2", "ev-soon")])
    events = [
        {"slug": "ev-soon", "startTime": _iso(now + timedelta(minutes=6)), "period": "NS",
         "markets": [{"slug": "soon-1"}, {"slug": "soon-2"}, {"slug": "untracked"}]},
        {"slug": "ev-live", "startTime": _iso(now - timedelta(minutes=30)), "period": "Q2",
         "markets": [{"slug": "live-1"}]},
    ]
    pm = _FakePM(events)
    dbs = {"shadow": str(tmp_path / "none"), "traces": str(tmp_path / "none"),
           "fastlane": str(fl), "weather": str(tmp_path / "none")}
    db = str(tmp_path / "closes.db")
    stats = await closer.capture(db=db, pm=pm, dbs=dbs, max_calls=2)
    rows = {r["market_slug"]: r for r in closer.load_closes(db).values()}
    # Budget of 2: one listing + one quote. The second upcoming market waits.
    assert pm.events.calls == 1 and pm.markets.calls == ["soon-1"]
    assert stats["budget_skipped"] == 1
    assert rows["soon-1"]["status"] == "pre_start" and rows["soon-1"]["bid"] == 0.40
    assert rows["live-1"]["status"] == "missed" and rows["live-1"]["bid"] is None
    assert "untracked" not in rows and "soon-2" not in rows

    # Next pass: already-captured markets are not quoted again.
    pm2 = _FakePM(events)
    await closer.capture(db=db, pm=pm2, dbs=dbs, max_calls=5)
    assert pm2.markets.calls == ["soon-2"]


def test_closer_and_paper_never_reach_the_order_path():
    import pathlib
    root = pathlib.Path(closer.__file__).parent
    for name in ("closer.py", "paper.py"):
        src = (root / name).read_text(encoding="utf-8")
        assert ".orders" not in src and "daemon" not in src, name
