"""Weather is arithmetic: forecast, band, error model. No network here."""

from contextlib import closing

import pytest

import weather as w


def test_slug_bands_match_the_displayed_outcomes():
    assert w.parse_slug("tc-temp-nychigh-2026-09-21-gte66lt67f") == {
        "city": "nychigh", "date": "2026-09-21", "lo": 66.0, "hi": 67.0}
    assert w.parse_slug("tc-temp-nychigh-2026-09-21-lt66f")["hi"] == 65.0
    assert w.parse_slug("tc-temp-nychigh-2026-09-21-gte74f") == {
        "city": "nychigh", "date": "2026-09-21", "lo": 74.0, "hi": None}
    assert w.parse_slug("asc-nfl-x") is None


def test_band_probability_is_a_proper_distribution():
    f, sd = 67.0, 2.5
    bands = [(None, 65.0), (66.0, 67.0), (68.0, 69.0), (70.0, 71.0), (72.0, 73.0), (74.0, None)]
    total = sum(w.band_probability(f, lo, hi, sd) for lo, hi in bands)
    assert total == pytest.approx(1.0, abs=1e-6)
    # a two-degree band centred on the forecast, day-ahead: about a third
    assert 0.28 < w.band_probability(67.0, 66.0, 67.0, 2.5) < 0.36
    # forecast well outside an open band
    assert w.band_probability(67.0, 74.0, None, 2.5) < 0.01


def test_an_unpriced_market_has_no_price_not_a_zero_price():
    # Tomorrow's ladder, listed before trading opens: outcomePrices are 0.
    assert w.market_price({"outcomePrices": '["0","0"]'}) is None
    assert w.market_price({"outcomePrices": None}) is None
    assert w.market_price({}) is None
    assert w.market_price({"outcomePrices": '["0.30","0.34"]'}) == pytest.approx(0.32)
    assert w.market_price({"outcomePrices": '["0","0.01"]'}) == pytest.approx(0.005)


def test_stored_zero_prices_become_null_and_leave_the_market_brier(tmp_path, capsys):
    db = str(tmp_path / "w.db")
    with closing(w.connect(db)) as conn:
        conn.executemany(
            "INSERT INTO forecasts (at, market_slug, city, target_date, lead_days, forecast_high,"
            " band_lo, band_hi, p_model, market_mid, resolved_outcome)"
            " VALUES ('t', ?, 'nychigh', '2026-09-24', 1, 67, 66, 67, 0.3, ?, '1')",
            [("a", 0.0), ("b", 0.6)])
        conn.commit()
    with closing(w.connect(db)) as conn:                       # the migration runs on connect
        mids = dict(conn.execute("SELECT market_slug, market_mid FROM forecasts"))
    assert mids == {"a": None, "b": 0.6}
    w.score(db)
    out = capsys.readouterr().out
    assert "1 resolved weather markets" in out, "the unpriced row is not scored as a 0.0 price"
    assert "0.1600" in out, "market Brier on the priced row only: (0.6 - 1)^2"
