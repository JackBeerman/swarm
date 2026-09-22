"""Weather is arithmetic: forecast, band, error model. No network here."""

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
