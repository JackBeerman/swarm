"""
arb.py -- complete-set arbitrage on events where exactly one leg wins.

SHADOW ONLY. Places no orders. Records what a complete set would have
cost and paid, net of fees, at the depth actually on the book.

If every outcome of an event is listed and exactly one resolves YES:

    buy YES on every leg   costs  sum(ask_i) + fees,   pays exactly 1
    buy NO  on every leg   costs  sum(1 - bid_i) + fees, pays exactly n - 1

so the YES set profits when sum(ask) + fees < 1 and the NO set profits
when sum(bid) - fees > 1, whatever happens. That is the only trade in
this repo whose profit does not depend on being right about anything.

The whole risk is in "exactly one leg wins". A set is admitted only when
completeness can be PROVED from the market data: numeric bands that
chain with no gap and no overlap from an open lower band to an open
upper band (weather highs: "65 or below", "66 to 67", ..., "74 or
above"). "Who wins X" events are excluded -- an unlisted winner makes
every leg lose -- however exhaustive they look.

    python arb.py            # scan once, print, record
    python arb.py --report   # what has been seen so far
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from typing import Any

from fees import fee_usd

log = logging.getLogger("arb")

DB_PATH = os.getenv("ARB_DB", "arb.db")
TAGS = ("weather",)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    id           INTEGER PRIMARY KEY,
    at           TEXT NOT NULL,
    event_slug   TEXT NOT NULL,
    title        TEXT,
    n_legs       INTEGER NOT NULL,
    sum_ask      REAL,          -- NULL when a leg had no ask
    sum_bid      REAL,          -- NULL when a leg had no bid
    yes_profit   REAL,          -- per complete set, after fees; NULL if not buyable
    no_profit    REAL,
    yes_sets     REAL,          -- complete sets buyable at the touch (min depth)
    no_sets      REAL,
    legs_json    TEXT
);
CREATE INDEX IF NOT EXISTS idx_scans_at ON scans(at);
"""


def connect(path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


# --------------------------------------------------------------------------
# completeness -- the part that must be right
# --------------------------------------------------------------------------

_BAND = re.compile(r"-(?:(?:gte(?P<lo>\d+))?(?:lt(?P<lt>\d+))?)f?$")


def band_of(slug: str) -> tuple[float | None, float | None] | None:
    """
    Integer band [lo, hi] a weather-style slug covers; None bounds are open.
    gte66lt67 -> (66, 67)  (listed as "66 to 67", inclusive)
    lt66      -> (None, 65)
    gte74     -> (74, None)
    """
    m = _BAND.search(slug or "")
    if not m or (m["lo"] is None and m["lt"] is None):
        return None
    lo = float(m["lo"]) if m["lo"] else None
    if m["lo"] and m["lt"]:
        hi: float | None = float(m["lt"])
    elif m["lt"]:
        hi = float(m["lt"]) - 1
    else:
        hi = None
    return lo, hi


def complete_ladder(slugs: list[str]) -> bool:
    """
    True only if the bands chain from an open bottom to an open top with
    no gap and no overlap on integers. Anything unparseable -> False.
    """
    bands = [band_of(s) for s in slugs]
    if len(bands) < 2 or any(b is None for b in bands):
        return False
    bands.sort(key=lambda b: -1e9 if b[0] is None else b[0])
    if bands[0][0] is not None or bands[-1][1] is not None:
        return False                           # both ends must be open
    for (_, hi), (lo, _) in zip(bands, bands[1:], strict=False):
        if hi is None or lo is None or lo != hi + 1:
            return False
    return True


# --------------------------------------------------------------------------
# the arithmetic
# --------------------------------------------------------------------------

def price_set(legs: list[dict[str, Any]]) -> dict[str, Any]:
    """
    legs: [{slug, bid, ask, bid_shares, ask_shares}]. Profit per complete
    set after fees, and how many sets the thinnest leg allows.
    """
    n = len(legs)
    out: dict[str, Any] = {"n_legs": n, "sum_ask": None, "sum_bid": None,
                           "yes_profit": None, "no_profit": None, "yes_sets": 0.0, "no_sets": 0.0}
    if all(leg.get("ask") is not None for leg in legs):
        out["sum_ask"] = round(sum(leg["ask"] for leg in legs), 4)
        fees = sum(fee_usd(leg["ask"], 1) for leg in legs)
        out["yes_profit"] = round(1.0 - out["sum_ask"] - fees, 4)
        out["yes_sets"] = min(float(leg.get("ask_shares") or 0) for leg in legs)
    if all(leg.get("bid") is not None for leg in legs):
        out["sum_bid"] = round(sum(leg["bid"] for leg in legs), 4)
        # NO on leg i costs 1 - bid_i; its fee is on that NO price.
        fees = sum(fee_usd(1.0 - leg["bid"], 1) for leg in legs)
        out["no_profit"] = round((n - 1) - sum(1.0 - leg["bid"] for leg in legs) - fees, 4)
        out["no_sets"] = min(float(leg.get("bid_shares") or 0) for leg in legs)
    return out


# --------------------------------------------------------------------------
# scan
# --------------------------------------------------------------------------

async def scan(db: str = DB_PATH, tags: tuple[str, ...] = TAGS) -> list[dict[str, Any]]:
    from polymarket_us import AsyncPolymarketUS

    from adapters import QuoteFetcher

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    found = []
    async with AsyncPolymarketUS() as pm:
        quotes = QuoteFetcher(pm, concurrency=1)
        for tag in tags:
            page = await pm.events.list({"limit": 40, "closed": False, "tagSlug": tag})
            for ev in page.get("events", []) or []:
                markets = [m for m in ev.get("markets") or [] if not m.get("closed")]
                slugs = [m["slug"] for m in markets]
                if not complete_ladder(slugs):
                    log.info("skip %s: set not provably complete", ev.get("slug"))
                    continue
                legs = []
                for m in markets:
                    b = await quotes.bbo(m["slug"])
                    if b is None:
                        legs = []
                        break
                    legs.append({"slug": m["slug"], "title": m.get("title"),
                                 "bid": b.get("bid"), "ask": b.get("ask"),
                                 "bid_shares": b.get("bid_shares"), "ask_shares": b.get("ask_shares")})
                if not legs:
                    log.warning("skip %s: a leg could not be quoted", ev.get("slug"))
                    continue
                res = price_set(legs)
                row = {"at": now, "event_slug": ev.get("slug"), "title": ev.get("title"), **res,
                       "legs": legs}
                found.append(row)
                flag = ""
                if (res["yes_profit"] or -1) > 0 or (res["no_profit"] or -1) > 0:
                    flag = "   <-- ARB"
                log.info("%-34s legs=%d  sum_ask=%s sum_bid=%s  yes=%s no=%s%s",
                         (ev.get("title") or "")[:34], res["n_legs"], res["sum_ask"], res["sum_bid"],
                         res["yes_profit"], res["no_profit"], flag)
    with closing(connect(db)) as conn:
        conn.executemany(
            "INSERT INTO scans (at, event_slug, title, n_legs, sum_ask, sum_bid, yes_profit,"
            " no_profit, yes_sets, no_sets, legs_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [(r["at"], r["event_slug"], r["title"], r["n_legs"], r["sum_ask"], r["sum_bid"],
              r["yes_profit"], r["no_profit"], r["yes_sets"], r["no_sets"],
              json.dumps(r["legs"])) for r in found])
        conn.commit()
    return found


def report(db: str = DB_PATH) -> None:
    with closing(connect(db)) as conn:
        rows = conn.execute("SELECT * FROM scans").fetchall()
    arbs = [r for r in rows if (r["yes_profit"] or -1) > 0 or (r["no_profit"] or -1) > 0]
    print(f"{len(rows)} complete sets scanned across {len({r['at'] for r in rows})} scans; "
          f"{len(arbs)} priced to profit after fees")
    for r in arbs[-20:]:
        best = max((r["yes_profit"] or -1, "YES", r["yes_sets"]), (r["no_profit"] or -1, "NO", r["no_sets"]))
        print(f"  {r['at']}  {r['title'][:40]:<41} {best[1]} set +${best[0]:.3f} x {best[2]:.0f} sets")
    if rows:
        ya = [r["sum_ask"] for r in rows if r["sum_ask"] is not None]
        print(f"  typical sum of asks {sorted(ya)[len(ya) // 2]:.3f} (1.000 = no house margin)" if ya else "")


def main() -> None:
    ap = argparse.ArgumentParser(description="Complete-set arbitrage scanner. Shadow only.")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--tags", default=",".join(TAGS))
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if args.report:
        report()
        return
    asyncio.run(scan(tags=tuple(t.strip() for t in args.tags.split(","))))
    report()


if __name__ == "__main__":
    main()
