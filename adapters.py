"""
adapters.py -- normalize polymarket_us SDK types into the flat dicts the
pipeline consumes.

This module exists because the field names in the original spec came from
Polymarket's *legacy* CLOB/Gamma API, not from Polymarket US. Verified
against polymarket-us 0.1.2:

    spec assumed              actual (polymarket_us)
    ------------------------  ----------------------------------------
    market["question"]        MarketDetail["title"]
    market["volumeNum"]       MarketDetail["volume"]
    market["liquidityNum"]    MarketDetail["liquidity"]
    market["endDate"]         Event["endTime"]        (on the EVENT)
    market["resolution...]    -- does not exist --
    market["oneHourPrice..."] -- does not exist --
    bbo["bid"] / ["ask"]      bbo["bestBid"]["value"] (a STRING)
    volumeNumMin (filter)     volumeMin

Two consequences worth internalizing:

1. `Amount` is {"value": "0.55", "currency": "USD"} -- a decimal string,
   deliberately, to avoid float drift on money. Parse it once, at this
   boundary, and never let the raw string reach the sizing code.

2. There is no price-change field anywhere in the REST API. The spec's
   "sudden price movements" trigger cannot be built from REST polling; it
   has to be derived from the markets websocket (`market_data_lite`) or
   from a local rolling window over `stats.lastTradePx`. PriceTracker
   below does the latter, which is the cheap version.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any, Deque


def amount(a: Any) -> float | None:
    """Parse an SDK Amount ({"value": "0.55", "currency": "USD"}) to float."""
    if a is None:
        return None
    if isinstance(a, (int, float)):
        return float(a)
    if isinstance(a, dict):
        v = a.get("value")
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None
    try:
        return float(a)
    except (TypeError, ValueError):
        return None


def normalize_bbo(bbo: dict[str, Any] | None) -> dict[str, Any]:
    """MarketBBO -> {"bid", "ask", "bid_depth", "ask_depth", "last"}."""
    if not bbo:
        return {"bid": None, "ask": None}
    return {
        "bid": amount(bbo.get("bestBid")),
        "ask": amount(bbo.get("bestAsk")),
        "bid_depth": bbo.get("bidDepth"),
        "ask_depth": bbo.get("askDepth"),
        "last": amount(bbo.get("lastTradePx")),
        "open_interest": bbo.get("openInterest"),
    }


def normalize_market(
    market: dict[str, Any],
    event: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    MarketDetail (+ optional parent Event) -> the flat dict used by
    JevTriage.build_state and the gatherers.

    Pass the parent event when you have it. Without it you lose the close
    time and the tags, and the close time in particular materially changes
    how a triage model should read a price move.
    """
    ev = event or {}
    tags = [t.get("slug") for t in ev.get("tags", []) if isinstance(t, dict)]
    return {
        "slug": market.get("slug"),
        "question": market.get("title"),
        "outcome": market.get("outcome"),
        "description": (market.get("description") or ev.get("description") or ""),
        "event_slug": market.get("eventSlug") or ev.get("slug"),
        "event_title": ev.get("title"),
        "closes_at": ev.get("endTime"),
        "starts_at": ev.get("startTime"),
        "tags": tags,
        "volume_usd": market.get("volume"),
        "liquidity_usd": market.get("liquidity"),
        "active": market.get("active"),
        "closed": market.get("closed"),
    }


def iter_event_markets(
    events_response: dict[str, Any],
    min_volume: float = 0.0,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """
    Flatten a GetEventsResponse into [(market, parent_event), ...].

    Collect via events rather than markets.list: the close time and tags
    live on the event, and both matter to triage.
    """
    out: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for ev in events_response.get("events", []):
        for m in ev.get("markets", []):
            if (m.get("volume") or 0) < min_volume:
                continue
            if m.get("closed") or not m.get("active"):
                continue
            out.append((m, ev))
    return out


# --------------------------------------------------------------------------
# price movement, since REST does not provide it
# --------------------------------------------------------------------------

class PriceTracker:
    """
    Rolling mid-price window per market, so triage can be handed an actual
    price delta instead of a field that does not exist.

    Feed it from either the markets websocket or your poll loop. Memory is
    bounded per market; call prune() periodically if you track thousands.
    """

    def __init__(self, window_seconds: float = 3600.0, max_points: int = 240):
        self.window = window_seconds
        self.max_points = max_points
        self._series: dict[str, Deque[tuple[float, float]]] = {}

    def observe(self, slug: str, mid: float, ts: float | None = None) -> None:
        if mid is None:
            return
        t = ts if ts is not None else time.time()
        dq = self._series.setdefault(slug, deque(maxlen=self.max_points))
        dq.append((t, float(mid)))

    def observe_bbo(self, slug: str, bbo: dict[str, Any]) -> None:
        nb = normalize_bbo(bbo)
        if nb["bid"] is not None and nb["ask"] is not None:
            self.observe(slug, (nb["bid"] + nb["ask"]) / 2.0)

    def change(self, slug: str, seconds: float) -> float | None:
        """Price delta over the trailing `seconds`. None if not enough history."""
        dq = self._series.get(slug)
        if not dq or len(dq) < 2:
            return None
        now, latest = dq[-1]
        cutoff = now - seconds
        prior = None
        for t, p in dq:
            if t >= cutoff:
                prior = p
                break
        if prior is None:
            return None
        return round(latest - prior, 4)

    def coverage(self, slug: str) -> float:
        """Seconds of history held. Below ~900s, treat deltas as unreliable."""
        dq = self._series.get(slug)
        if not dq or len(dq) < 2:
            return 0.0
        return dq[-1][0] - dq[0][0]

    def prune(self, keep_slugs: set[str]) -> None:
        for slug in list(self._series):
            if slug not in keep_slugs:
                del self._series[slug]
