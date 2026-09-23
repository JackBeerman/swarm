"""
kalshi.py -- read-only Kalshi market data. No account, no key, no orders.

SHADOW ONLY. This module has no order path of any kind: it issues GET
requests to public market-data endpoints and nothing else. A test pins
that (tests/test_kalshi.py greps this file for write verbs and account
paths). Do not add one here; an order path, if one is ever justified,
belongs in daemon.py behind the same gates as every other order.

VERIFIED 2026-09-23 (docs + live GETs, paced >= 2 s):
  * Base URL. The docs' quick start uses
    https://external-api.kalshi.com/trade-api/v2 ; the older
    https://api.elections.kalshi.com/trade-api/v2 answers identically
    (both 200 on /series/KXHIGHNY). "elections" is only a hostname -- it
    serves every category.
  * "No authentication headers are required" for series, events, markets
    and orderbook (docs.kalshi.com/getting_started/quick_start_market_data).
    Rate limits are token buckets per account tier; unauthenticated reads
    are not separately documented, so this client paces itself at
    >= 1.5 s between calls and backs off on 429 (docs: 429 carries no
    Retry-After header, "apply exponential backoff").
  * Prices are dollar STRINGS ("0.4600") in *_dollars fields; sizes are
    fixed-point strings in *_fp fields ("3230.50"). Contracts may be
    fractional (min 0.01).
  * The orderbook returns BIDS ONLY: {"orderbook_fp": {"yes_dollars":
    [[px, qty], ...], "no_dollars": [...]}}, ascending, best bid LAST. A
    YES ask at p is a NO bid at 1 - p (docs: getting_started/
    orderbook_responses). `?depth=N` returns the best N levels.
  * Fees are per SERIES: `fee_type` (quadratic | quadratic_with_maker_fees
    | quadratic_with_combo_maker_fees | flat) and `fee_multiplier`. Seen:
    weather quadratic x1; MLB game quadratic_with_maker_fees x0.5; MLB
    spread/total quadratic x0.5; NFL game quadratic_with_maker_fees x1.
    See taker_fee() for the formula and how sure we are of it.
"""

from __future__ import annotations

import logging
import math
import time
from typing import Any

import httpx

log = logging.getLogger("kalshi")

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
MIN_INTERVAL_S = 1.5

#: Taker coefficient of the General Trading Fees Table. Kalshi Fee
#: Schedule (2nd iteration, 2022-09-22, filed with the CFTC):
#:   "fees = round up(0.07 x C x P x (1-P))", round up = to the next cent.
#: The current schedule (kalshi.com/docs/kalshi-fee-schedule.pdf) returned
#: HTTP 429 to every fetch on 2026-09-23. Secondary sources (2026) give the
#: same 0.07 scaled by a per-market multiplier M, and the API's own schema
#: says fee_multiplier is "a floating point multiplier applied to the fee
#: calculations". So: taker = ceil_cent(M x 0.07 x C x P x (1-P)).
#: The M < 1 reading is the part NOT verified against the primary PDF;
#: xvenue.py therefore also reports every opportunity at M = 1.
TAKER_RATE = 0.07

#: Kalshi categories this project never reads. Politics is a standing
#: operator rule; "Economics" and "Financials" hold Fed and policy
#: markets (federal policy is on the restricted list too).
DENY_CATEGORIES = {"politics", "elections", "world", "economics", "financials",
                   "companies", "mentions", "social", "health"}


def taker_fee(price: float, contracts: float, multiplier: float = 1.0) -> float:
    """Kalshi taker fee for one order, rounded up to the cent."""
    if contracts <= 0 or not (0.0 < price < 1.0):
        return 0.0
    raw = multiplier * TAKER_RATE * contracts * price * (1.0 - price)
    return math.ceil(raw * 100 - 1e-9) / 100.0


def taker_fee_per_contract(price: float, multiplier: float = 1.0) -> float:
    """Marginal taker fee per contract before rounding."""
    if not (0.0 < price < 1.0):
        return 0.0
    return multiplier * TAKER_RATE * price * (1.0 - price)


def dollars(v: Any) -> float | None:
    """'0.4600' -> 0.46; None/'' -> None."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def market_quote(m: dict[str, Any]) -> dict[str, float | None]:
    """
    Top of book from a listed market. A zero price means no order on that
    side (a YES bid of 0.00 is an empty book, not a price), so it is None.
    """
    def px(k: str) -> float | None:
        v = dollars(m.get(k))
        return v if v is not None and 0.0 < v < 1.0 else None

    return {
        "yes_bid": px("yes_bid_dollars"), "yes_ask": px("yes_ask_dollars"),
        "yes_bid_size": dollars(m.get("yes_bid_size_fp")),
        "yes_ask_size": dollars(m.get("yes_ask_size_fp")),
    }


def ladders(orderbook: dict[str, Any]) -> dict[str, list[tuple[float, float]]]:
    """
    Orderbook -> the two BUY ladders, cheapest first:
      yes_asks: buy YES at 1 - (NO bid), size of that NO bid
      no_asks:  buy NO  at 1 - (YES bid)
    """
    ob = orderbook.get("orderbook_fp") or {}

    def levels(key: str) -> list[tuple[float, float]]:
        out = []
        for row in ob.get(key) or []:
            p, q = dollars(row[0]), dollars(row[1])
            if p is not None and q and 0.0 < p < 1.0:
                out.append((round(1.0 - p, 4), q))
        return sorted(out)

    return {"yes_asks": levels("no_dollars"), "no_asks": levels("yes_dollars")}


def series_blocked(series: dict[str, Any]) -> str | None:
    """
    Reason a series must never be read, else None. Two layers, like the
    rest of the repo: an explicit category deny-list, then the shared
    political_tag() over the series' category, categories and tags.
    """
    from questions import political_tag

    cats = [series.get("category") or ""] + list(series.get("categories") or [])
    for c in cats:
        if c.strip().lower() in DENY_CATEGORIES:
            return f"category {c}"
    tag = political_tag([*cats, *(series.get("tags") or []), series.get("title") or ""])
    if tag:
        return f"political tag {tag}"
    return None


class KalshiError(RuntimeError):
    pass


class Kalshi:
    """
    Paced, read-only GET client with a hard wall-clock deadline.

    Every call waits at least `min_interval` since the previous one and
    refuses to start after `deadline` (time.monotonic()), so a scan can
    never run away.
    """

    def __init__(self, base_url: str = BASE_URL, min_interval: float = MIN_INTERVAL_S,
                 deadline: float | None = None, client: httpx.Client | None = None,
                 max_retries: int = 3):
        self.base_url = base_url.rstrip("/")
        self.min_interval = min_interval
        self.deadline = deadline
        self.max_retries = max_retries
        self._client = client or httpx.Client(timeout=20.0,
                                              headers={"Accept": "application/json"})
        self._last = 0.0
        self.calls = 0

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "Kalshi":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        for attempt in range(self.max_retries + 1):
            if self.deadline is not None and time.monotonic() > self.deadline:
                raise KalshiError("wall-clock budget exhausted")
            wait = self._last + self.min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            self.calls += 1
            r = self._client.get(f"{self.base_url}{path}", params=params)
            if r.status_code == 429 and attempt < self.max_retries:
                time.sleep(2.0 * (2 ** attempt))
                continue
            if r.status_code != 200:
                raise KalshiError(f"GET {path} -> {r.status_code}")
            return r.json()
        raise KalshiError(f"GET {path} -> 429 after retries")

    def series(self, ticker: str) -> dict[str, Any]:
        return self._get(f"/series/{ticker}").get("series") or {}

    def events(self, series_ticker: str, status: str = "open",
               max_pages: int = 3) -> list[dict[str, Any]]:
        """Open events of one series with their markets nested."""
        out: list[dict[str, Any]] = []
        cursor = None
        for _ in range(max_pages):
            params: dict[str, Any] = {"series_ticker": series_ticker, "status": status,
                                      "limit": 200, "with_nested_markets": "true"}
            if cursor:
                params["cursor"] = cursor
            page = self._get("/events", params)
            out.extend(page.get("events") or [])
            cursor = page.get("cursor")
            if not cursor:
                break
        return out

    def orderbook(self, ticker: str, depth: int = 10) -> dict[str, Any]:
        return self._get(f"/markets/{ticker}/orderbook", {"depth": depth})
