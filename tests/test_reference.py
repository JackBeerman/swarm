"""
reference.py: team tables, de-vig, which side YES pays on, matching, budget.

Two kinds of fixture, kept apart on purpose:
  * EXCHANGE markets are trimmed copies of live events.list responses
    (2026-09-22): Padres-Dodgers and Falcons-Packers, field names and
    values as sent.
  * ODDS payloads follow The Odds API v4 documentation's example response
    (Cowboys at Buccaneers, 2021-09-10, American odds). That shape is from
    the docs, NOT from a live call -- no key existed when this was written.
    Where a test needs more than the example's single bookmaker, the same
    block is repeated under other book keys.
"""

import asyncio
import copy
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

import reference as ref
from fees import fee_usd

NOW = datetime(2026, 9, 22, 20, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# teams
# --------------------------------------------------------------------------

def test_tables_are_complete_and_one_to_one():
    assert len(ref.MLB_TEAMS) == 30 and len(set(ref.MLB_TEAMS.values())) == 30
    assert len(ref.NFL_TEAMS) == 32 and len(set(ref.NFL_TEAMS.values())) == 32


# The exchange's own (abbreviation, name) pairs, copied from live events.
EXCHANGE_NAMES = {
    "mlb": {"az": "Arizona Diamondbacks", "ath": "Athletics", "stl": "St. Louis Cardinals",
            "cws": "Chicago White Sox", "wsh": "Washington Nationals", "sd": "San Diego Padres"},
    "nfl": {"jax": "Jacksonville Jaguars", "was": "Washington Commanders",
            "lar": "Los Angeles Rams", "lac": "Los Angeles Chargers", "sf": "San Francisco 49ers"},
}


@pytest.mark.parametrize("sport", ["mlb", "nfl"])
def test_exchange_names_map_to_the_table(sport):
    for abbr, name in EXCHANGE_NAMES[sport].items():
        assert ref.book_team(sport, name) == ref.TEAMS[sport][abbr]


def test_book_spellings_and_non_teams():
    assert ref.book_team("mlb", "Oakland Athletics") == "Athletics"
    assert ref.book_team("mlb", "St Louis Cardinals") == "St. Louis Cardinals"
    assert ref.book_team("mlb", "Draw") is None
    assert ref.book_team("nfl", "Los Angeles") is None          # never guess which LA
    assert ref.book_team("nfl", "Los Angeles Dodgers") is None  # wrong sport


# --------------------------------------------------------------------------
# de-vig
# --------------------------------------------------------------------------

def test_american_odds_from_the_docs_example():
    assert ref.to_decimal(240, "american") == pytest.approx(3.40)
    assert ref.to_decimal(-303, "american") == pytest.approx(1.330, abs=1e-3)
    assert ref.to_decimal(1.87, "decimal") == 1.87
    with pytest.raises(ValueError):
        ref.to_decimal(50, "american")


@pytest.mark.parametrize("odds", [(1.87, 2.03), (1.25, 4.2), (1.10, 7.5)])
def test_methods_sum_to_one_and_order_as_expected(odds):
    d = ref.devig(list(odds))
    for probs in d.values():
        assert sum(probs) == pytest.approx(1.0, abs=1e-9)
    # Power takes the most margin off the longshot, multiplicative the least.
    assert d["power"][1] <= d["shin"][1] <= d["multiplicative"][1]


def test_shin_is_equal_margin_subtraction_for_two_outcomes():
    q = [1 / 1.25, 1 / 4.2]
    margin = sum(q) - 1
    shin = ref.devig([1.25, 4.2])["shin"]
    assert shin[0] == pytest.approx(q[0] - margin / 2, abs=1e-6)


def test_no_margin_means_no_change():
    for probs in ref.devig([2.0, 2.0]).values():
        assert probs == pytest.approx([0.5, 0.5])


def test_bad_odds_raise():
    with pytest.raises(ValueError):
        ref.devig([1.0, 3.0])


# --------------------------------------------------------------------------
# the exchange side
# --------------------------------------------------------------------------

def _team(tid, abbr, name, alias, safe):
    return {"id": tid, "name": name, "abbreviation": abbr, "league": "mlb",
            "alias": alias, "safeName": safe}


SD_LAD = {
    "slug": "mlb-sd-lad-2026-09-22", "startTime": "2026-09-23T02:10:00Z",
    "period": "NS", "live": False,
    "teams": [_team(3022, "sd", "San Diego Padres", "San Diego Padres", "Padres"),
              _team(3014, "lad", "Los Angeles Dodgers", "Los Angeles Dodgers", "Dodgers")],
}


def _side(slug, desc, long_, tid, abbr):
    return {"identifier": slug, "description": desc, "long": long_, "teamId": tid,
            "team": None if tid is None else {"id": tid, "abbreviation": abbr}}


def _market(slug, smt, line, sides, desc, title, bid, ask):
    return {"slug": slug, "sportsMarketType": smt, "line": line, "marketSides": sides,
            "description": desc, "title": title, "active": True, "closed": False,
            "status": "MARKET_STATUS_OPEN", "feeCoefficient": 0.0695,
            "bestBidQuote": {"value": bid, "currency": "USD"},
            "bestAskQuote": {"value": ask, "currency": "USD"}}


S = "asc-mlb-sd-lad-2026-09-22"
MLB_ML = _market(
    "aec-mlb-sd-lad-2026-09-22", "baseball_team_full_game_winner", None,
    [_side("aec-mlb-sd-lad-2026-09-22", "San Diego Padres", True, 3022, "sd"),
     _side("aec-mlb-sd-lad-2026-09-22", "Los Angeles Dodgers", False, 3014, "lad")],
    "This market will settle to the winner of the San Diego Padres vs Los Angeles Dodgers "
    "MLB game scheduled for 2026-09-22 at 10:10PM ET.",
    "San Diego Padres vs Los Angeles Dodgers", "0.4550", "0.4600")
MLB_POS = _market(
    f"{S}-pos-1pt5", "baseball_team_full_game_spread", 1.5,
    [_side(f"{S}-pos-1pt5", "+1.50", True, 3022, "sd"),
     _side(f"{S}-pos-1pt5", "-1.50", False, 3014, "lad")],
    "This market will settle to Yes if the San Diego Padres cover a +1.5 run spread in the "
    "San Diego Padres vs Los Angeles Dodgers MLB game scheduled for 2026-09-22 at 10:10PM ET.",
    "Los Angeles Dodgers wins by over 1.5 runs", "0.6300", "0.6400")
MLB_NEG = _market(
    f"{S}-neg-1pt5", "baseball_team_full_game_spread", -1.5,
    [_side(f"{S}-neg-1pt5", "-1.50", True, 3022, "sd"),
     _side(f"{S}-neg-1pt5", "+1.50", False, 3014, "lad")],
    "This market will settle to Yes if the San Diego Padres cover a -1.5 run spread in the "
    "San Diego Padres vs Los Angeles Dodgers MLB game scheduled for 2026-09-22 at 10:10PM ET.",
    "San Diego Padres wins by over 1.5 runs", "0.3350", "0.3400")
MLB_TOTAL = _market(
    "tsc-mlb-sd-lad-2026-09-22-8pt5", "baseball_team_full_game_total", 8.5,
    [_side("tsc-mlb-sd-lad-2026-09-22-8pt5", "Over", True, None, None),
     _side("tsc-mlb-sd-lad-2026-09-22-8pt5", "Under", False, None, None)],
    "This market will settle to Yes if the San Diego Padres and Los Angeles Dodgers combine "
    "for over 8.5 runs in the San Diego Padres vs Los Angeles Dodgers MLB game.",
    "Over 8.5 total runs", "0.4900", "0.4950")
MLB_F5 = _market(
    "tsc-mlb-sd-lad-2026-09-22-f5-4pt5", "baseball_team_first_five_total", 4.5,
    [_side("tsc-mlb-sd-lad-2026-09-22-f5-4pt5", "Over", True, None, None),
     _side("tsc-mlb-sd-lad-2026-09-22-f5-4pt5", "Under", False, None, None)],
    "This market will settle to Yes if the San Diego Padres and Los Angeles Dodgers combine "
    "for over 4.5 runs in the first 5 innings.", "Over 4.5 total runs in first 5 innings",
    "0.5000", "0.5100")
MLB_TEAM_TOTAL = _market(
    "tsc-mlb-sd-lad-2026-09-22-tt-sd-3pt5", "baseball_team_total_runs", 3.5,
    [_side("tsc-mlb-sd-lad-2026-09-22-tt-sd-3pt5", "Yes", True, 3022, "sd"),
     _side("tsc-mlb-sd-lad-2026-09-22-tt-sd-3pt5", "No", False, 3022, "sd")],
    "", "San Diego Padres to have Over 3.5 runs", "0.5400", "0.5600")


def test_moneyline_yes_is_the_long_team():
    c = ref.parse_market(MLB_ML, SD_LAD, "mlb")
    assert (c.kind, c.period, c.team, c.line) == ("h2h", "full", "San Diego Padres", None)
    assert (c.bid, c.ask, c.fee_rate) == (0.455, 0.46, 0.0695)
    assert c.teams == {"San Diego Padres", "Los Angeles Dodgers"}


def test_underdog_spread_yes_is_not_the_title():
    """Live 2026-09-22: titled 'Dodgers wins by over 1.5' but YES = Padres +1.5."""
    c = ref.parse_market(MLB_POS, SD_LAD, "mlb")
    assert (c.team, c.line, c.text_check) == ("San Diego Padres", 1.5, "agree")
    assert "Dodgers" in MLB_POS["title"]
    assert c.label == "San Diego Padres +1.5"


def test_favourite_spread_and_totals():
    c = ref.parse_market(MLB_NEG, SD_LAD, "mlb")
    assert (c.team, c.line) == ("San Diego Padres", -1.5)
    t = ref.parse_market(MLB_TOTAL, SD_LAD, "mlb")
    assert (t.kind, t.team, t.line, t.label) == ("totals", None, 8.5, "Over 8.5")
    f5 = ref.parse_market(MLB_F5, SD_LAD, "mlb")
    assert (f5.kind, f5.period, f5.line) == ("totals", "f5", 4.5)


def _skip_reason(market, event=SD_LAD, sport="mlb", **kw):
    with pytest.raises(ref.Skip) as e:
        ref.parse_market(market, event, sport, **kw)
    return str(e.value)


def test_uncovered_types_are_skipped():
    assert "not covered" in _skip_reason(MLB_TEAM_TOTAL)


def test_any_disagreeing_structured_field_skips():
    m = copy.deepcopy(MLB_POS)
    m["line"] = -1.5                                    # sign vs slug and side description
    assert "disagree" in _skip_reason(m)
    m = copy.deepcopy(MLB_POS)
    m["marketSides"][0]["team"]["abbreviation"] = "lad"
    assert "disagree" in _skip_reason(m)
    m = copy.deepcopy(MLB_POS)
    m["marketSides"][1]["long"] = True                  # two long sides
    assert "one long" in _skip_reason(m)
    m = copy.deepcopy(MLB_POS)
    m["slug"] = f"{S}-pos-2pt5"
    assert _skip_reason(m)
    m = copy.deepcopy(MLB_TOTAL)
    m["marketSides"][0]["description"] = "Under"
    assert "Over" in _skip_reason(m)


def test_description_naming_the_other_team_is_a_conflict():
    m = copy.deepcopy(MLB_POS)
    m["description"] = m["description"].replace("San Diego Padres cover a +1.5",
                                                "Los Angeles Dodgers cover a -1.5")
    assert "text_conflict" in _skip_reason(m)


def test_unknown_or_renamed_team_skips_the_event():
    ev = copy.deepcopy(SD_LAD)
    ev["teams"][0]["abbreviation"] = "sdp"
    assert "unknown team" in _skip_reason(MLB_ML, ev)
    ev = copy.deepcopy(SD_LAD)
    ev["teams"][0]["name"] = "San Diego Friars"
    assert "disagrees with table" in _skip_reason(MLB_ML, ev)


# NFL, live 2026-09-22: the "pos" spread's DESCRIPTION names the other side.
ATL_GB = {
    "slug": "nfl-atl-gb-2026-09-24", "startTime": "2026-09-25T00:15:00Z", "live": False,
    "teams": [{"id": 49, "name": "Atlanta Falcons", "abbreviation": "atl", "league": "nfl",
               "alias": "Falcons", "safeName": "ATL Falcons"},
              {"id": 59, "name": "Green Bay Packers", "abbreviation": "gb", "league": "nfl",
               "alias": "Packers", "safeName": "GB Packers"}],
}
N = "asc-nfl-atl-gb-2026-09-24"
NFL_POS = _market(
    f"{N}-pos-3pt5", "football_team_full_game_spread", 3.5,
    [_side(f"{N}-pos-3pt5", "+3.50", True, 49, "atl"),
     _side(f"{N}-pos-3pt5", "-3.50", False, 59, "gb")],
    "This market will settle to Yes if Green Bay Packers wins by more than 3.5 in the Green "
    "Bay Packers vs Atlanta Falcons professional football game scheduled for Sep 24, 2026.",
    "Green Bay Packers wins by over 3.5 points", "0.4350", "0.4400")
NFL_NEG = _market(
    f"{N}-neg-1pt5", "football_team_full_game_spread", -1.5,
    [_side(f"{N}-neg-1pt5", "-1.50", True, 49, "atl"),
     _side(f"{N}-neg-1pt5", "+1.50", False, 59, "gb")],
    "This market will settle to Yes if Atlanta Falcons wins by more than 1.5 in the Atlanta "
    "Falcons vs Green Bay Packers professional football game scheduled for Sep 24, 2026.",
    "Atlanta Falcons wins by over 1.5 points", "0.2600", "0.2650")
NFL_ML = _market(
    "aec-nfl-atl-gb-2026-09-24", "football_team_full_game_winner", None,
    [_side("aec-nfl-atl-gb-2026-09-24", "Falcons", True, 49, "atl"),
     _side("aec-nfl-atl-gb-2026-09-24", "Packers", False, 59, "gb")],
    "This market will settle to the winner of the Atlanta Falcons vs Green Bay Packers game.",
    "ATL Falcons vs GB Packers", "0.2800", "0.2850")


def test_nfl_pos_spread_conflict_is_skipped_by_default():
    assert "text_conflict" in _skip_reason(NFL_POS, ATL_GB, "nfl")
    c = ref.parse_market(NFL_POS, ATL_GB, "nfl", trust_sides=True)
    assert (c.team, c.line, c.text_check) == ("Atlanta Falcons", 3.5, "conflict")


def test_nfl_wins_by_description_agrees_on_the_favourite_side():
    c = ref.parse_market(NFL_NEG, ATL_GB, "nfl")
    assert (c.team, c.line, c.text_check) == ("Atlanta Falcons", -1.5, "agree")
    ml = ref.parse_market(NFL_ML, ATL_GB, "nfl")      # side described by alias "Falcons"
    assert ml.team == "Atlanta Falcons"


def test_claims_skip_started_games_and_f5_on_request():
    ev = {**SD_LAD, "markets": [MLB_ML, MLB_F5, MLB_TEAM_TOTAL, {"slug": "astatc-x"}]}
    claims, skips, seen = ref.claims_from_events([ev], "mlb", NOW, include_f5=False)
    assert seen == 3 and [c.kind for c in claims] == ["h2h"]
    assert skips["first-five (needs --f5)"] == 1
    late = NOW + timedelta(hours=7)
    claims, skips, _ = ref.claims_from_events([ev], "mlb", late)
    assert not claims and skips["game started"] == 2


# --------------------------------------------------------------------------
# the sportsbook side: the docs example, verbatim, plus spreads/totals
# --------------------------------------------------------------------------

DOCS_EVENT = {
    "id": "bda33adca828c09dc3cac3a856aef176",
    "sport_key": "americanfootball_nfl",
    "commence_time": "2021-09-10T00:20:00Z",
    "home_team": "Tampa Bay Buccaneers",
    "away_team": "Dallas Cowboys",
    "bookmakers": [{
        "key": "unibet", "title": "Unibet", "last_update": "2021-06-10T13:33:18Z",
        "markets": [
            {"key": "h2h", "outcomes": [{"name": "Dallas Cowboys", "price": 240},
                                        {"name": "Tampa Bay Buccaneers", "price": -303}]},
            {"key": "spreads", "outcomes": [
                {"name": "Dallas Cowboys", "price": -109, "point": 6.5},
                {"name": "Tampa Bay Buccaneers", "price": -111, "point": -6.5}]},
            {"key": "totals", "outcomes": [{"name": "Over", "price": -110, "point": 45.5},
                                           {"name": "Under", "price": -110, "point": 45.5}]},
        ]}],
}

DAL_TB = {
    "slug": "nfl-dal-tb-2021-09-09", "startTime": "2021-09-10T00:20:00Z", "live": False,
    "teams": [{"id": 1, "name": "Dallas Cowboys", "abbreviation": "dal", "alias": "Cowboys"},
              {"id": 2, "name": "Tampa Bay Buccaneers", "abbreviation": "tb",
               "alias": "Buccaneers"}],
}
D = "asc-nfl-dal-tb-2021-09-09"
DAL_ML = _market("aec-nfl-dal-tb-2021-09-09", "football_team_full_game_winner", None,
                 [_side("x", "Cowboys", True, 1, "dal"), _side("x", "Buccaneers", False, 2, "tb")],
                 "", "", "0.2500", "0.2600")
DAL_SPREAD = _market(f"{D}-pos-6pt5", "football_team_full_game_spread", 6.5,
                     [_side("x", "+6.50", True, 1, "dal"), _side("x", "-6.50", False, 2, "tb")],
                     "", "", "0.4400", "0.4500")
DAL_TOTAL = _market("tsc-nfl-dal-tb-2021-09-09-total-45pt5", "football_team_full_game_total",
                    45.5, [_side("x", "Over", True, None, None),
                           _side("x", "Under", False, None, None)], "", "", "0.5000", "0.5100")
THEN = datetime(2021, 9, 9, 18, 0, tzinfo=timezone.utc)


def _with_books(*keys):
    ev = copy.deepcopy(DOCS_EVENT)
    block = ev["bookmakers"][0]
    ev["bookmakers"] = [{**copy.deepcopy(block), "key": k, "title": k} for k in keys]
    return [ev]


def _claims():
    ev = {**DAL_TB, "markets": [DAL_ML, DAL_SPREAD, DAL_TOTAL]}
    claims, skips, _ = ref.claims_from_events([ev], "nfl", THEN)
    assert not skips
    return claims


def test_docs_payload_parses():
    games, skips = ref.parse_odds([DOCS_EVENT], "nfl", odds_format="american")
    assert not skips and len(games) == 1
    g = games[0]
    assert g.teams == {"Dallas Cowboys", "Tampa Bay Buccaneers"}
    assert g.books["unibet"]["spreads"][0] == ("Dallas Cowboys", pytest.approx(1 + 100 / 109), 6.5)


def test_pinnacle_is_the_source_when_present():
    games, _ = ref.parse_odds(_with_books("unibet", "pinnacle"), "nfl", "american")
    rows, skips = ref.compare(_claims(), games, THEN)
    assert not skips and len(rows) == 3
    ml = next(r for r in rows if r["kind"] == "h2h")
    expect = ref.devig([3.40, 1 + 100 / 303])["power"][0]
    assert ml["fair_prob"] == pytest.approx(expect, abs=1e-5)
    assert ml["fair_source"] == "pinnacle" and ml["method"] == "power"
    assert json.loads(ml["books_used"]) == ["pinnacle", "unibet"]
    assert ml["yes_side"] == "Dallas Cowboys win"
    tot = next(r for r in rows if r["kind"] == "totals")
    assert tot["fair_prob"] == pytest.approx(0.5)


def test_consensus_needs_three_books():
    games, _ = ref.parse_odds(_with_books("unibet", "draftkings"), "nfl", "american")
    rows, skips = ref.compare(_claims(), games, THEN)
    assert not rows and sum(skips.values()) == 3
    games, _ = ref.parse_odds(_with_books("unibet", "draftkings", "fanduel"), "nfl", "american")
    rows, _ = ref.compare(_claims(), games, THEN)
    assert rows and all(r["fair_source"] == "consensus" and r["n_books"] == 3 for r in rows)


def test_line_must_match_exactly():
    m = copy.deepcopy(DAL_SPREAD)
    m.update(slug=f"{D}-pos-5pt5", line=5.5)
    m["marketSides"][0]["description"], m["marketSides"][1]["description"] = "+5.50", "-5.50"
    ev = {**DAL_TB, "markets": [m]}
    claims, _, _ = ref.claims_from_events([ev], "nfl", THEN)
    games, _ = ref.parse_odds(_with_books("pinnacle"), "nfl", "american")
    rows, skips = ref.compare(claims, games, THEN)
    assert not rows and skips == {"no book quotes this exact line": 1}


def test_two_book_games_in_the_window_are_ambiguous():
    two = _with_books("pinnacle") + _with_books("pinnacle")
    two[1]["id"] = "game-2"
    two[1]["commence_time"] = "2021-09-10T02:50:00Z"          # a doubleheader, 2.5 h later
    games, _ = ref.parse_odds(two, "nfl", "american")
    rows, skips = ref.compare(_claims(), games, THEN)
    assert not rows and skips == {"two book games in the window (doubleheader?)": 3}


def test_book_game_out_of_window_or_unknown_team_is_not_matched():
    far = _with_books("pinnacle")
    far[0]["commence_time"] = "2021-09-11T00:20:00Z"
    games, _ = ref.parse_odds(far, "nfl", "american")
    rows, skips = ref.compare(_claims(), games, THEN)
    assert not rows and skips == {"no book game for this pair and time": 3}
    odd = _with_books("pinnacle")
    odd[0]["home_team"] = "Tampa Bay"
    games, skips = ref.parse_odds(odd, "nfl", "american")
    assert not games and skips


def test_three_way_h2h_is_not_used():
    ev = _with_books("pinnacle")
    ev[0]["bookmakers"][0]["markets"][0]["outcomes"].append({"name": "Draw", "price": 5000})
    games, _ = ref.parse_odds(ev, "nfl", "american")
    rows, skips = ref.compare(_claims()[:1], games, THEN)
    assert not rows and skips == {"no book quotes this exact line": 1}


def test_edge_is_net_of_the_fee():
    ey, en = ref.edges(0.62, 0.53, 0.55, 0.07)
    assert ey == pytest.approx(0.62 - 0.55 - fee_usd(0.55, 10, 0.07) / 10)
    assert en == pytest.approx(0.53 - 0.62 - fee_usd(0.53, 10, 0.07) / 10)
    assert ref.edges(0.5, None, None, 0.07) == (None, None)


# --------------------------------------------------------------------------
# budget, inertness, the HTTP call
# --------------------------------------------------------------------------

def test_cost_estimate_follows_the_docs():
    assert ref.estimate_cost(3, 10) == 3
    assert ref.estimate_cost(3, 11) == 6
    assert ref.estimate_cost(1, 0) == 1


def _mem():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(ref._SCHEMA)
    return conn


def test_budget_refuses_past_the_caps_and_reserve():
    conn = _mem()
    b = ref.Budget(conn, monthly=20, daily=6, reserve=25)
    assert b.refusal(3, NOW) is None
    b.record("mlb", "/x", 3, None, NOW)
    b.record("mlb", "/x", 3, None, NOW)
    assert "daily" in b.refusal(3, NOW)
    assert b.refusal(3, NOW + timedelta(days=1)) is None
    resp = httpx.Response(200, headers={"x-requests-remaining": "26", "x-requests-last": "3",
                                        "x-requests-used": "474"})
    b.record("mlb", "/x", 3, resp, NOW + timedelta(days=2))
    assert "reserve" in b.refusal(3, NOW + timedelta(days=2))
    assert b.spent_since(NOW - timedelta(days=1)) == 9


def test_no_key_is_inert_and_touches_nothing(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(ref, "api_key", lambda: None)

    async def boom(*a, **k):
        raise AssertionError("must not call the exchange without a key")
    monkeypatch.setattr(ref, "fetch_events", boom)
    db = tmp_path / "reference.db"
    assert asyncio.run(ref.record("mlb", db=str(db))) == 0
    out = capsys.readouterr().out
    assert "INERT" in out and ref.ENV_KEY in out and ref.SIGNUP_URL in out
    assert not db.exists()


@respx.mock
def test_odds_get_records_credits_and_explains_401():
    conn = _mem()
    b = ref.Budget(conn, monthly=450, daily=15)
    route = respx.get(f"{ref.API_BASE}/sports/baseball_mlb/odds").mock(return_value=httpx.Response(
        200, json=[], headers={"x-requests-remaining": "497", "x-requests-last": "3",
                               "x-requests-used": "3"}))

    async def go():
        async with httpx.AsyncClient() as c:
            return await ref.odds_get(c, "k", b, "mlb", "/sports/baseball_mlb/odds",
                                      {"markets": "h2h,spreads,totals"}, 3, NOW)
    assert asyncio.run(go()) == []
    assert route.calls[0].request.url.params["markets"] == "h2h,spreads,totals"
    row = conn.execute("SELECT * FROM odds_calls").fetchone()
    assert (row["credits_last"], row["credits_remaining"], row["status"]) == (3, 497, 200)
    route.mock(return_value=httpx.Response(401, json={"message": "bad key"}))
    with pytest.raises(ref.OddsError, match="rejected the key") as e:
        asyncio.run(go())
    assert "k" not in str(e.value).split()


def test_budget_refusal_makes_no_request():
    conn = _mem()
    b = ref.Budget(conn, monthly=2, daily=2)

    async def go():
        async with httpx.AsyncClient() as c:
            return await ref.odds_get(c, "k", b, "mlb", "/sports/x/odds", {}, 3, NOW)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(url__startswith=ref.API_BASE)
        with pytest.raises(ref.OddsError, match="budget"):
            asyncio.run(go())
        assert not route.called


# --------------------------------------------------------------------------
# store and score
# --------------------------------------------------------------------------

def test_store_then_score_counts_by_event(tmp_path):
    games, _ = ref.parse_odds(_with_books("pinnacle"), "nfl", "american")
    rows, _ = ref.compare(_claims(), games, THEN)
    conn = ref.connect(str(tmp_path / "r.db"))
    ref.store(conn, rows)
    ref.store(conn, rows)                                   # same snapshot: ignored
    assert conn.execute("SELECT COUNT(*) FROM comparisons").fetchone()[0] == 3
    conn.execute("UPDATE comparisons SET resolved_outcome='1'")
    s = ref.score_rows(conn.execute("SELECT * FROM comparisons").fetchall())
    assert s["events"] == 1 and s["markets"] == 3
    assert sum(s["closer_by_event"].values()) == 1
    assert set(s["brier_by_method"]) == set(ref.METHODS)
    conn.close()
