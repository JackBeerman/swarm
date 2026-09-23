"""
Cross-venue matching. The whole risk is admitting a pair that is not the
same proposition, so most of these tests are refusals. Fixtures are real
public responses from both venues captured 2026-09-23 (White Sox at
Royals; NYC high temperature), trimmed.
"""

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import kalshi
import xvenue
from fees import fee_per_share

FIX = Path(__file__).parent / "fixtures" / "xvenue"
NOW = datetime(2026, 9, 23, 10, 40, tzinfo=timezone.utc)


def load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def k_series(*names):
    return {n: (load(f"kalshi_series_{n}.json")["series"], load(f"kalshi_events_{n}.json")["events"])
            for n in names}


MLB = ("KXMLBGAME", "KXMLBSPREAD", "KXMLBTOTAL")


def mlb_pairs(pm_events=None, series=None, **kw):
    pm = pm_events or {"mlb": load("pm_events_mlb.json")["events"]}
    pairs, skips, n_pm, n_k = xvenue.build_pairs(pm, series or k_series(*MLB), NOW, **kw)
    return pairs, skips


def by_key(pairs):
    return {(p.pm.slug, p.k.ticker): p.relation for p in pairs}


# --------------------------------------------------------------------------
# small parsers
# --------------------------------------------------------------------------

def test_event_ticker_date_and_eastern_start():
    info = xvenue.parse_event_ticker("KXMLBGAME-26SEP231940CWSKC")
    assert info["date"] == "2026-09-23" and info["teams"] == "CWSKC"
    assert info["start_utc"] == datetime(2026, 9, 23, 23, 40, tzinfo=timezone.utc)
    nfl = xvenue.parse_event_ticker("KXNFLGAME-26SEP24ATLGB")
    assert nfl["start_utc"] is None and nfl["teams"] == "ATLGB"
    assert xvenue.et_to_utc(datetime(2026, 12, 1, 19, 0)).hour == 0     # EST is UTC-5


def test_team_split_must_be_unique():
    assert xvenue.split_teams("NYJDET", "nfl") == ("NYJ", "DET")
    assert xvenue.split_teams("MIAMIN", "nfl") == ("MIA", "MIN")
    assert xvenue.split_teams("KCLV", "nfl") == ("KC", "LV")
    with pytest.raises(xvenue.Skip):
        xvenue.split_teams("XXXKC", "mlb")


def test_kalshi_team_table_names_teams_the_reference_table_knows():
    from reference import TEAMS
    for sport, table in xvenue.KALSHI_TEAMS.items():
        assert len(table) == len(TEAMS[sport])
        assert {pm for pm, _ in table.values()} == set(TEAMS[sport])


def test_kalshi_band_is_cross_checked_against_its_label():
    ev = load("kalshi_events_KXHIGHNY.json")["events"][0]
    bands = {m["ticker"].rsplit("-", 1)[1]: xvenue.kalshi_band(m) for m in ev["markets"]}
    assert bands["T65"] == (None, 64.0)
    assert bands["B65.5"] == (65.0, 66.0)
    assert bands["T72"] == (73.0, None)
    bad = dict(ev["markets"][0], yes_sub_title="63° or below")
    with pytest.raises(xvenue.Skip, match="label"):
        xvenue.kalshi_band(bad)


# --------------------------------------------------------------------------
# sports matching on the real CWS-KC slate
# --------------------------------------------------------------------------

def test_real_game_matches_moneyline_spreads_and_totals_with_the_right_orientation():
    pairs, _ = mlb_pairs()
    got = by_key(pairs)
    g = "26SEP231940CWSKC"
    # Polymarket moneyline YES = White Sox (marketSides long), not the title.
    assert got[("aec-mlb-cws-kc-2026-09-23", f"KXMLBGAME-{g}-CWS")] == "same"
    assert got[("aec-mlb-cws-kc-2026-09-23", f"KXMLBGAME-{g}-KC")] == "complement"
    # White Sox +1.5 is the complement of "Royals win by more than 1.5".
    assert got[("asc-mlb-cws-kc-2026-09-23-pos-1pt5", f"KXMLBSPREAD-{g}-KC2")] == "complement"
    # White Sox -1.5 is "White Sox win by more than 1.5".
    assert got[("asc-mlb-cws-kc-2026-09-23-neg-1pt5", f"KXMLBSPREAD-{g}-CWS2")] == "same"
    assert got[("tsc-mlb-cws-kc-2026-09-23-8pt5", f"KXMLBTOTAL-{g}-9")] == "same"
    # Never a spread paired with the wrong team or line.
    assert ("asc-mlb-cws-kc-2026-09-23-pos-1pt5", f"KXMLBSPREAD-{g}-CWS2") not in got
    assert ("asc-mlb-cws-kc-2026-09-23-pos-1pt5", f"KXMLBSPREAD-{g}-KC3") not in got
    # First-five and team totals never match a full-game Kalshi line.
    assert not any("-f5-" in s or "-tt-" in s for s, _ in got)


def test_prices_are_consistent_and_show_no_free_money_on_this_slate():
    pairs, _ = mlb_pairs()
    rows = [xvenue.pair_row(p) for p in pairs]
    assert rows and all(xvenue.best_edge(r)[0] < 0 for r in rows)
    # Two venues pricing one game within a cent or two: every edge is small.
    assert all(xvenue.best_edge(r)[0] > -0.08 for r in rows)


def test_rescheduled_game_is_a_near_miss_not_a_pair():
    pm = copy.deepcopy(load("pm_events_mlb.json")["events"])
    pm[0]["startTime"] = "2026-09-24T17:35:00Z"      # moved: Kalshi still says 19:40 ET
    pairs, skips = mlb_pairs({"mlb": pm})
    assert pairs == []
    assert any("start times differ" in s[2] for s in skips)


def test_a_second_kalshi_game_same_teams_same_day_is_ambiguous():
    ser = k_series(*MLB)
    for name in MLB:
        evs = ser[name][1]
        twin = copy.deepcopy(evs[0])
        twin["event_ticker"] = twin["event_ticker"].replace("1940CWSKC", "1310CWSKC")
        for m in twin["markets"]:
            m["ticker"] = m["ticker"].replace("1940CWSKC", "1310CWSKC")
        evs.append(twin)
    pm = copy.deepcopy(load("pm_events_mlb.json")["events"])
    pairs, skips = mlb_pairs({"mlb": pm}, ser)
    assert pairs == []
    assert any("ambiguous" in s[2] for s in skips)


def test_kalshi_spread_whose_team_id_disagrees_with_the_game_market_is_refused():
    ser = k_series(*MLB)
    for m in ser["KXMLBSPREAD"][1][0]["markets"]:
        m["custom_strike"] = {"baseball_team": "00000000-not-the-team"}
    pairs, skips = mlb_pairs(None, ser)
    assert not any(p.k.kind == "spreads" for p in pairs)
    assert any("team id differs" in s[2] for s in skips)


def test_kalshi_rule_text_must_agree_with_the_strike():
    ser = k_series(*MLB)
    m = ser["KXMLBTOTAL"][1][0]["markets"][0]
    m["rules_primary"] = m["rules_primary"].replace("2.5", "3.5")
    kc, skips = xvenue.kalshi_sport_claims("mlb", {"KXMLBTOTAL": ser["KXMLBTOTAL"]})
    assert m["ticker"] not in {c.ticker for c in kc}
    assert "kalshi total strike and rule text disagree" in skips


def test_sub_game_period_on_kalshi_is_never_full_game():
    ser = k_series(*MLB)
    m = ser["KXMLBTOTAL"][1][0]["markets"][0]
    m["rules_primary"] = m["rules_primary"].replace("in the Chicago", "in the first 5 innings of the Chicago")
    kc, skips = xvenue.kalshi_sport_claims("mlb", {"KXMLBTOTAL": ser["KXMLBTOTAL"]})
    assert m["ticker"] not in {c.ticker for c in kc}
    assert "kalshi market is not full game" in skips


def test_political_series_and_events_are_never_read():
    ser = k_series(*MLB)
    s, evs = ser["KXMLBGAME"]
    ser["KXMLBGAME"] = (dict(s, category="Politics"), evs)
    kc, skips = xvenue.kalshi_sport_claims("mlb", {"KXMLBGAME": ser["KXMLBGAME"]})
    assert kc == [] and any("blocked" in k for k in skips)
    pm = copy.deepcopy(load("pm_events_mlb.json")["events"])
    pm[0]["tags"].append({"slug": "us-pol", "label": "US Politics"})
    claims, pskips = xvenue.pm_sport_claims(pm, "mlb", NOW)
    assert claims == [] and "polymarket event has a political tag" in pskips
    assert not (xvenue.ALLOWED_SERIES & {"KXPRES", "KXFED", "KXFEDDECISION"})


def test_polymarket_doubleheader_is_skipped():
    pm = copy.deepcopy(load("pm_events_mlb.json")["events"])
    pm[0]["slug"] += "-dh2"
    claims, skips = xvenue.pm_sport_claims(pm, "mlb", NOW)
    assert claims == [] and any("doubleheader" in k for k in skips)


# --------------------------------------------------------------------------
# weather
# --------------------------------------------------------------------------

def test_weather_matches_same_station_date_and_band_only():
    pm = {"weather": load("pm_events_weather.json")["events"]}
    pairs, skips, n_pm, n_k = xvenue.build_pairs(pm, k_series("KXHIGHNY"), NOW)
    got = by_key(pairs)
    assert got[("tc-temp-nychigh-2026-09-23-gte67lt68f", "KXHIGHNY-26SEP23-B67.5")] == "same"
    assert got[("tc-temp-nychigh-2026-09-23-lt65f", "KXHIGHNY-26SEP23-T65")] == "same"
    assert got[("tc-temp-nychigh-2026-09-23-gte73f", "KXHIGHNY-26SEP23-T72")] == "same"
    assert len(pairs) == 6 and all(p.pm.station == "KNYC" == p.k.station for p in pairs)
    assert all("Weather Company" in xvenue.pair_row(p)["risk"] for p in pairs)


def test_weather_different_station_or_band_does_not_match():
    ev = copy.deepcopy(load("pm_events_weather.json")["events"])
    for m in ev[0]["markets"]:
        m["description"] = m["description"].replace("(KNYC)", "(KLGA)")
    pairs, skips, _, _ = xvenue.build_pairs({"weather": ev}, k_series("KXHIGHNY"), NOW)
    assert pairs == [] and any("station" in s[2] for s in skips)
    ser = k_series("KXHIGHNY")
    for m in ser["KXHIGHNY"][1][0]["markets"]:
        if m["ticker"].endswith("B67.5"):
            m.update(floor_strike=67, cap_strike=69, yes_sub_title="67° to 69°")
    pairs, skips, _, _ = xvenue.build_pairs({"weather": load("pm_events_weather.json")["events"]},
                                            ser, NOW)
    assert "tc-temp-nychigh-2026-09-23-gte67lt68f" not in {p.pm.slug for p in pairs}
    assert any("bands differ" in s[2] for s in skips)


# --------------------------------------------------------------------------
# the arithmetic
# --------------------------------------------------------------------------

def _pair(relation, pm_bid, pm_ask, k_bid, k_ask, mult=1.0):
    p = xvenue.PClaim("pm", "ev", "mlb", "totals", None, 8.5, frozenset(), "2026-09-23", None,
                      pm_bid, pm_ask, 0.07, "Over 8.5")
    k = xvenue.KClaim("K", "g", "mlb", "totals", None, 8.5, frozenset(), "2026-09-23", None,
                      k_bid, k_ask, 100.0, 100.0, "quadratic", mult)
    return xvenue.Pair(p, k, relation)


def test_same_proposition_hedge_is_yes_here_no_there():
    pair = _pair("same", 0.40, 0.42, 0.50, 0.51)
    legs = xvenue.legs(pair)
    assert legs["pm_yes"] == {"pm_side": "YES", "pm_px": 0.42, "k_side": "NO", "k_px": 0.5,
                              "k_size": 100.0}
    e = xvenue.price_pair(pair)["edge_pm_yes"]
    assert e == pytest.approx(1 - 0.42 - 0.50 - fee_per_share(0.42, 0.07)
                              - kalshi.taker_fee_per_contract(0.50), abs=1e-5)
    assert e > 0


def test_complement_hedge_is_yes_on_both():
    legs = xvenue.legs(_pair("complement", 0.40, 0.42, 0.50, 0.51))
    assert (legs["pm_yes"]["k_side"], legs["pm_yes"]["k_px"]) == ("YES", 0.51)
    assert (legs["pm_no"]["k_side"], legs["pm_no"]["k_px"]) == ("NO", 0.5)


def test_conservative_edge_uses_a_full_kalshi_fee():
    row = xvenue.price_pair(_pair("same", 0.40, 0.42, 0.50, 0.51, mult=0.5))
    assert row["edge_pm_yes_m1"] < row["edge_pm_yes"]


def test_walk_stops_where_the_next_set_loses_and_counts_whole_sets():
    pm_asks = [(0.40, 10.5), (0.47, 100)]
    k_asks = [(0.50, 5), (0.52, 100)]
    res = xvenue.walk(pm_asks, k_asks, 0.07, 1.0)
    # 0.40+0.50 and 0.40+0.52 clear fees; 0.47+0.52 does not.
    assert res["sets"] == 10
    assert res["profit"] == pytest.approx(10 - res["cost"] - res["fees"])
    assert res["profit"] > 0


def test_polymarket_book_becomes_buy_ladders():
    lad = xvenue.pm_ladders(load("pm_book.json"))
    assert lad["yes_asks"][0] == (0.46, 74.0)
    assert lad["no_asks"][0][0] == pytest.approx(1 - 0.455)
