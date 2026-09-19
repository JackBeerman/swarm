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

import os

import pytest

os.environ.setdefault("TYPESAFE_API_KEY", "test-key")

from adapters import (  # noqa: E402
    PriceTracker,
    amount,
    derive_notionals,
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
