"""
adapters.py -- normalize polymarket_us SDK types into the flat dicts the
pipeline consumes.

This module exists because the field names in the original spec came from
Polymarket's *legacy* CLOB/Gamma API, not from Polymarket US.

WHERE THESE NAMES COME FROM. An earlier version of this table was read off
the SDK's TypedDicts in polymarket_us/types/. Those are declared
`total=False`, so they assert nothing at runtime, and several of the fields
they declare are never sent by the gateway. The mapping below was taken
from live responses on 2026-09-19 instead. Where the stubs and the wire
disagree, the wire wins:

    stub / spec says          actually on the wire
    ------------------------  ----------------------------------------
    Market["volume"]          -- does not exist -- (see derive_notionals)
    Market["liquidity"]       -- does not exist --
    Market["title"]           the OUTCOME leg ("Democratic Party")
    Market["question"]        the event question ("U.S House Midterm Winner")
    Market["outcome"]         -- does not exist -- (use title)
    Event["endTime"]          Event["endDate"]; the market has its OWN
                              endDate, and the two differ
    bbo["bestBid"]            bbo["marketData"]["bestBid"] -- everything
                              is wrapped in a marketData envelope
    market["resolution...]    -- does not exist --
    market["oneHourPrice..."] -- does not exist --
    volumeNumMin (filter)     volumeMin -- accepted and SILENTLY IGNORED

One more, and it is the one that produces zero markets rather than wrong
ones: `active` and `closed` are orthogonal. A resolved market is
`active=True, closed=True, status="MARKET_STATUS_RESOLVED"`. Querying
events with {"active": True} returns resolved markets almost exclusively.
Filter on `closed: False`.

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


_EMPTY_BBO = {
    "bid": None, "ask": None, "bid_depth": None, "ask_depth": None,
    "last": None, "open_interest": None, "shares_traded": None,
    "bid_shares": None, "ask_shares": None, "state": None,
}


def normalize_bbo(bbo: dict[str, Any] | None) -> dict[str, Any]:
    """
    MarketBBO -> flat quote dict.

    The response is wrapped: {"marketData": {"bestBid": {...}, ...}}. Read
    through the envelope. Missing it does not raise -- it silently returns
    bid=None for every market, which reads downstream as `no_quote` and
    looks exactly like a selective gate.
    """
    if not bbo:
        return dict(_EMPTY_BBO)
    md = bbo.get("marketData")
    if isinstance(md, dict):
        bbo = md
    return {
        "bid": amount(bbo.get("bestBid")),
        "ask": amount(bbo.get("bestAsk")),
        "bid_depth": bbo.get("bidDepth"),
        "ask_depth": bbo.get("askDepth"),
        "last": amount(bbo.get("lastTradePx")),
        # Share counts, not dollars, and sent as decimal strings.
        "open_interest": _num(bbo.get("openInterest")),
        "shares_traded": _num(bbo.get("sharesTraded")),
        "bid_shares": _num(bbo.get("bidShares")),
        "ask_shares": _num(bbo.get("askShares")),
        "state": bbo.get("state"),
    }


def _num(v: Any) -> float | None:
    """The share-count fields arrive as strings like "446567.0000"."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def derive_notionals(bbo: dict[str, Any]) -> dict[str, float | None]:
    """
    Dollar volume and open notional, since the API sends neither.

    `sharesTraded` and `openInterest` are share counts; a share settles at
    $0 or $1, so mid-price is the conversion. This is arithmetic, so it
    belongs here and not in a question -- but note it is a *proxy*, and
    the floors in questions.py were never tuned against its distribution.
    """
    bid, ask = bbo.get("bid"), bbo.get("ask")
    if bid is None or ask is None:
        return {"volume_usd": None, "liquidity_usd": None}
    mid = (bid + ask) / 2.0
    traded, oi = bbo.get("shares_traded"), bbo.get("open_interest")
    return {
        "volume_usd": None if traded is None else round(traded * mid, 2),
        "liquidity_usd": None if oi is None else round(oi * mid, 2),
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
        # `question` is the event-level proposition ("U.S House Midterm
        # Winner"); `title` is the leg being priced ("Democratic Party").
        # Triage needs both, and swapping them makes every question in
        # questions.py read against the wrong string.
        "question": market.get("question") or market.get("title"),
        "outcome": market.get("title"),
        "description": (market.get("description") or ev.get("description") or ""),
        "event_slug": market.get("eventSlug") or ev.get("slug"),
        "event_title": ev.get("title"),
        # The market's own endDate is when THIS leg resolves and can be
        # months past the event's. Prefer it; that difference is the whole
        # "capital parked too long" check.
        "closes_at": (market.get("endDate") or ev.get("endDate")
                      or ev.get("endTime")),
        "starts_at": ev.get("startTime") or market.get("startDate"),
        "tags": tags,
        # Not on the wire. Filled by derive_notionals() from the quote.
        "volume_usd": None,
        "liquidity_usd": None,
        "active": market.get("active"),
        "closed": market.get("closed"),
        "status": market.get("status"),
    }


def iter_event_markets(
    events_response: dict[str, Any],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """
    Flatten a GetEventsResponse into [(market, parent_event), ...].

    Collect via events rather than markets.list: the close time and tags
    live on the event, and both matter to triage.

    There is deliberately no min_volume argument. Markets carry no volume
    field, so the old one compared None to a floor and returned an empty
    list at any nonzero value. Volume filtering now needs a quote, and so
    happens in structural_filter() after the bbo fetch.
    """
    out: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for ev in events_response.get("events", []):
        for m in ev.get("markets", []) or []:
            # `active` stays True on resolved markets, so it cannot carry
            # this on its own.
            if m.get("closed") or not m.get("active"):
                continue
            if m.get("status") == "MARKET_STATUS_RESOLVED":
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
