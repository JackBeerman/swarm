"""
Ensemble band probabilities. No network: the fixtures are real Open-Meteo
ensemble responses (NYC, 2026-09-23, fahrenheit, timezone=GMT) trimmed to
three members, so the parser is tested against the wire shape, not a guess.
"""

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import respx

import weather_ensemble as we

FIX = Path(__file__).parent / "fixtures"
LADDER = [(None, 63.0), (64.0, 65.0), (66.0, 67.0), (68.0, 69.0), (70.0, None)]


def _load(model: str) -> dict:
    return json.loads((FIX / f"open_meteo_ensemble_{model}.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("model", ["gfs025", "google_weathernext2_ensemble"])
def test_parser_reads_the_live_shape(model):
    s = we.parse_open_meteo(_load(model))
    assert len(s.members) == 3, "control temperature_2m counts as a member, plus _memberNN"
    assert len(s.times) == 72 and all(len(m) == 72 for m in s.members)
    assert s.times[0] == datetime(2026, 9, 23, 0, tzinfo=timezone.utc)
    assert all(40 < v < 90 for m in s.members for v in m), "fahrenheit, as requested"


def test_members_are_ordered_control_first_then_by_number():
    p = _load("google_weathernext2_ensemble")
    h = p["hourly"]
    s = we.parse_open_meteo(p)
    assert s.members[0] == h["temperature_2m"]
    assert s.members[2] == h["temperature_2m_member63"]


def test_utc_offset_in_the_response_is_undone():
    p = _load("gfs025")
    p["utc_offset_seconds"] = -4 * 3600          # as if timezone=America/New_York (EDT)
    s = we.parse_open_meteo(p)
    assert s.times[0] == datetime(2026, 9, 23, 4, tzinfo=timezone.utc)


def test_celsius_is_converted():
    p = _load("gfs025")
    p["hourly_units"]["temperature_2m"] = "°C"
    p["hourly"]["temperature_2m"] = [20.0] * 72
    assert we.parse_open_meteo(p).members[0][0] == pytest.approx(68.0)


def test_climate_day_is_local_standard_time_even_in_summer():
    # New York in September is on EDT (UTC-4) but the climate day is EST (UTC-5):
    # 05:00 UTC to 05:00 UTC, i.e. 01:00 to 00:59 on the wall clock.
    start, end = we.climate_day_utc("nychigh", "2026-09-23")
    assert start == datetime(2026, 9, 23, 5, tzinfo=timezone.utc)
    assert end == datetime(2026, 9, 24, 5, tzinfo=timezone.utc)
    start, _ = we.climate_day_utc("laxhigh", "2026-09-23")
    assert start == datetime(2026, 9, 23, 8, tzinfo=timezone.utc)


def test_local_standard_date_decides_the_lead():
    late_evening = datetime(2026, 9, 23, 2, 0, tzinfo=timezone.utc)   # 21:00 EST on the 22nd
    assert str(we.local_standard_date("nychigh", late_evening)) == "2026-09-22"
    assert str(we.local_standard_date("laxhigh", late_evening)) == "2026-09-22"


def test_daily_max_uses_only_the_climate_day_hours():
    t0 = datetime(2026, 9, 23, 0, tzinfo=timezone.utc)
    times = [t0 + timedelta(hours=i) for i in range(48)]
    m = [50.0] * 48
    m[3] = 99.0          # 03 UTC: still the 22nd in EST, must be ignored
    m[20] = 70.0         # 20 UTC: afternoon of the 23rd
    m[29] = 98.0         # 05 UTC on the 24th: the next climate day
    s = we.MemberSeries(times, [m, [None] * 48])
    start, end = we.climate_day_utc("nychigh", "2026-09-23")
    assert we.member_daily_max(s, start, end) == [70.0], "all-None member is dropped"


def test_member_short_of_the_day_is_dropped():
    s = we.parse_open_meteo(_load("gfs025"))
    start, end = we.climate_day_utc("nychigh", "2026-09-25")   # beyond the 72 h fixture
    assert we.member_daily_max(s, start, end) == []


def test_band_probs_are_smoothed_fractions_that_sum_to_one():
    maxes = [66.4, 66.6, 67.2, 65.0, 68.9]      # reported: 66, 67, 67, 65, 69
    ps = we.ensemble_band_probs(maxes, LADDER, alpha=0.5)
    assert sum(ps) == pytest.approx(1.0)
    k, n = len(LADDER), len(maxes)
    assert ps[2] == pytest.approx((3 + 0.5) / (n + 0.5 * k))
    assert ps[0] == pytest.approx(0.5 / (n + 0.5 * k)), "empty band is small, never 0"
    assert all(0 < p < 1 for p in ps)
    # every member in one band still leaves room for the others
    assert max(we.ensemble_band_probs([66.0] * 50, LADDER)) < 1


def test_rounding_is_half_up_like_the_climate_report():
    assert we.reported_high(66.5) == 67
    assert we.reported_high(66.49) == 66
    assert we.ensemble_band_probs([63.5], LADDER, alpha=0)[1] == 1.0


def test_dressing_and_bias_move_mass_the_right_way():
    maxes = [66.0] * 20
    raw = we.ensemble_band_probs(maxes, LADDER, alpha=0)
    dressed = we.ensemble_band_probs(maxes, LADDER, alpha=0, dress_sd=1.5)
    assert sum(dressed) == pytest.approx(1.0, abs=1e-6)
    assert dressed[2] < raw[2] and dressed[1] > 0 and dressed[3] > 0
    warm = we.ensemble_band_probs(maxes, LADDER, alpha=0, bias=2.0)
    assert warm[3] == 1.0


def test_nws_normal_matches_weather_py():
    import weather
    ps = we.nws_normal_probs(67.0, 1, LADDER)
    assert ps[2] == pytest.approx(weather.band_probability(67.0, 66.0, 67.0, weather.ERROR_SD[1]))
    assert sum(ps) == pytest.approx(1.0, abs=1e-6)


EVENTS = [{"slug": "nyc-high-2026-09-23", "markets": [
    {"slug": "tc-temp-nychigh-2026-09-23-gte66lt67f", "outcomePrices": '["0.30","0.34"]'},
    {"slug": "tc-temp-nychigh-2026-09-23-lt64f", "outcomePrices": '["0.05","0.07"]'},
    {"slug": "tc-temp-nychigh-2026-09-23-gte70f", "outcomePrices": '["0.02","0.04"]'},
    {"slug": "tc-temp-nychigh-2026-09-23-gte64lt65f", "outcomePrices": '["0.20","0.24"]'},
    {"slug": "tc-temp-nychigh-2026-09-23-gte68lt69f", "outcomePrices": '["0.25","0.29"]'},
    {"slug": "tc-temp-nychigh-2026-09-23-gte72f", "closed": True},
    {"slug": "asc-nfl-something", "outcomePrices": '["0.5","0.5"]'},
]}]


def test_ladders_group_sort_and_skip_closed_and_foreign():
    lads = we.ladders_from_events(EVENTS)
    assert list(lads) == [("nychigh", "2026-09-23")]
    lad = lads[("nychigh", "2026-09-23")]
    assert lad.bands == LADDER
    assert lad.mids[2] == pytest.approx(0.32)


class _Fixed:
    """A non-Open-Meteo source behind the same interface (how WeatherNext 3 plugs in)."""
    name = "fixed"

    async def fetch(self, client, lat, lon, days):
        return we.parse_open_meteo(_load("google_weathernext2_ensemble"))


@respx.mock
async def test_compute_and_store_record_every_source_side_by_side(tmp_path):
    respx.get(we.OPEN_METEO_ENSEMBLE).mock(return_value=httpx.Response(200, json=_load("gfs025")))
    now = datetime(2026, 9, 23, 2, 0, tzinfo=timezone.utc)      # evening of the 22nd, EST
    lads = we.ladders_from_events(EVENTS)
    async with httpx.AsyncClient() as c:
        bands, runs = await we.compute(lads, [we.OpenMeteoEnsemble("gfs025"), _Fixed()],
                                       now, c, nws_client=None, pause=0)
    assert {r[1] for r in bands} == {"om:gfs025", "fixed"}
    assert len(bands) == 2 * len(LADDER)
    assert all(r[5] == 1 for r in bands), "lead counted from the station's standard date"
    for src in ("om:gfs025", "fixed"):
        assert sum(r[8] for r in bands if r[1] == src) == pytest.approx(1.0)
    assert all(r[5] == 3 for r in runs), "three members each in the trimmed fixture"

    db = str(tmp_path / "w.db")
    with closing(we.connect(db)) as conn:
        we.store(conn, bands, runs)
        we.store(conn, bands, runs)                               # idempotent
        assert conn.execute("SELECT COUNT(*) FROM band_forecasts").fetchone()[0] == len(bands)
        # weather.py's own table still exists alongside
        assert conn.execute("SELECT COUNT(*) FROM forecasts").fetchone()[0] == 0


def _row(src, slug, p, mid, y, lead=1, at="t0"):
    return {"source": src, "market_slug": slug, "p_model": p, "market_mid": mid,
            "resolved_outcome": str(y), "lead_days": lead, "city": "nychigh",
            "target_date": "2026-09-23", "at": at}


def test_brier_table_scores_source_and_market_on_the_same_rows():
    rows = [_row("a", "s1", 0.9, 0.5, 1), _row("a", "s2", 0.1, 0.5, 0),
            _row("b", "s1", 0.5, 0.5, 1)]
    t = {r["source"]: r for r in we.brier_table(rows)}
    assert t["a"]["brier"] == pytest.approx(0.01)
    assert t["a"]["market"] == pytest.approx(0.25)
    assert t["b"]["n"] == 1 and t["a"]["days"] == 1


def test_paired_uses_only_markets_every_source_priced():
    rows = [_row("a", "s1", 0.9, 0.5, 1), _row("b", "s1", 0.6, 0.5, 1),
            _row("a", "s2", 0.1, 0.5, 0)]                          # b missing on s2
    pr = we.paired(rows)
    assert pr["a"] == (1, pytest.approx(0.01))
    assert pr["b"][1] == pytest.approx(0.16)
    assert pr["market"][1] == pytest.approx(0.25)


def test_outcomes_weather_py_already_has_are_copied_not_refetched(tmp_path):
    db = str(tmp_path / "w.db")
    with closing(we.connect(db)) as conn:
        conn.execute("INSERT INTO forecasts (at, market_slug, city, target_date, lead_days,"
                     " forecast_high, band_lo, band_hi, p_model, market_mid, resolved_outcome)"
                     " VALUES ('t','s1','nychigh','2026-09-23',1,67,66,67,0.3,0.3,'1')")
        we.store(conn, [("t2", "om:x", "s1", "nychigh", "2026-09-23", 1, 66.0, 67.0, 0.4, 0.3),
                        ("t2", "om:x", "s9", "nychigh", "2026-09-23", 1, 68.0, 69.0, 0.2, 0.3)], [])
        assert we.copy_known_outcomes(conn) == 1
    with closing(sqlite3.connect(db)) as conn:
        got = dict(conn.execute("SELECT market_slug, resolved_outcome FROM band_forecasts"))
    assert got == {"s1": "1", "s9": None}


def test_unpriced_ladders_record_null_not_zero(tmp_path):
    events = [{"markets": [
        {"slug": "tc-temp-laxhigh-2026-09-24-lt78f", "outcomePrices": '["0","0"]'},
        {"slug": "tc-temp-laxhigh-2026-09-24-gte78f", "outcomePrices": '["0","0"]'}]}]
    lad = we.ladders_from_events(events)[("laxhigh", "2026-09-24")]
    assert lad.mids == [None, None]
    db = str(tmp_path / "w.db")
    with closing(we.connect(db)) as conn:
        we.store(conn, [("t", "om:x", "s1", "laxhigh", "2026-09-24", 1, None, 77.0, 0.4, 0.0)], [])
    with closing(we.connect(db)) as conn:                     # legacy 0.0 cleaned on connect
        assert conn.execute("SELECT market_mid FROM band_forecasts").fetchone()[0] is None
    rows = [_row("a", "s1", 0.9, None, 1), _row("a", "s2", 0.2, 0.5, 0)]
    (t,) = we.brier_table(rows)
    assert t["n"] == 1 and t["market"] == pytest.approx(0.25)


def test_sweep_finds_the_bias_of_a_cold_grid_cell():
    # Every member sits 6 F below what happened: the fit should warm it back.
    ladder = [(None, 77.0), (78.0, 79.0), (80.0, 81.0), (82.0, 83.0), (84.0, None)]
    runs, bands = [], {}
    for i, truth_band in enumerate([2, 2, 3, 1]):
        day = f"2026-09-{10 + i}"
        truth_mid = [76, 78.5, 80.5, 82.5, 85][truth_band]
        runs.append({"at": "t", "source": "om:x", "city": "laxhigh", "target_date": day,
                     "maxes_json": json.dumps([truth_mid - 6 + d for d in (-0.3, 0.0, 0.3)])})
        bands[("t", "om:x", "laxhigh", day)] = [
            (lo, hi, 1.0 if j == truth_band else 0.0) for j, (lo, hi) in enumerate(ladder)]
    fit = we.sweep_fit(runs, bands)[("om:x", "laxhigh")]
    assert fit["days"] == 4
    assert fit["best_bias"] == pytest.approx(6.0, abs=1.0)
    assert fit["best"] < fit["raw"]
