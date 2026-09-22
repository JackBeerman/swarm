"""
weather.py -- forecast-implied probability vs the market's price. Code only.

SHADOW ONLY. Places no orders.

The first weather paper run (2026-09-22) spent $0.20 on three gatherers
and Opus, and Tier 3 returned the market's own price on every market:
the gatherers searched for the actual temperature, which did not exist
yet, and fell back to climate normals. Nobody read the forecast. The
forecast is free, numeric, and published by the agency that settles the
market, so this category is arithmetic, not judgment: no LLM, no Jev.

For each open "Highest temperature in <city> on <date>" market:
  p_model = P(band | NWS forecast high, forecast error ~ Normal(0, sd))
and the record is (forecast, band, p_model, market mid). Outcomes arrive
the next day from the settlement endpoint, so calibration of BOTH the
model and the market accumulates daily, which no other category offers.

    python weather.py            # record today's comparison
    python weather.py --backfill # fill outcomes
    python weather.py --score    # model vs market, by lead time
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import os
import re
import sqlite3
import statistics
from contextlib import closing
from datetime import date, datetime, timezone
from typing import Any

import httpx

from adapters import listed_mid, normalize_market

log = logging.getLogger("weather")

DB_PATH = os.getenv("WEATHER_DB", "weather.db")

#: Exchange city slug -> the NWS station the market settles on.
STATIONS: dict[str, tuple[str, float, float]] = {
    "nychigh": ("KNYC Central Park", 40.7789, -73.9692),
    "laxhigh": ("KLAX", 33.9425, -118.4081),
    "sfohigh": ("KSFO", 37.6188, -122.3750),
    "miahigh": ("KMIA", 25.7959, -80.2870),
    "mdwhigh": ("KMDW Chicago Midway", 41.7868, -87.7522),
}

#: Forecast error SD in degrees F by lead (days ahead). Placeholders from
#: published NWS verification (day-1 MAE ~2 F); --score replaces them
#: with what this station actually did.
ERROR_SD = {0: 2.0, 1: 2.5, 2: 3.2, 3: 4.0}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS forecasts (
    id            INTEGER PRIMARY KEY,
    at            TEXT NOT NULL,
    market_slug   TEXT NOT NULL,
    city          TEXT NOT NULL,
    target_date   TEXT NOT NULL,
    lead_days     INTEGER NOT NULL,
    forecast_high REAL NOT NULL,
    band_lo       REAL,                 -- NULL = open below
    band_hi       REAL,                 -- NULL = open above
    p_model       REAL NOT NULL,
    market_mid    REAL,
    resolved_outcome TEXT,
    UNIQUE(market_slug, at)
);
"""


def connect(path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


# --------------------------------------------------------------------------
# the arithmetic
# --------------------------------------------------------------------------

_SLUG = re.compile(r"^tc-temp-(?P<city>[a-z]+)-(?P<date>\d{4}-\d{2}-\d{2})-(?P<band>.+)$")


def parse_slug(slug: str) -> dict[str, Any] | None:
    """tc-temp-nychigh-2026-09-21-gte66lt67f -> city, date, band [66, 67]."""
    m = _SLUG.match(slug or "")
    if not m:
        return None
    band = m["band"]
    g = re.search(r"gte(\d+)", band)
    lt = re.search(r"lt(\d+)", band)
    # Verified against displayed outcomes 2026-09-21: gte66lt67 is shown as
    # "66 to 67" (inclusive), lt66 as "65 or below", gte74 as "74 or above".
    lo = float(g.group(1)) if g else None
    if g and lt:
        hi: float | None = float(lt.group(1))
    elif lt:
        hi = float(lt.group(1)) - 1
    else:
        hi = None
    return {"city": m["city"], "date": m["date"], "lo": lo, "hi": hi}


def _phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def band_probability(forecast: float, lo: float | None, hi: float | None, sd: float) -> float:
    """P(observed integer high falls in [lo, hi]) with a normal error around the forecast."""
    upper = _phi((hi + 0.5 - forecast) / sd) if hi is not None else 1.0
    lower = _phi((lo - 0.5 - forecast) / sd) if lo is not None else 0.0
    return max(0.0, min(1.0, upper - lower))


# --------------------------------------------------------------------------
# NWS
# --------------------------------------------------------------------------

_NWS_HEADERS = {"User-Agent": "(swarm research; weather.py)", "Accept": "application/geo+json"}


async def forecast_highs(client: httpx.AsyncClient, lat: float, lon: float) -> dict[str, float]:
    """{YYYY-MM-DD: daytime high F} from the NWS period forecast."""
    p = (await client.get(f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}")).json()["properties"]
    periods = (await client.get(p["forecast"])).json()["properties"]["periods"]
    out: dict[str, float] = {}
    for per in periods:
        if per.get("isDaytime") and per.get("temperatureUnit") == "F":
            out.setdefault(per["startTime"][:10], float(per["temperature"]))
    return out


# --------------------------------------------------------------------------
# record
# --------------------------------------------------------------------------

async def record(db: str = DB_PATH) -> None:
    from polymarket_us import AsyncPolymarketUS

    now = datetime.now(timezone.utc)
    async with AsyncPolymarketUS() as pm, httpx.AsyncClient(
            timeout=20, headers=_NWS_HEADERS, follow_redirects=True) as client:
        page = await pm.events.list({"limit": 40, "closed": False, "tagSlug": "weather"})
        highs: dict[str, dict[str, float]] = {}
        rows = []
        for ev in page.get("events", []) or []:
            for market in ev.get("markets") or []:
                n = normalize_market(market, ev)
                info = parse_slug(n.get("slug") or "")
                if not info or info["city"] not in STATIONS:
                    continue
                name, lat, lon = STATIONS[info["city"]]
                if info["city"] not in highs:
                    try:
                        highs[info["city"]] = await forecast_highs(client, lat, lon)
                    except Exception as exc:  # noqa: BLE001 -- one station down must not stop the rest
                        log.warning("NWS failed for %s: %s", name, exc)
                        highs[info["city"]] = {}
                f = highs[info["city"]].get(info["date"])
                if f is None:
                    continue
                lead = (date.fromisoformat(info["date"]) - now.date()).days
                # Lead 0 is kept for the record but is not a fair test: by
                # afternoon the market sees the day's observations and this
                # model sees the morning forecast. --score reports it apart.
                sd = ERROR_SD.get(max(0, lead), ERROR_SD[3])
                p = band_probability(f, info["lo"], info["hi"], sd)
                rows.append((now.isoformat(), n["slug"], info["city"], info["date"], lead, f,
                             info["lo"], info["hi"], p, listed_mid(market)))
        with closing(connect(db)) as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO forecasts (at, market_slug, city, target_date, lead_days,"
                " forecast_high, band_lo, band_hi, p_model, market_mid)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
            conn.commit()
    print(f"recorded {len(rows)} markets across {len(highs)} stations")
    for r in sorted(rows, key=lambda r: (r[2], r[3], r[6] or -99)):
        band = f"{'' if r[6] is None else int(r[6])}..{'' if r[7] is None else int(r[7])}"
        gap = (r[8] - r[9]) if r[9] is not None else float("nan")
        flag = "  <--" if abs(gap) >= 0.15 else ""
        print(f"  {r[2]:<8} {r[3]} lead={r[4]} fcst={r[5]:.0f}F band {band:<8} "
              f"model={r[8]:.2f} market={r[9] if r[9] is not None else float('nan'):.2f} gap={gap:+.2f}{flag}")


async def backfill(db: str = DB_PATH, pause: float = 2.0) -> None:
    from polymarket_us import AsyncPolymarketUS

    with closing(connect(db)) as conn:
        slugs = [r[0] for r in conn.execute(
            "SELECT DISTINCT market_slug FROM forecasts WHERE resolved_outcome IS NULL")]
        filled = 0
        async with AsyncPolymarketUS() as pm:
            for slug in slugs:
                try:
                    res = await pm.markets.settlement(slug)
                except Exception:  # noqa: BLE001 -- NotFoundError means not settled yet
                    await asyncio.sleep(pause)
                    continue
                v = res.get("settlement")
                if v in (0, 1, "0", "1"):
                    conn.execute("UPDATE forecasts SET resolved_outcome=? WHERE market_slug=?",
                                 (str(int(v)), slug))
                    conn.commit()
                    filled += 1
                await asyncio.sleep(pause)
    print(f"filled {filled} of {len(slugs)}")


def score(db: str = DB_PATH) -> None:
    """Brier of the forecast model vs the market, by lead time. Unit = station-day."""
    with closing(connect(db)) as conn:
        rows = conn.execute(
            "SELECT * FROM forecasts WHERE resolved_outcome IS NOT NULL AND market_mid IS NOT NULL"
        ).fetchall()
    print("=" * 60)
    print(f"  {len(rows)} resolved weather markets, "
          f"{len({(r['city'], r['target_date']) for r in rows})} station-days")
    print("=" * 60)
    if not rows:
        print("\n  nothing resolved yet.")
        return
    print(f"\n  {'lead':>5}{'n':>5}{'days':>6}{'model brier':>13}{'market brier':>14}")
    for lead in sorted({r["lead_days"] for r in rows}):
        rs = [r for r in rows if r["lead_days"] == lead]
        y = [float(r["resolved_outcome"]) for r in rs]
        bm = statistics.mean((r["p_model"] - yy) ** 2 for r, yy in zip(rs, y, strict=True))
        bk = statistics.mean((r["market_mid"] - yy) ** 2 for r, yy in zip(rs, y, strict=True))
        days = len({(r["city"], r["target_date"]) for r in rs})
        print(f"  {lead:>5}{len(rs):>5}{days:>6}{bm:>13.4f}{bk:>14.4f}")
    print("\n  Station-days are the unit: every band of one city-day resolves together.")
    print("  Lead 0 is not a fair comparison: the market saw the day's observations.")


def main() -> None:
    ap = argparse.ArgumentParser(description="NWS forecast vs weather market prices. Shadow only.")
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--score", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if args.backfill:
        asyncio.run(backfill())
    elif args.score:
        score()
    else:
        asyncio.run(record())


if __name__ == "__main__":
    main()
