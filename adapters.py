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

The websocket's `marketDataLite` payload (verified live 2026-09-20) carries
the same field set as the REST `marketData` envelope -- bestBid/bestAsk/
lastTradePx/sharesTraded/openInterest/bidShares/askShares/state -- not
the three fields the SDK stub `_MarketDataLitePayload` declares. Parse
it with normalize_bbo({"marketData": payload}); nothing else is needed.

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

import asyncio
import json
import logging
import time
from collections import deque
from typing import Any, Deque

log = logging.getLogger("adapters")


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
        # TWO DIFFERENT CLOCKS, and conflating them is why short-dated
        # markets looked absent. `endDate` is the SETTLEMENT DEADLINE, not
        # the event: a college football game played today carries an
        # endDate ~332h out. The outcome is decided in hours, and -- verified
        # live 2026-09-20 -- markets.settlement() returns the result within
        # minutes of the final. endDate is the DEADLINE by which the exchange
        # must settle, not when it does. A backfill can run the same day.
        #
        #   settles_at  when the payout lands      -> capital-parked check
        #   event_at    when the outcome is known  -> everything else
        #
        # `closes_at` stays as settles_at for the existing consumers.
        "closes_at": (market.get("endDate") or ev.get("endDate")
                      or ev.get("endTime")),
        "settles_at": (market.get("endDate") or ev.get("endDate")),
        "event_at": (ev.get("startTime") or market.get("gameStartTime")
                     or market.get("startDate")),
        "starts_at": ev.get("startTime") or market.get("startDate"),
        # Live game state. Present on game events only, and absent from
        # season futures. `period` is the reliable one -- it is on every
        # sports event; score/elapsed populate once play begins.
        #   NS = not started, FT = full time, otherwise in play
        #   ("Q4", "1H", "Bot 5th", "34'")
        "period": ev.get("period"),
        "score": ev.get("score"),
        "elapsed": ev.get("elapsed"),
        "is_live": bool(ev.get("live")),
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


# Sampling across these instead of taking the default listing page. The
# default page is ~half sports, and sports markets are uniformly
# `contested_event`: a first calibration sweep triaged 33 markets and all
# 33 were that one type, with gate scores spanning 0.38-0.48. A threshold
# sweep over that sample is a cliff, not a curve, and tuning against it
# would tune the gate to baseball.
DEFAULT_TAG_MIX = (
    "economics", "business", "crypto", "culture", "science",
    "politics", "sports",
)


async def fetch_events_across_tags(
    pm: Any,
    tags: "tuple[str, ...] | list[str]" = DEFAULT_TAG_MIX,
    per_tag: int = 10,
    pause: float = 1.0,
    extra: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """
    One events.list per tag, de-duplicated by event slug.

    `tagSlug` is a real filter on this gateway (verified: economics,
    crypto, culture and sports each return disjoint event sets). Without
    it the sample composition is whatever the exchange happens to list
    first, which is not a choice anyone made.

    A failing tag is skipped rather than fatal: partial coverage beats no
    sweep, and tag slugs may change.
    """
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for tag in tags:
        try:
            page = await pm.events.list(
                {"limit": per_tag, "closed": False, "tagSlug": tag,
                 **(extra or {})}
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("tag %s failed, skipping: %s", tag, exc)
            continue
        for ev in page.get("events", []) or []:
            slug = ev.get("slug")
            if slug and slug in seen:
                continue
            if slug:
                seen.add(slug)
            out.append(ev)
        await asyncio.sleep(pause)
    return out


#: `period` values that mean the outcome is already determined. Markets on
#: these stay open until settlement, but the books go wide the moment play
#: ends -- measured spreads of 0.28-0.49 on finished college football games,
#: and 0 shares on the one quote near $1.00. There is no settlement-lag
#: trade here; treat FT as a reason to skip, not an opportunity.
PERIOD_FINISHED = {"FT", "AOT", "FT_PEN", "AET", "Final", "F"}

#: `period` value meaning play has not begun.
PERIOD_NOT_STARTED = {"NS", "TBD", "PST"}


def game_state(market: dict[str, Any]) -> str:
    """
    'not_started' | 'in_play' | 'finished' | 'unknown', from a normalized
    market. Arithmetic on a string, so it belongs in code, not a question.
    """
    p = (market.get("period") or "").strip()
    if not p:
        return "unknown"
    if p in PERIOD_FINISHED:
        return "finished"
    if p in PERIOD_NOT_STARTED:
        return "not_started"
    return "in_play"


def price_band_reject(market: dict[str, Any], min_price: float = 0.05,
                      max_price: float = 0.95) -> bool:
    """
    True when the market's listed prices put it outside the price band on
    the same side -- an extreme line the structural filter would reject
    after a paced quote. Conservative on purpose: anything ambiguous or
    unparseable is NOT rejected here; it goes to the quote.

    `outcomePrices` is a JSON STRING on the wire ('["0.9850","0.9900"]'),
    not a list -- the same decimal-string convention as Amount.
    """
    raw = market.get("outcomePrices")
    try:
        vals = json.loads(raw) if isinstance(raw, str) else (raw or [])
        prices = [float(v) for v in vals][:2]
    except (TypeError, ValueError):
        return False
    if len(prices) < 2:
        return False
    return all(p > max_price for p in prices) or all(p < min_price for p in prices)


def listed_mid(market: dict[str, Any]) -> float | None:
    """Mid of the two listed outcomePrices, or None. Cheap; no quote."""
    raw = market.get("outcomePrices")
    try:
        vals = json.loads(raw) if isinstance(raw, str) else (raw or [])
        p = [float(v) for v in vals][:2]
    except (TypeError, ValueError):
        return None
    return (p[0] + p[1]) / 2.0 if len(p) == 2 else None


def main_lines_first(
    pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    min_price: float = 0.05,
    max_price: float = 0.95,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """
    Prescreen extremes, then order by closeness to 0.50.

    A game event lists ~800 markets extreme-first: "cover 17.5" at 0.985,
    "4th down conversions over 0.5" at 0.98. Taking the head of that list
    -- as the first in-play feed did -- subscribes to thirty markets that
    will barely tick all game. The main lines (game total, spread,
    moneyline) and the contested props sit near 0.50, and those are the
    ones whose price actually moves with the score.
    """
    kept = [(m, e) for m, e in pairs if not price_band_reject(m, min_price, max_price)]

    def key(pe: tuple[dict[str, Any], dict[str, Any]]) -> float:
        mid = listed_mid(pe[0])
        return abs(mid - 0.5) if mid is not None else 0.49  # unknown: near the back
    return sorted(kept, key=key)


def interleave_by_event(
    pairs: list[tuple[dict[str, Any], dict[str, Any]]],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """
    Round-robin the markets across their parent events.

    Events flatten into long runs of near-identical legs -- one event can
    contribute 30 markets that differ only by which team is named. Taking
    the head of that list gives a sweep that is entirely one sport, which
    is worse than a small sample: it is a biased one, and thresholds tuned
    on it would be tuned to a single market type.

    Taking one market per event per round gives the same count with
    coverage across events.
    """
    by_event: dict[str, list] = {}
    for m, ev in pairs:
        by_event.setdefault(ev.get("slug") or id(ev), []).append((m, ev))
    out = []
    rounds = max((len(v) for v in by_event.values()), default=0)
    for i in range(rounds):
        for bucket in by_event.values():
            if i < len(bucket):
                out.append(bucket[i])
    return out


# --------------------------------------------------------------------------
# paced access to the quote endpoint
# --------------------------------------------------------------------------

class QuoteFetcher:
    """
    Rate-limited, retrying wrapper around `markets.bbo`.

    Volume and liquidity are derived from the quote, so the structural
    filter needs one request per candidate market -- roughly 1,100 per
    sweep at the current floors. That is not optional and it is not
    cheap in requests.

    Unpaced, this gets Cloudflare-blocked at around 25 rapid calls and
    returns an HTML error page rather than JSON. A collector at 2
    concurrent with 1.2s spacing still lost ~30% of a sample, so treat
    failures as expected and retry them rather than dropping the market.

    Pacing is global, not per-task: a semaphore alone bounds concurrency
    but still lets N tasks fire simultaneously the moment one frees up.
    """

    def __init__(
        self,
        pm: Any,
        concurrency: int = 2,
        min_interval: float = 1.2,
        max_retries: int = 3,
        backoff_base: float = 1.5,
        block_pause: float = 30.0,
    ):
        self._pm = pm
        self._sem = asyncio.Semaphore(concurrency)
        self._min_interval = min_interval
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._block_pause = block_pause
        self._lock = asyncio.Lock()
        self._next_at = 0.0
        self.attempts = 0
        self.failures = 0

    async def _pace(self) -> None:
        """Hold the floor between request starts, across all tasks."""
        async with self._lock:
            now = time.monotonic()
            wait = self._next_at - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            self._next_at = now + self._min_interval

    async def bbo(self, slug: str) -> dict[str, Any] | None:
        """Normalized quote, or None once retries are exhausted."""
        for attempt in range(self._max_retries + 1):
            async with self._sem:
                await self._pace()
                self.attempts += 1
                try:
                    raw = await self._pm.markets.bbo(slug)
                except Exception as exc:  # noqa: BLE001 - transport or HTML
                    if type(exc).__name__ in ("RateLimitError",
                                              "PermissionDeniedError"):
                        # A Cloudflare block is global, not per-task. Without
                        # this, the blocked task backs off while the others
                        # keep hitting the endpoint at full pace and prolong
                        # the block. Push the shared floor so everyone waits.
                        async with self._lock:
                            self._next_at = max(
                                self._next_at,
                                time.monotonic() + self._block_pause,
                            )
                        log.warning("quote endpoint blocked; pausing all "
                                    "requests %.0fs", self._block_pause)
                    if attempt == self._max_retries:
                        self.failures += 1
                        log.debug("bbo %s failed after %d attempts: %s",
                                  slug, attempt + 1, exc)
                        return None
                else:
                    return normalize_bbo(raw)
            await asyncio.sleep(self._backoff_base * (2 ** attempt))
        return None

    def report(self) -> str:
        ok = self.attempts - self.failures
        return (f"quotes: {ok}/{self.attempts} requests succeeded, "
                f"{self.failures} markets dropped")


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
