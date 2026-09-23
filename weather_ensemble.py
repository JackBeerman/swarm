"""
weather_ensemble.py -- band probabilities from ensemble forecasts. Code only.

SHADOW ONLY. Places no orders.

weather.py turns ONE NWS point forecast into band probabilities with an
assumed Normal error whose SD is a placeholder. An ensemble gives the
spread directly: N equally likely runs of a model, each with its own
daily high. Here

    p(band) = (count of members whose daily high lands in the band + alpha)
              / (N + alpha * K)          K = bands on the ladder

so no band is ever exactly 0 or 1. Optional Gaussian dressing (dress_sd)
spreads each member over neighbouring degrees; it is off by default and
`--sweep` refits it from stored member highs without any new calls.

The day boundary matters. The market settles on the NWS Daily Climate
Report, which covers midnight to midnight LOCAL STANDARD TIME all year:
during daylight time the climate day runs 01:00 to 00:59 the next day on
the wall clock. So the daily max is taken over the UTC hours of the LST
day, never over the civil (DST) day and never over Open-Meteo's own
`daily=temperature_2m_max`, which uses the civil day.

Sources sit behind one interface (EnsembleSource.fetch -> MemberSeries),
so a direct WeatherNext 3 feed (BigQuery / GCS, needs an approved Google
account) can be added later without touching the arithmetic. WeatherNext 2
is already available through Open-Meteo, free and keyless, as
`google_weathernext2_ensemble` (64 members).

The NWS-normal model from weather.py is recorded in the same table at the
same moment against the same market price, as source "nws_normal", so
every source is scored on identical markets.

    python weather_ensemble.py            # record all open weather ladders
    python weather_ensemble.py --backfill # fill outcomes (copies weather.py's first)
    python weather_ensemble.py --score    # Brier by source and lead, vs the market
    python weather_ensemble.py --sweep    # refit dressing SD from stored member highs

Known limits (measure, do not assume):
  * Member values are 0.25-degree grid cells, not the station thermometer.
  * Hourly values are interpolated from 3- or 6-hourly model steps, so the
    peak between steps is missed: expect a cold bias in the daily max.
  * Raw ensembles are usually under-dispersed near the surface at short
    lead. Both are what --score and --sweep are for.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import sqlite3
import statistics
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Protocol

import httpx

import weather
from adapters import listed_mid

log = logging.getLogger("weather_ensemble")

DB_PATH = weather.DB_PATH

#: UTC offset of LOCAL STANDARD TIME (hours) at each settlement station.
#: The climate day ignores daylight saving, so these never change.
STANDARD_UTC_OFFSET: dict[str, int] = {
    "nychigh": -5,   # Eastern
    "miahigh": -5,   # Eastern
    "mdwhigh": -6,   # Central
    "laxhigh": -8,   # Pacific
    "sfohigh": -8,   # Pacific
}

OPEN_METEO_ENSEMBLE = "https://ensemble-api.open-meteo.com/v1/ensemble"

#: Open-Meteo ensemble models; member counts verified against live
#: responses 2026-09-23 (control `temperature_2m` + `_memberNN`).
DEFAULT_MODELS: tuple[str, ...] = (
    "ecmwf_ifs025",                  # 51 members
    "gfs025",                        # 31 members
    "icon_seamless",                 # 40 members
    "google_weathernext2_ensemble",  # 64 members (WeatherNext 2, via Open-Meteo)
)

#: Pseudocount spread over the ladder: floor per band = alpha / (N + alpha*K).
ALPHA = 0.5

#: A member's daily high needs this many hourly values inside the LST day.
MIN_HOURS = 20


# --------------------------------------------------------------------------
# sources
# --------------------------------------------------------------------------

@dataclass
class MemberSeries:
    """Hourly member values on a shared UTC time axis."""
    times: list[datetime]                  # tz-aware UTC
    members: list[list[float | None]]      # members[i][t], degrees F
    meta: dict[str, Any] = field(default_factory=dict)


class EnsembleSource(Protocol):
    """Anything that can give per-member hourly 2 m temperature (F) at a point."""
    name: str

    async def fetch(self, client: httpx.AsyncClient, lat: float, lon: float,
                    days: int) -> MemberSeries: ...


def parse_open_meteo(payload: dict[str, Any], variable: str = "temperature_2m") -> MemberSeries:
    """
    Open-Meteo ensemble JSON -> MemberSeries.

    Shape (live, 2026-09-23): hourly = {"time": ["2026-09-23T00:00", ...],
    "temperature_2m": [...], "temperature_2m_member01": [...], ...}. The
    un-suffixed key is the control run and counts as a member. Times are
    wall-clock in the requested timezone; `utc_offset_seconds` converts.
    """
    if "error" in payload and payload.get("error"):
        raise ValueError(f"open-meteo error: {payload.get('reason')}")
    hourly = payload["hourly"]
    offset = timedelta(seconds=int(payload.get("utc_offset_seconds") or 0))
    times = [datetime.fromisoformat(t).replace(tzinfo=timezone.utc) - offset
             for t in hourly["time"]]
    unit = (payload.get("hourly_units") or {}).get(variable, "")
    keys = [k for k in hourly if k == variable or k.startswith(variable + "_member")]
    keys.sort(key=lambda k: 0 if k == variable else int(k.rsplit("member", 1)[1]))
    celsius = "C" in unit      # we ask for fahrenheit; convert if the API ignored it
    members = [[None if v is None else (float(v) * 9 / 5 + 32 if celsius else float(v))
                for v in hourly[k]] for k in keys]
    for m in members:
        if len(m) != len(times):
            raise ValueError("member series length does not match the time axis")
    return MemberSeries(times, members, {
        "grid_lat": payload.get("latitude"), "grid_lon": payload.get("longitude"),
        "elevation": payload.get("elevation"), "unit": unit})


@dataclass
class OpenMeteoEnsemble:
    """One Open-Meteo ensemble model. Free, keyless, non-commercial terms."""
    model: str

    @property
    def name(self) -> str:
        return f"om:{self.model}"

    async def fetch(self, client: httpx.AsyncClient, lat: float, lon: float,
                    days: int) -> MemberSeries:
        r = await client.get(OPEN_METEO_ENSEMBLE, params={
            "latitude": f"{lat:.4f}", "longitude": f"{lon:.4f}", "hourly": "temperature_2m",
            "temperature_unit": "fahrenheit", "timezone": "GMT",
            "forecast_days": days, "models": self.model})
        r.raise_for_status()
        return parse_open_meteo(r.json())


# --------------------------------------------------------------------------
# the arithmetic
# --------------------------------------------------------------------------

def climate_day_utc(city: str, day: str) -> tuple[datetime, datetime]:
    """[start, end) in UTC of the NWS climate day: local STANDARD midnight to midnight."""
    off = timedelta(hours=STANDARD_UTC_OFFSET[city])
    start = datetime.fromisoformat(day).replace(tzinfo=timezone.utc) - off
    return start, start + timedelta(days=1)


def local_standard_date(city: str, now: datetime) -> date:
    return (now.astimezone(timezone.utc) + timedelta(hours=STANDARD_UTC_OFFSET[city])).date()


def member_daily_max(series: MemberSeries, start: datetime, end: datetime,
                     min_hours: int = MIN_HOURS) -> list[float]:
    """Each member's max over [start, end). Members short of min_hours are dropped."""
    idx = [i for i, t in enumerate(series.times) if start <= t < end]
    out = []
    for m in series.members:
        vals = [m[i] for i in idx if m[i] is not None]
        if len(vals) >= min_hours:
            out.append(max(vals))
    return out


def reported_high(x: float) -> int:
    """The climate report's whole degree: round half up."""
    return math.floor(x + 0.5)


def in_band(v: int, lo: float | None, hi: float | None) -> bool:
    return (lo is None or v >= lo) and (hi is None or v <= hi)


def ensemble_band_probs(maxes: list[float], bands: list[tuple[float | None, float | None]],
                        alpha: float = ALPHA, dress_sd: float = 0.0,
                        bias: float = 0.0) -> list[float]:
    """
    Probability per band from member daily highs.

    dress_sd == 0: fraction of members whose reported (rounded) high is in
    the band. dress_sd > 0: each member becomes Normal(max + bias, dress_sd)
    integrated over the band's integer edges (weather.band_probability).
    Either way alpha pseudocounts are spread over the K bands.
    """
    n, k = len(maxes), len(bands)
    if n == 0 or k == 0:
        return [float("nan")] * k
    raw = []
    for lo, hi in bands:
        if dress_sd > 0:
            mass = sum(weather.band_probability(m + bias, lo, hi, dress_sd) for m in maxes)
        else:
            mass = float(sum(in_band(reported_high(m + bias), lo, hi) for m in maxes))
        raw.append(mass)
    return [(c + alpha) / (n + alpha * k) for c in raw]


def nws_normal_probs(forecast_high: float, lead: int,
                     bands: list[tuple[float | None, float | None]]) -> list[float]:
    sd = weather.ERROR_SD.get(max(0, lead), weather.ERROR_SD[3])
    return [weather.band_probability(forecast_high, lo, hi, sd) for lo, hi in bands]


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS band_forecasts (
    id            INTEGER PRIMARY KEY,
    at            TEXT NOT NULL,
    source        TEXT NOT NULL,        -- nws_normal | om:<model> | ...
    market_slug   TEXT NOT NULL,
    city          TEXT NOT NULL,
    target_date   TEXT NOT NULL,
    lead_days     INTEGER NOT NULL,     -- vs the station's local standard date
    band_lo       REAL,
    band_hi       REAL,
    p_model       REAL NOT NULL,
    market_mid    REAL,
    resolved_outcome TEXT,
    UNIQUE(source, market_slug, at)
);
CREATE INDEX IF NOT EXISTS idx_bf_slug ON band_forecasts(market_slug);
CREATE TABLE IF NOT EXISTS ensemble_runs (
    id            INTEGER PRIMARY KEY,
    at            TEXT NOT NULL,
    source        TEXT NOT NULL,
    city          TEXT NOT NULL,
    target_date   TEXT NOT NULL,
    lead_days     INTEGER NOT NULL,
    n_members     INTEGER NOT NULL,
    maxes_json    TEXT NOT NULL,        -- each member's LST-day max, F
    point         REAL,                 -- nws_normal: the forecast high
    meta_json     TEXT,
    UNIQUE(source, city, target_date, at)
);
"""


def connect(path: str = DB_PATH) -> sqlite3.Connection:
    """weather.db with weather.py's table and these two alongside it."""
    conn = weather.connect(path)
    conn.executescript(_SCHEMA)
    return conn


# --------------------------------------------------------------------------
# record
# --------------------------------------------------------------------------

@dataclass
class Ladder:
    city: str
    day: str
    slugs: list[str] = field(default_factory=list)
    bands: list[tuple[float | None, float | None]] = field(default_factory=list)
    mids: list[float | None] = field(default_factory=list)


def ladders_from_events(events: list[dict[str, Any]]) -> dict[tuple[str, str], Ladder]:
    """Group open weather-high markets by (city, date), bands sorted bottom to top."""
    out: dict[tuple[str, str], Ladder] = {}
    for ev in events:
        for m in ev.get("markets") or []:
            if m.get("closed"):
                continue
            info = weather.parse_slug(m.get("slug") or "")
            if not info or info["city"] not in STANDARD_UTC_OFFSET:
                continue
            lad = out.setdefault((info["city"], info["date"]), Ladder(info["city"], info["date"]))
            lad.slugs.append(m["slug"])
            lad.bands.append((info["lo"], info["hi"]))
            lad.mids.append(listed_mid(m))
    for lad in out.values():
        order = sorted(range(len(lad.bands)),
                       key=lambda i: -1e9 if lad.bands[i][0] is None else lad.bands[i][0])
        lad.slugs = [lad.slugs[i] for i in order]
        lad.bands = [lad.bands[i] for i in order]
        lad.mids = [lad.mids[i] for i in order]
    return out


def default_sources(models: tuple[str, ...] = DEFAULT_MODELS) -> list[EnsembleSource]:
    return [OpenMeteoEnsemble(m) for m in models]


async def compute(ladders: dict[tuple[str, str], Ladder], sources: list[EnsembleSource],
                  now: datetime, client: httpx.AsyncClient,
                  nws_client: httpx.AsyncClient | None = None,
                  pause: float = 0.5) -> tuple[list[tuple], list[tuple]]:
    """
    Fetch every source once per city and turn it into band rows.
    Returns (band_forecasts rows, ensemble_runs rows). No exchange calls.
    """
    at = now.isoformat()
    band_rows: list[tuple] = []
    run_rows: list[tuple] = []
    by_city: dict[str, list[Ladder]] = defaultdict(list)
    for lad in ladders.values():
        by_city[lad.city].append(lad)
    for city, lads in sorted(by_city.items()):
        _, lat, lon = weather.STATIONS[city]
        today = local_standard_date(city, now)
        lads = [lad for lad in lads if (date.fromisoformat(lad.day) - today).days >= 0]
        if not lads:
            continue
        last = max(date.fromisoformat(lad.day) for lad in lads)
        # +2: the LST day of the last date ends up to 8 h into the next UTC day.
        days = min(16, (last - now.astimezone(timezone.utc).date()).days + 2)

        results: dict[str, MemberSeries | None] = {}
        for src in sources:
            try:
                results[src.name] = await src.fetch(client, lat, lon, days)
            except Exception as exc:  # noqa: BLE001 -- one source down must not stop the rest
                log.warning("%s failed for %s: %s", src.name, city, exc)
                results[src.name] = None
            await asyncio.sleep(pause)
        nws: dict[str, float] = {}
        if nws_client is not None:
            try:
                nws = await weather.forecast_highs(nws_client, lat, lon)
            except Exception as exc:  # noqa: BLE001
                log.warning("NWS failed for %s: %s", city, exc)

        for lad in lads:
            lead = (date.fromisoformat(lad.day) - today).days
            start, end = climate_day_utc(city, lad.day)
            f = nws.get(lad.day)
            if f is not None:
                ps = nws_normal_probs(f, lead, lad.bands)
                run_rows.append((at, "nws_normal", city, lad.day, lead, 1, "[]", f, None))
                band_rows += _rows(at, "nws_normal", lad, lead, ps)
            for name, series in results.items():
                if series is None:
                    continue
                maxes = member_daily_max(series, start, end)
                if not maxes:
                    log.warning("%s: no member covers the %s %s climate day", name, city, lad.day)
                    continue
                ps = ensemble_band_probs(maxes, lad.bands)
                run_rows.append((at, name, city, lad.day, lead, len(maxes),
                                 json.dumps([round(m, 2) for m in maxes]), None,
                                 json.dumps(series.meta)))
                band_rows += _rows(at, name, lad, lead, ps)
    return band_rows, run_rows


def _rows(at: str, source: str, lad: Ladder, lead: int, ps: list[float]) -> list[tuple]:
    return [(at, source, slug, lad.city, lad.day, lead, lo, hi, p, mid)
            for slug, (lo, hi), p, mid in zip(lad.slugs, lad.bands, ps, lad.mids, strict=True)]


def store(conn: sqlite3.Connection, band_rows: list[tuple], run_rows: list[tuple]) -> None:
    conn.executemany(
        "INSERT OR IGNORE INTO band_forecasts (at, source, market_slug, city, target_date,"
        " lead_days, band_lo, band_hi, p_model, market_mid) VALUES (?,?,?,?,?,?,?,?,?,?)",
        band_rows)
    conn.executemany(
        "INSERT OR IGNORE INTO ensemble_runs (at, source, city, target_date, lead_days,"
        " n_members, maxes_json, point, meta_json) VALUES (?,?,?,?,?,?,?,?,?)", run_rows)
    conn.commit()


def print_table(band_rows: list[tuple], run_rows: list[tuple]) -> None:
    sources = sorted({r[1] for r in band_rows}, key=lambda s: (s != "nws_normal", s))
    p: dict[tuple[str, str], float] = {(r[2], r[1]): r[8] for r in band_rows}
    mid = {r[2]: r[9] for r in band_rows}
    meta = {(r[1], r[2], r[3]): r for r in run_rows}
    groups: dict[tuple[str, str], list[tuple]] = defaultdict(list)
    for r in band_rows:
        if r[1] == sources[0]:
            groups[(r[3], r[4])].append(r)
    short = {s: s.replace("om:", "").replace("google_weathernext2_ensemble", "wn2")
             .replace("_ifs025", "").replace("_seamless", "").replace("nws_normal", "nws")[:7]
             for s in sources}
    for (city, day), rows in sorted(groups.items()):
        lead = rows[0][5]
        hdr = []
        for s in sources:
            m = meta.get((s, city, day))
            if m is None:
                continue
            if s == "nws_normal":
                hdr.append(f"nws fcst {m[7]:.0f}F")
            else:
                mx = json.loads(m[6])
                hdr.append(f"{short[s]} n={m[5]} med={statistics.median(mx):.1f}")
        print(f"\n  {city} {day} lead={lead}   " + "; ".join(hdr))
        print("  " + f"{'band':<9}" + "".join(f"{short[s]:>8}" for s in sources) + f"{'market':>8}")
        for r in sorted(rows, key=lambda r: -1e9 if r[6] is None else r[6]):
            lo, hi = r[6], r[7]
            band = (f"<={hi:.0f}" if lo is None else f">={lo:.0f}" if hi is None
                    else f"{lo:.0f}-{hi:.0f}")
            cells = "".join(f"{p[(r[2], s)]:>8.2f}" if (r[2], s) in p else f"{'-':>8}"
                            for s in sources)
            mk = mid.get(r[2])
            print(f"  {band:<9}{cells}{mk if mk is None else f'{mk:.3f}':>8}")


async def record(db: str = DB_PATH, models: tuple[str, ...] = DEFAULT_MODELS,
                 with_nws: bool = True) -> None:
    from polymarket_us import AsyncPolymarketUS

    now = datetime.now(timezone.utc)
    async with AsyncPolymarketUS() as pm:
        # ONE exchange call: the listed prices come with the event list.
        page = await pm.events.list({"limit": 40, "closed": False, "tagSlug": "weather"})
    events = page.get("events", []) or []
    if len(events) >= 40:
        log.warning("event list hit the limit; some ladders may be missing")
    ladders = ladders_from_events(events)
    async with httpx.AsyncClient(timeout=30) as client, httpx.AsyncClient(
            timeout=20, headers=weather._NWS_HEADERS, follow_redirects=True) as nws_client:
        band_rows, run_rows = await compute(ladders, default_sources(models), now, client,
                                            nws_client if with_nws else None)
    with closing(connect(db)) as conn:
        store(conn, band_rows, run_rows)
    print(f"recorded {len(band_rows)} band forecasts, {len(ladders)} ladders, "
          f"{len({r[1] for r in band_rows})} sources at {now.isoformat(timespec='seconds')}")
    print_table(band_rows, run_rows)


# --------------------------------------------------------------------------
# outcomes and scoring
# --------------------------------------------------------------------------

def copy_known_outcomes(conn: sqlite3.Connection) -> int:
    """Outcomes weather.py's backfill already fetched cost no exchange call here."""
    n = conn.execute(
        "UPDATE band_forecasts SET resolved_outcome = (SELECT f.resolved_outcome FROM"
        " forecasts f WHERE f.market_slug = band_forecasts.market_slug AND"
        " f.resolved_outcome IS NOT NULL LIMIT 1) WHERE resolved_outcome IS NULL AND"
        " market_slug IN (SELECT market_slug FROM forecasts WHERE resolved_outcome IS NOT NULL)"
    ).rowcount
    conn.commit()
    return n


async def backfill(db: str = DB_PATH, pause: float = 2.0, max_calls: int = 20) -> None:
    """Copy outcomes weather.py already has; ask the exchange for at most max_calls more."""
    with closing(connect(db)) as conn:
        copied = copy_known_outcomes(conn)
        today = datetime.now(timezone.utc).date().isoformat()
        slugs = [r[0] for r in conn.execute(
            "SELECT DISTINCT market_slug FROM band_forecasts WHERE resolved_outcome IS NULL"
            " AND target_date < ? ORDER BY target_date", (today,))][:max_calls]
        filled = 0
        if slugs:
            from polymarket_us import AsyncPolymarketUS

            async with AsyncPolymarketUS() as pm:
                for slug in slugs:
                    try:
                        res = await pm.markets.settlement(slug)
                    except Exception:  # noqa: BLE001 -- NotFoundError means not settled yet
                        await asyncio.sleep(pause)
                        continue
                    v = res.get("settlement")
                    if v in (0, 1, "0", "1"):
                        conn.execute("UPDATE band_forecasts SET resolved_outcome=?"
                                     " WHERE market_slug=?", (str(int(v)), slug))
                        conn.commit()
                        filled += 1
                    await asyncio.sleep(pause)
    print(f"copied {copied} rows from weather.py's outcomes; settled {filled} of {len(slugs)} asked")


def brier_table(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Per (source, lead): Brier of the source and of the market on the SAME rows.
    Rows: dicts with source, lead_days, p_model, market_mid, resolved_outcome,
    city, target_date.
    """
    out = []
    keys = sorted({(r["source"], r["lead_days"]) for r in rows})
    for src, lead in keys:
        rs = [r for r in rows if r["source"] == src and r["lead_days"] == lead
              and r["market_mid"] is not None]
        if not rs:
            continue
        y = [float(r["resolved_outcome"]) for r in rs]
        out.append({
            "source": src, "lead": lead, "n": len(rs),
            "days": len({(r["city"], r["target_date"]) for r in rs}),
            "brier": statistics.mean((r["p_model"] - yy) ** 2 for r, yy in zip(rs, y, strict=True)),
            "market": statistics.mean((r["market_mid"] - yy) ** 2
                                      for r, yy in zip(rs, y, strict=True)),
        })
    return out


def paired(rows: list[dict[str, Any]]) -> dict[str, tuple[int, float]]:
    """Brier per source on markets EVERY source (and the market) priced at the same snapshot."""
    by_key: dict[tuple[str, str], dict[str, dict[str, Any]]] = defaultdict(dict)
    for r in rows:
        if r["market_mid"] is not None:
            by_key[(r["at"], r["market_slug"])][r["source"]] = r
    sources = sorted({r["source"] for r in rows})
    common = [v for v in by_key.values() if all(s in v for s in sources)]
    if not common:
        return {}
    out = {}
    for s in sources + ["market"]:
        errs = []
        for v in common:
            r = v[sources[0]]
            p = r["market_mid"] if s == "market" else v[s]["p_model"]
            errs.append((p - float(r["resolved_outcome"])) ** 2)
        out[s] = (len(common), statistics.mean(errs))
    return out


def _resolved(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM band_forecasts WHERE resolved_outcome IS NOT NULL")]
    # weather.py's own rows, scored for continuity. Its lead is on the UTC date.
    rows += [dict(r, source="nws_normal(weather.py)") for r in conn.execute(
        "SELECT * FROM forecasts WHERE resolved_outcome IS NOT NULL")]
    return rows


def score(db: str = DB_PATH) -> None:
    with closing(connect(db)) as conn:
        rows = _resolved(conn)
    print("=" * 72)
    print(f"  {len(rows)} resolved band forecasts, "
          f"{len({(r['city'], r['target_date']) for r in rows})} station-days")
    print("=" * 72)
    if not rows:
        print("\n  nothing resolved yet.")
        return
    print(f"\n  {'source':<30}{'lead':>5}{'n':>6}{'days':>6}{'brier':>9}{'market':>9}")
    for t in brier_table(rows):
        print(f"  {t['source']:<30}{t['lead']:>5}{t['n']:>6}{t['days']:>6}"
              f"{t['brier']:>9.4f}{t['market']:>9.4f}")
    new = [r for r in rows if r["source"] != "nws_normal(weather.py)"]
    for lead in sorted({r["lead_days"] for r in new}):
        pr = paired([r for r in new if r["lead_days"] == lead])
        if pr:
            n = next(iter(pr.values()))[0]
            print(f"\n  paired, lead {lead} ({n} markets every source priced at the same snapshot):")
            for s, (_, b) in sorted(pr.items(), key=lambda kv: kv[1][1]):
                print(f"    {s:<34}{b:.4f}")
    print("\n  Station-days are the unit: every band of one city-day resolves together.")
    print("  Lead 0 is not a fair comparison: the market saw the day's observations.")


def sweep(db: str = DB_PATH, sds: tuple[float, ...] = (0.0, 0.75, 1.0, 1.5, 2.0, 3.0),
          biases: tuple[float, ...] = (0.0, 1.0, 2.0)) -> None:
    """Refit dressing SD and warm bias per source from stored member highs. No calls."""
    with closing(connect(db)) as conn:
        runs = [dict(r) for r in conn.execute("SELECT * FROM ensemble_runs WHERE n_members > 1")]
        bands = defaultdict(list)
        for r in conn.execute("SELECT at, source, city, target_date, band_lo, band_hi,"
                              " resolved_outcome FROM band_forecasts"
                              " WHERE resolved_outcome IS NOT NULL"):
            bands[(r[0], r[1], r[2], r[3])].append((r[4], r[5], float(r[6])))
    res: dict[tuple[str, float, float], list[float]] = defaultdict(list)
    for run in runs:
        lad = bands.get((run["at"], run["source"], run["city"], run["target_date"]))
        if not lad:
            continue
        maxes = json.loads(run["maxes_json"])
        for sd in sds:
            for b in biases:
                ps = ensemble_band_probs(maxes, [(lo, hi) for lo, hi, _ in lad], dress_sd=sd, bias=b)
                res[(run["source"], sd, b)] += [(p - y) ** 2 for p, (_, _, y) in zip(ps, lad, strict=True)]
    if not res:
        print("nothing resolved with stored member highs yet.")
        return
    print(f"  {'source':<36}{'dress_sd':>9}{'bias':>6}{'n':>6}{'brier':>9}")
    for (src, sd, b), errs in sorted(res.items()):
        print(f"  {src:<36}{sd:>9.2f}{b:>6.1f}{len(errs):>6}{statistics.mean(errs):>9.4f}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Ensemble weather probabilities vs the market. Shadow only.")
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--score", action="store_true")
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--models", default=",".join(DEFAULT_MODELS))
    ap.add_argument("--no-nws", action="store_true", help="skip the NWS-normal source")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if args.backfill:
        asyncio.run(backfill())
    elif args.score:
        score()
    elif args.sweep:
        sweep()
    else:
        models = tuple(m.strip() for m in args.models.split(",") if m.strip())
        asyncio.run(record(models=models, with_nws=not args.no_nws))


if __name__ == "__main__":
    main()
