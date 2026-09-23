"""
Tests for the SDK adapter layer.

The fixtures below are cut down from responses captured off the live
gateway on 2026-09-19, NOT from the SDK's TypedDicts. The stubs in
polymarket_us/types/ are `total=False`, so they assert nothing at runtime,
and they declare three fields (`volume`, `liquidity`, `endTime`) that the
gateway never sends. An earlier version of this file was written from
those stubs; it passed while the adapter returned None for every quote.
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest

os.environ.setdefault("TYPESAFE_API_KEY", "test-key")

from adapters import (  # noqa: E402
    PriceTracker,
    amount,
    derive_notionals,
    interleave_by_event,
    iter_event_markets,
    normalize_bbo,
    normalize_market,
)
from swarm import JevTriage  # noqa: E402

# Everything the quote endpoint returns is wrapped in `marketData`. Reading
# `bbo["bestBid"]` straight off the top level yields None for every market,
# which reads downstream as `no_quote` and looks like a selective gate.
RAW_BBO = {
    "marketData": {
        "marketSlug": "fed-cuts-october",
        "bestBid": {"value": "0.53", "currency": "USD"},
        "bestAsk": {"value": "0.55", "currency": "USD"},
        "bidDepth": 4200,
        "askDepth": 3800,
        "lastTradePx": {"value": "0.54", "currency": "USD"},
        # Share counts, sent as decimal strings. There is no dollar volume
        # field anywhere in the response.
        "sharesTraded": "120000.0000",
        "openInterest": "88000.0000",
        "bidShares": "1100000",
        "askShares": "280000",
        "state": "MARKET_STATE_OPEN",
    }
}

# `question` is the event-level proposition, `title` the leg being priced.
# Both are present on 100% of open markets. Neither `volume` nor
# `liquidity` appears at all.
RAW_MARKET = {
    "id": "991",
    "slug": "fed-cuts-october",
    "question": "Will the Fed cut rates in October?",
    "title": "Yes",
    "titleShort": "Yes",
    "description": "Resolves YES if the FOMC lowers the target range.",
    "active": True,
    "closed": False,
    "status": "MARKET_STATUS_OPEN",
    "endDate": "2026-10-29T18:00:00Z",
    "startDate": "2026-10-01T00:00:00Z",
    "outcomes": ["Yes", "No"],
    "outcomePrices": ["0.5300", "0.4700"],
    "eventSlug": "fomc-october",
}

# Events carry `endDate`. `endTime` is declared by the SDK stub and is
# never sent -- it was 0/50 across sampled events.
RAW_EVENT = {
    "id": "77",
    "slug": "fomc-october",
    "title": "FOMC October Decision",
    "description": "Federal Open Market Committee, October meeting.",
    "startTime": "2026-10-01T00:00:00Z",
    "endDate": "2026-10-29T18:00:00Z",
    "active": True,
    "closed": False,
    "markets": [RAW_MARKET],
    "tags": [{"id": "3", "slug": "economics", "label": "Economics"}],
}

# A resolved market. `active` stays True, so only `closed`/`status`
# distinguish it -- this is the shape that made {"active": True} return
# nothing but settled markets.
RESOLVED_MARKET = {
    **RAW_MARKET,
    "slug": "fed-cuts-september",
    "active": True,
    "closed": True,
    "status": "MARKET_STATUS_RESOLVED",
}


def test_amount_parses_decimal_strings():
    assert amount({"value": "0.55", "currency": "USD"}) == 0.55
    assert amount({"value": "1", "currency": "USD"}) == 1.0
    assert amount(0.42) == 0.42
    assert amount(None) is None
    assert amount({"currency": "USD"}) is None
    assert amount({"value": "abc"}) is None


def test_normalize_bbo_reads_through_the_marketdata_envelope():
    b = normalize_bbo(RAW_BBO)
    assert b["bid"] == 0.53
    assert b["ask"] == 0.55
    assert b["bid_depth"] == 4200
    assert b["last"] == 0.54
    assert isinstance(b["bid"], float)
    assert b["shares_traded"] == 120_000.0
    assert b["open_interest"] == 88_000.0


def test_normalize_bbo_ignores_a_top_level_shape():
    """
    The envelope is what the gateway sends, but tolerate an unwrapped dict
    rather than silently returning None if it ever changes back.
    """
    flat = dict(RAW_BBO["marketData"])
    assert normalize_bbo(flat)["bid"] == 0.53


def test_normalize_bbo_survives_empty():
    assert normalize_bbo(None)["bid"] is None
    assert normalize_bbo({})["ask"] is None
    assert normalize_bbo({"marketData": {}})["bid"] is None


def test_normalize_market_maps_question_and_outcome_separately():
    m = normalize_market(RAW_MARKET, RAW_EVENT)
    # `question` is the proposition; `title` is the leg. Swapping them
    # makes every question in questions.py read the wrong string.
    assert m["question"] == "Will the Fed cut rates in October?"
    assert m["outcome"] == "Yes"
    assert m["tags"] == ["economics"]


def test_normalize_market_leaves_volume_unset():
    """No volume or liquidity field exists; it is derived from the quote."""
    m = normalize_market(RAW_MARKET, RAW_EVENT)
    assert m["volume_usd"] is None
    assert m["liquidity_usd"] is None


def test_close_time_prefers_the_markets_own_end_date():
    """
    A leg can resolve months after its parent event ends, and that gap is
    exactly what the "capital parked too long" check is measuring.
    """
    late = {**RAW_MARKET, "endDate": "2027-02-01T23:59:00Z"}
    m = normalize_market(late, RAW_EVENT)
    assert m["closes_at"] == "2027-02-01T23:59:00Z"

    # Priority: market endDate, then event endDate, then the stub's
    # `endTime` purely as tolerance -- the gateway never sends it, so
    # reading it FIRST (as this once did) yields None on every market.
    no_date = {k: v for k, v in RAW_MARKET.items() if k != "endDate"}
    assert normalize_market(no_date, RAW_EVENT)["closes_at"] == \
        "2026-10-29T18:00:00Z"
    stub_only = {"slug": "x", "endTime": "2026-01-01T00:00:00Z"}
    assert normalize_market(no_date, stub_only)["closes_at"] == \
        "2026-01-01T00:00:00Z"


def test_normalize_market_without_event_still_has_a_close_time():
    m = normalize_market(RAW_MARKET)
    assert m["closes_at"] == "2026-10-29T18:00:00Z"
    assert m["question"] == "Will the Fed cut rates in October?"


def test_derive_notionals_converts_share_counts_to_dollars():
    b = normalize_bbo(RAW_BBO)
    n = derive_notionals(b)
    # 120_000 shares at a 0.54 mid
    assert n["volume_usd"] == pytest.approx(64_800.0)
    assert n["liquidity_usd"] == pytest.approx(47_520.0)


def test_derive_notionals_is_none_without_a_quote():
    assert derive_notionals(normalize_bbo(None))["volume_usd"] is None


def test_iter_event_markets_skips_resolved_markets_that_stay_active():
    """
    The regression that produced zero candidates: `active` remains True on
    a settled market, so it cannot carry this filter alone.
    """
    ev = {**RAW_EVENT, "markets": [RAW_MARKET, RESOLVED_MARKET]}
    pairs = iter_event_markets({"events": [ev]})
    assert [m["slug"] for m, _ in pairs] == ["fed-cuts-october"]


def test_iter_event_markets_takes_no_volume_floor():
    """
    The old signature filtered on a field that does not exist, so any
    nonzero floor emptied the list. Passing one must now be a hard error
    rather than a silent zero.
    """
    with pytest.raises(TypeError):
        iter_event_markets({"events": [RAW_EVENT]}, min_volume=50_000)


def test_iter_event_markets_skips_closed():
    closed = {**RAW_EVENT, "markets": [{**RAW_MARKET, "closed": True}]}
    assert iter_event_markets({"events": [closed]}) == []


# --------------------------------------------------------------------------
# the regression this layer exists to prevent
# --------------------------------------------------------------------------

def test_raw_sdk_shapes_produce_a_hollow_state():
    """
    Passing raw objects through yields a state with no prices and no
    volume, and Jev would return confident probabilities about nothing.

    Note what makes this *worse* than it used to be: the raw market really
    does carry `question`, so the bad state is no longer obviously empty --
    it reads like a normal market with a missing quote. The prices are the
    tell, and only the adapter supplies them.
    """
    bad = JevTriage.build_state(RAW_MARKET, RAW_BBO)
    assert bad["best_bid"] is None
    assert bad["best_ask"] is None
    assert bad["volume_usd"] is None
    assert bad["spread"] is None
    assert bad["question"] is not None, "this is the part that looks fine"

    good = JevTriage.build_state(
        normalize_market(RAW_MARKET, RAW_EVENT), normalize_bbo(RAW_BBO)
    )
    assert good["best_bid"] == 0.53
    assert good["spread"] == pytest.approx(0.02)
    assert good["volume_usd"] == pytest.approx(64_800.0)
    assert good["question"].startswith("Will the Fed")


def test_tier1_state_excludes_market_internals():
    """
    Tier 1 judges market STRUCTURE, not market state. Prices, volume and
    spread are computed in code and must never be sent to Jev -- asking a
    model to compare two floats is both wasteful and less reliable than
    an `if`.
    """
    full = JevTriage.build_state(
        normalize_market(RAW_MARKET, RAW_EVENT), normalize_bbo(RAW_BBO)
    )
    # build_state keeps numerics for structural_filter()...
    assert full["best_bid"] == 0.53
    assert full["volume_usd"] == pytest.approx(64_800.0)
    assert full["hours_to_close"] is not None

    # ...but the payload actually sent to the model does not.
    sent = JevTriage._model_state(full)
    for forbidden in (
        "best_bid", "best_ask", "spread", "volume_usd",
        "liquidity_usd", "bid_depth", "hours_to_close",
    ):
        assert forbidden not in sent, f"{forbidden} must not reach the model"
    assert set(sent) <= {"question", "description", "outcome", "event", "tags"}
    assert sent["question"].startswith("Will the Fed")


# --------------------------------------------------------------------------
# PriceTracker
# --------------------------------------------------------------------------

def test_tracker_computes_change_over_window():
    t = PriceTracker()
    now = 1_000_000.0
    t.observe("m", 0.50, ts=now - 3000)
    t.observe("m", 0.54, ts=now - 1800)
    t.observe("m", 0.61, ts=now)
    assert t.change("m", 3600) == pytest.approx(0.11)
    assert t.change("m", 2000) == pytest.approx(0.07)


def test_tracker_returns_none_without_history():
    t = PriceTracker()
    assert t.change("m", 3600) is None
    t.observe("m", 0.5)
    assert t.change("m", 3600) is None, "a single point is not a delta"


def test_tracker_coverage_reports_thin_history():
    """
    PriceTracker survives the Tier 1 redesign because Tier 2 still needs
    price movement to hand a gatherer. It is simply no longer fed to Jev.
    """
    t = PriceTracker()
    now = 1_000_000.0
    t.observe("fed-cuts-october", 0.50, ts=now - 100)
    t.observe("fed-cuts-october", 0.55, ts=now)
    assert t.coverage("fed-cuts-october") == 100
    assert t.change("fed-cuts-october", 3600) == pytest.approx(0.05)


def test_tracker_bounded_memory():
    t = PriceTracker(max_points=10)
    for i in range(500):
        t.observe("m", 0.5, ts=float(i))
    assert len(t._series["m"]) == 10


def test_tracker_prune():
    t = PriceTracker()
    t.observe("a", 0.5)
    t.observe("b", 0.5)
    t.prune({"a"})
    assert "a" in t._series and "b" not in t._series


# --------------------------------------------------------------------------
# QuoteFetcher -- the sweep makes one request per market, so pacing is not
# optional. Unpaced, the gateway returns an HTML block page, not JSON.
# --------------------------------------------------------------------------

class _FakeMarkets:
    def __init__(self, fail_times=0, exc=RuntimeError("429")):
        self.calls = 0
        self.starts: list[float] = []
        self._fail_times = fail_times
        self._exc = exc

    async def bbo(self, slug):
        self.calls += 1
        self.starts.append(time.monotonic())
        if self.calls <= self._fail_times:
            raise self._exc
        return dict(RAW_BBO)


class _FakePM:
    def __init__(self, **kw):
        self.markets = _FakeMarkets(**kw)


@pytest.mark.asyncio
async def test_quote_fetcher_normalizes_and_counts():
    from adapters import QuoteFetcher
    pm = _FakePM()
    q = QuoteFetcher(pm, concurrency=2, min_interval=0.0)
    bbo = await q.bbo("x")
    assert bbo["bid"] == 0.53, "must return a NORMALIZED quote"
    assert q.attempts == 1 and q.failures == 0


@pytest.mark.asyncio
async def test_quote_fetcher_retries_then_succeeds():
    from adapters import QuoteFetcher
    pm = _FakePM(fail_times=2)
    q = QuoteFetcher(pm, concurrency=1, min_interval=0.0, backoff_base=0.0)
    bbo = await q.bbo("x")
    assert bbo is not None, "a transient block must not drop the market"
    assert pm.markets.calls == 3


@pytest.mark.asyncio
async def test_quote_fetcher_gives_up_and_reports():
    from adapters import QuoteFetcher
    pm = _FakePM(fail_times=99)
    q = QuoteFetcher(pm, concurrency=1, min_interval=0.0, max_retries=2,
                     backoff_base=0.0)
    assert await q.bbo("x") is None
    assert q.failures == 1
    assert "dropped" in q.report()


@pytest.mark.asyncio
async def test_quote_fetcher_paces_globally_not_just_per_task():
    """
    A semaphore alone bounds concurrency but lets N tasks fire the instant
    one frees up. The floor is between request STARTS, across all tasks.
    """
    from adapters import QuoteFetcher
    pm = _FakePM()
    q = QuoteFetcher(pm, concurrency=4, min_interval=0.05)
    await asyncio.gather(*(q.bbo(f"m{i}") for i in range(4)))
    gaps = [b - a for a, b in zip(pm.markets.starts, pm.markets.starts[1:], strict=False)]
    assert all(g >= 0.04 for g in gaps), f"requests not paced: {gaps}"


def test_interleave_spreads_markets_across_events():
    """
    A first live sweep took 40 markets and every one was MLB: events
    flatten into long runs of near-identical legs, so the head of the list
    is one event's teams. Thresholds tuned on that are tuned to one sport.
    """
    ev_a = {"slug": "a", "markets": []}
    ev_b = {"slug": "b", "markets": []}
    ev_c = {"slug": "c", "markets": []}
    pairs = (
        [({"slug": f"a{i}"}, ev_a) for i in range(30)]
        + [({"slug": f"b{i}"}, ev_b) for i in range(3)]
        + [({"slug": f"c{i}"}, ev_c) for i in range(2)]
    )
    assert len({e["slug"] for _, e in pairs[:5]}) == 1, "precondition"

    out = interleave_by_event(pairs)
    assert len(out) == len(pairs), "interleaving must not drop markets"
    assert {e["slug"] for _, e in out[:3]} == {"a", "b", "c"}
    assert len({m["slug"] for m, _ in out}) == len(pairs), "no duplicates"


# --------------------------------------------------------------------------
# Live sports: two clocks, and game state
# --------------------------------------------------------------------------

GAME_EVENT = {
    "slug": "cfb-ga-ark-2026-09-19",
    "title": "Georgia vs Arkansas",
    "startTime": "2026-09-19T16:00:00Z",   # kickoff
    "endDate": "2026-10-03T16:00:00Z",     # settlement deadline, ~2 weeks later
    "period": "Q4",
    "score": "45-17",
    "elapsed": "8:24",
    "live": True,
    "tags": [{"slug": "sports"}],
    "markets": [],
}

GAME_MARKET = {
    "slug": "astatc-cfb-ga-ark-2026-09-19-fd-h-23",
    "question": "Team Total First Downs: Over 23.5",
    "title": "Over",
    "active": True,
    "closed": False,
    "status": "MARKET_STATUS_OPEN",
    "endDate": "2026-10-03T16:00:00Z",
    "gameStartTime": "2026-09-19T16:00:00Z",
}


def test_event_clock_is_separate_from_settlement_clock():
    """
    A game played today settles ~332h later. Gating on the settlement
    deadline made every short-dated market look like a two-week hold, and
    is why a fast-resolution sweep found nothing.
    """
    m = normalize_market(GAME_MARKET, GAME_EVENT)
    assert m["event_at"] == "2026-09-19T16:00:00Z"
    assert m["settles_at"] == "2026-10-03T16:00:00Z"
    assert m["event_at"] != m["settles_at"], "conflating these hides fast markets"


def test_live_state_reaches_the_normalized_market():
    m = normalize_market(GAME_MARKET, GAME_EVENT)
    assert m["period"] == "Q4"
    assert m["score"] == "45-17"
    assert m["elapsed"] == "8:24"
    assert m["is_live"] is True


def test_game_state_classifies_period():
    from adapters import game_state
    assert game_state({"period": "NS"}) == "not_started"
    assert game_state({"period": "FT"}) == "finished"
    for live in ("Q4", "1H", "Bot 5th", "34'", "Q1"):
        assert game_state({"period": live}) == "in_play", live
    assert game_state({"period": None}) == "unknown"
    assert game_state({}) == "unknown"


def test_season_futures_carry_no_period():
    """Live fields are on game events only; futures must not crash."""
    m = normalize_market(RAW_MARKET, RAW_EVENT)
    assert m["period"] is None
    from adapters import game_state
    assert game_state(m) == "unknown"


# --------------------------------------------------------------------------
# Market selection for a game: prescreen extremes, main lines first
# --------------------------------------------------------------------------

def _m(slug, prices):
    return ({"slug": slug, "outcomePrices": prices}, {"slug": "ev"})


def test_price_band_reject_only_when_both_prices_are_extreme():
    from adapters import price_band_reject
    assert price_band_reject({"outcomePrices": '["0.9850","0.9900"]'}) is True
    assert price_band_reject({"outcomePrices": '["0.0200","0.0300"]'}) is True
    assert price_band_reject({"outcomePrices": '["0.4800","0.5000"]'}) is False
    assert price_band_reject({"outcomePrices": '["0.0300","0.0600"]'}) is False
    assert price_band_reject({"outcomePrices": "garbage"}) is False
    assert price_band_reject({}) is False


def test_listed_mid_parses_the_json_string():
    from adapters import listed_mid
    assert listed_mid({"outcomePrices": '["0.4800","0.5200"]'}) == pytest.approx(0.50)
    assert listed_mid({"outcomePrices": "nope"}) is None
    assert listed_mid({}) is None


def test_main_lines_first_orders_by_closeness_to_half():
    """
    The first in-play feed took the head of a game's ~800-market list and
    got thirty 0.985 alt-lines that never ticked. Extremes must go, and
    the contested prices must come first.
    """
    from adapters import main_lines_first
    pairs = [
        _m("cover-17.5", '["0.9850","0.9900"]'),   # extreme -> dropped
        _m("total-39.5", '["0.5000","0.5100"]'),   # main line -> first
        _m("winner-1q",  '["0.4900","0.5000"]'),
        _m("sacks-3.5",  '["0.4200","0.5500"]'),
        _m("tt-21.5",    '["0.9300","0.9400"]'),   # kept, but last
        _m("unknown",    "garbage"),               # kept, near the back
    ]
    out = [m["slug"] for m, _ in main_lines_first(pairs)]
    assert "cover-17.5" not in out
    assert out[0] in ("total-39.5", "winner-1q")
    assert out.index("tt-21.5") > out.index("sacks-3.5")
    assert len(out) == 5


# --- what YES pays on: the structured side, never the title ----------------

def _mkt(slug, title, question, desc, long_desc, team, short_desc="x", short_team="Other"):
    return {"slug": slug, "title": title, "question": question, "description": desc,
            "marketSides": [{"long": True, "description": long_desc, "team": {"name": team} if team else None},
                            {"long": False, "description": short_desc, "team": {"name": short_team}}]}


def test_underdog_spread_reads_the_yes_side_not_the_inverted_title():
    """
    Giants-Rams 2026-09-21, final 6-28: 'pos 0.5' settled NO and 'pos 33.5'
    YES, so YES = Giants + line. The title and the settlement text named the
    Rams, and three real orders were placed on the wrong side because of it.
    """
    m = _mkt("asc-nfl-nyg-lar-2026-09-21-2h-pos-3pt5", "Los Angeles Rams wins by over 3.5 points",
             "Will the New York Giants cover 3.5 vs the Los Angeles Rams?",
             "This market will settle to Yes if Los Angeles Rams outscores New York Giants by more than 3.5",
             "+3.50", "New York Giants", "-3.50", "Los Angeles Rams")
    n = normalize_market(m, {"slug": "nfl-nyg-lar-2026-09-21", "title": "NY Giants vs LA Rams"})
    assert n["outcome"] == "New York Giants +3.5"
    assert n["side_text_conflict"] is True
    assert n["description"].startswith("Resolves YES if New York Giants covers +3.5")
    assert n["description_raw"].startswith("This market will settle to Yes if Los Angeles Rams")
    assert n["title_raw"] == "Los Angeles Rams wins by over 3.5 points"


def test_moneyline_names_the_team_yes_pays_on():
    m = _mkt("aec-mlb-tor-bal-2026-09-23", "Toronto Blue Jays vs Baltimore Orioles",
             "Who will win?", "settles to the winner", "Toronto Blue Jays", "Toronto Blue Jays")
    n = normalize_market(m)
    assert n["outcome"] == "Toronto Blue Jays wins" and n["outcome_kind"] == "moneyline"


def test_agreeing_favourite_spread_and_totals_keep_their_meaning():
    fav = normalize_market(_mkt("asc-mlb-tor-bal-2026-09-23-neg-1pt5", "Toronto Blue Jays wins by over 1.5 runs",
                                "q", "Yes if the Blue Jays cover -1.5", "-1.50", "Toronto Blue Jays"))
    assert fav["outcome"] == "Toronto Blue Jays -1.5" and fav["side_text_conflict"] is False
    assert fav["description"] == "Yes if the Blue Jays cover -1.5"
    tot = normalize_market(_mkt("tsc-mlb-tor-bal-2026-09-23-f5-2pt5", "Over 2.5 total runs in first 5 innings",
                                "q", "d", "Over", None))
    assert tot["outcome"] == "Over 2.5 total runs in first 5 innings" and tot["outcome_kind"] == "total"


def test_yes_no_legs_and_markets_without_sides_keep_the_title():
    wx = normalize_market(_mkt("tc-temp-nychigh-2026-09-23-gte67lt68f", "67 to 68", "q", "d", "Yes", None))
    assert wx["outcome"] == "67 to 68" and wx["outcome_kind"] == "other"
    bare = normalize_market({"slug": "s", "title": "Over", "question": "q"})
    assert bare["outcome"] == "Over"
