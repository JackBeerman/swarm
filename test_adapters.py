"""
Tests for the SDK adapter layer, written against the real polymarket_us
TypedDict shapes rather than the spec's assumed field names.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("TYPESAFE_API_KEY", "test-key")

from adapters import (  # noqa: E402
    PriceTracker,
    amount,
    iter_event_markets,
    normalize_bbo,
    normalize_market,
)
from swarm import JevTriage  # noqa: E402

# Shapes below match polymarket_us 0.1.2 TypedDicts exactly.
RAW_BBO = {
    "marketSlug": "fed-cuts-october",
    "bestBid": {"value": "0.53", "currency": "USD"},
    "bestAsk": {"value": "0.55", "currency": "USD"},
    "bidDepth": 4200,
    "askDepth": 3800,
    "lastTradePx": {"value": "0.54", "currency": "USD"},
    "sharesTraded": "120000",
    "openInterest": "88000",
}

RAW_MARKET = {
    "id": 991,
    "slug": "fed-cuts-october",
    "title": "Will the Fed cut rates in October?",
    "outcome": "Yes",
    "description": "Resolves YES if the FOMC lowers the target range.",
    "active": True,
    "closed": False,
    "liquidity": 140_000.0,
    "volume": 812_000.0,
    "eventSlug": "fomc-october",
}

RAW_EVENT = {
    "id": 77,
    "slug": "fomc-october",
    "title": "FOMC October Decision",
    "description": "Federal Open Market Committee, October meeting.",
    "startTime": "2026-10-01T00:00:00Z",
    "endTime": "2026-10-29T18:00:00Z",
    "active": True,
    "closed": False,
    "liquidity": 140_000.0,
    "volume": 812_000.0,
    "markets": [RAW_MARKET],
    "tags": [{"id": 3, "slug": "economics", "label": "Economics"}],
}


def test_amount_parses_decimal_strings():
    assert amount({"value": "0.55", "currency": "USD"}) == 0.55
    assert amount({"value": "1", "currency": "USD"}) == 1.0
    assert amount(0.42) == 0.42
    assert amount(None) is None
    assert amount({"currency": "USD"}) is None
    assert amount({"value": "abc"}) is None


def test_normalize_bbo_extracts_nested_amounts():
    b = normalize_bbo(RAW_BBO)
    assert b["bid"] == 0.53
    assert b["ask"] == 0.55
    assert b["bid_depth"] == 4200
    assert b["last"] == 0.54
    assert isinstance(b["bid"], float)


def test_normalize_bbo_survives_empty():
    assert normalize_bbo(None)["bid"] is None
    assert normalize_bbo({})["ask"] is None


def test_normalize_market_pulls_close_time_from_event():
    m = normalize_market(RAW_MARKET, RAW_EVENT)
    assert m["question"] == "Will the Fed cut rates in October?"
    assert m["volume_usd"] == 812_000.0
    assert m["liquidity_usd"] == 140_000.0
    assert m["closes_at"] == "2026-10-29T18:00:00Z"   # lives on the EVENT
    assert m["tags"] == ["economics"]


def test_normalize_market_without_event_has_no_close_time():
    m = normalize_market(RAW_MARKET)
    assert m["closes_at"] is None
    assert m["question"] == "Will the Fed cut rates in October?"


def test_iter_event_markets_applies_volume_floor():
    resp = {"events": [RAW_EVENT]}
    assert len(iter_event_markets(resp, min_volume=50_000)) == 1
    assert len(iter_event_markets(resp, min_volume=900_000)) == 0


def test_iter_event_markets_skips_closed():
    closed = {**RAW_EVENT, "markets": [{**RAW_MARKET, "closed": True}]}
    assert iter_event_markets({"events": [closed]}) == []


# --------------------------------------------------------------------------
# the regression this layer exists to prevent
# --------------------------------------------------------------------------

def test_raw_sdk_shapes_would_have_produced_an_empty_state():
    """
    Passing raw SDK objects straight through yields a state full of Nones,
    and Jev would return confident probabilities about nothing. This is the
    failure the adapter exists to make impossible.
    """
    bad = JevTriage.build_state(RAW_MARKET, RAW_BBO)
    assert bad["best_bid"] is None
    assert bad["best_ask"] is None
    assert bad["volume_usd"] is None
    assert bad["question"] is None

    good = JevTriage.build_state(
        normalize_market(RAW_MARKET, RAW_EVENT), normalize_bbo(RAW_BBO)
    )
    assert good["best_bid"] == 0.53
    assert good["spread"] == pytest.approx(0.02)
    assert good["volume_usd"] == 812_000.0
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
    assert full["volume_usd"] == 812_000.0
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
