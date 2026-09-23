"""
Paper portfolios: the replay must size with the live code, pay the fee,
respect the event cap, settle from outcomes, and measure drawdown right.
"""

from datetime import datetime, timedelta, timezone

import pytest

import paper
from fees import fee_usd
from paper import Decision
from schemas import Side, TradeSignal
from swarm import RiskConfig, size_from_signal

T = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
FLOOR = 10.0


def _d(slug="m1", ev="e1", at=T, side="YES", bid=0.49, ask=0.50, sizing="kelly", p=0.80,
       outcome="1", settle=T + timedelta(hours=6), lane="slow"):
    return Decision(lane, slug, ev, at, side, bid, ask, sizing, p, 1.0, 1.0, outcome, settle)


def test_kelly_entry_uses_size_from_signal_and_pays_the_fee():
    res = paper.replay("slow", [_d()])
    expected = size_from_signal(
        TradeSignal(market_slug="m1", side=Side.YES, probability=0.80, confidence=1.0,
                    reasoning="x"), {"bid": 0.49, "ask": 0.50}, 100 - FLOOR, 1.0, RiskConfig())
    row, = res.rows
    assert row["status"] == "opened"
    assert row["qty"] == expected.quantity and row["price"] == expected.limit_price
    assert row["fee"] == fee_usd(0.50, expected.quantity) > 0
    # Won: paid qty*0.50 + fee, received qty*1.
    assert row["pnl"] == pytest.approx(expected.quantity * 0.50 - row["fee"])
    assert res.summary()["final_equity"] == pytest.approx(100 + row["pnl"])
    assert res.summary()["fees_usd"] == pytest.approx(row["fee"])


def test_no_side_pays_one_minus_bid_and_is_paid_on_a_zero():
    res = paper.replay("slow", [_d(side="NO", bid=0.50, ask=0.51, p=0.80, outcome="0")])
    row, = res.rows
    assert row["price"] == pytest.approx(0.50)
    assert row["pnl"] == pytest.approx(row["qty"] * (1 - 0.50) - row["fee"])


def test_fixed_one_share_stake_is_labelled_and_loses_price_plus_fee():
    res = paper.replay("fast", [_d(sizing="fixed_1_share", p=None, bid=0.42, ask=0.43,
                                   outcome="0")])
    row, = res.rows
    assert (row["sizing"], row["qty"], row["price"]) == ("fixed_1_share", 1, 0.43)
    assert row["pnl"] == pytest.approx(-(0.43 + fee_usd(0.43, 1)))
    assert res.summary()["sizing"] == {"fixed_1_share": 1}


def test_fixed_stake_still_respects_the_spread_limit():
    res = paper.replay("fast", [_d(sizing="fixed_1_share", p=None, bid=0.44, ask=0.62)])
    assert res.rows[0]["status"] == "skipped:spread"


def test_no_edge_is_not_a_bet():
    res = paper.replay("slow", [_d(p=0.52)])
    assert res.rows[0]["status"] == "skipped:no_edge_or_spread"
    assert res.summary()["bets"] == 0


def test_the_event_cap_binds_across_markets_in_one_event():
    ds = [_d(slug=f"m{i}", at=T + timedelta(minutes=i)) for i in range(3)]
    res = paper.replay("slow", ds)
    # First: bankroll 90, the 8% position cap -> 14 shares at 0.50 = $7.00.
    # Second: bankroll 90 - 7.00 - 0.25 fee = 82.75, event room 8.275 - 7.00
    # = $1.275 -> 2 shares = $1.00, under the $2 minimum -> dropped. All three
    # entries precede any settlement, so the event holds them at once.
    assert [r["status"] for r in res.rows] == ["opened", "skipped:event_cap", "skipped:event_cap"]
    assert res.rows[0]["notional"] == pytest.approx(7.00)
    assert res.rows[0]["notional"] <= RiskConfig().max_event_fraction * (100 - FLOOR)


def test_the_event_cap_shrinks_an_order_that_partly_fits():
    risk = RiskConfig(max_event_fraction=0.10, max_position_fraction=0.08)
    first = _d(slug="a", p=0.80, ask=0.50, bid=0.49)                  # $7.00 in e1
    cheap = _d(slug="b", at=T + timedelta(minutes=1), p=0.60, bid=0.19, ask=0.20)
    res = paper.replay("slow", [first, cheap], risk=risk)
    row = res.rows[1]
    # Room 0.10 * 82.75 - 7.00 = 1.275... at 0.20 a share: 6 shares = $1.20, under $2 -> dropped.
    # Raise the event cap and the same order is shrunk, not dropped.
    assert row["status"] == "skipped:event_cap"
    res2 = paper.replay("slow", [first, cheap], risk=RiskConfig(max_event_fraction=0.12))
    row2 = res2.rows[1]
    unconstrained = paper.replay("slow", [cheap]).rows[0]
    assert row2["status"] == "opened" and row2["qty"] < unconstrained["qty"]
    assert res2.rows[0]["notional"] + row2["notional"] <= 0.12 * (100 - FLOOR)


def test_a_different_event_is_not_capped_by_the_first():
    ds = [_d(slug="a", ev="e1"), _d(slug="b", ev="e2", at=T + timedelta(minutes=1))]
    res = paper.replay("slow", ds)
    assert [r["status"] for r in res.rows] == ["opened", "opened"]


def test_cash_returns_at_settlement_before_the_next_entry():
    first = _d(slug="a", ev="e1", settle=T + timedelta(hours=1))
    later = _d(slug="b", ev="e1", at=T + timedelta(hours=2))
    res = paper.replay("slow", [first, later])
    kinds = [p["kind"] for p in res.curve]
    assert kinds == ["start", "entry", "settle", "entry", "settle"]
    # The event's exposure was released on settlement, so the second bet is not capped.
    assert res.rows[1]["status"] == "opened"


def test_unresolved_positions_stay_open_at_cost():
    res = paper.replay("weather", [_d(outcome=None)])
    s = res.summary()
    row = res.rows[0]
    assert s["open_positions"] == 1 and s["settled"] == 0 and row["pnl"] is None
    # Equity at cost: only the fee has left.
    assert s["final_equity"] == pytest.approx(100 - row["fee"])


@pytest.mark.parametrize("curve, usd, pct", [
    ([100, 120, 90, 130, 125], 30.0, 0.25),
    ([100, 101, 102], 0.0, 0.0),
    ([100, 95, 97, 80], 20.0, 0.20),
    ([], 0.0, 0.0),
])
def test_max_drawdown(curve, usd, pct):
    assert paper.max_drawdown(curve) == (usd, pct)


def test_settlement_estimate_is_the_slug_date_plus_36h():
    assert paper.estimated_settle("asc-nfl-nyg-lar-2026-09-21-2h-pos-3pt5") == datetime(
        2026, 9, 22, 12, tzinfo=timezone.utc)
    assert paper.estimated_settle("ccpc-bilbrd-1song-any2026-posmal") is None


def test_weather_loader_skips_bookless_mids_and_lead_zero(tmp_path):
    import weather
    db = tmp_path / "weather.db"
    with weather.connect(str(db)) as c:
        rows = [("tc-temp-nychigh-2026-09-24-gte66lt67f", 1, 0.30, 0.0),     # old: no book as 0.0
                ("tc-temp-nychigh-2026-09-24-gte68lt69f", 1, 0.30, None),    # new: no book as NULL
                ("tc-temp-nychigh-2026-09-23-gte67lt68f", 1, 0.31, 0.50),    # real
                ("tc-temp-nychigh-2026-09-22-gte65lt66f", 0, 0.30, 0.57)]    # lead 0
        c.executemany("INSERT INTO forecasts (at, market_slug, city, target_date, lead_days,"
                      " forecast_high, p_model, market_mid) VALUES (?,?,?,?,?,?,?,?)",
                      [("2026-09-22T21:52:50+00:00", s, "nychigh", s[16:26], lead, 67.0, p, m)
                       for s, lead, p, m in rows])
    none = str(tmp_path / "missing.db")
    ds = paper.load_decisions({"shadow": none, "traces": none, "fastlane": none,
                               "weather": str(db)})["weather"]
    assert [d.market_slug for d in ds] == ["tc-temp-nychigh-2026-09-23-gte67lt68f"]
    d, = ds
    assert d.side == "NO" and d.probability == pytest.approx(0.69)
    assert (d.bid, d.ask) == (0.49, 0.51)
