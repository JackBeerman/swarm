"""
measure_sources.py -- how far behind the world is each source?

Polls every source in sources.py (and the fast lane's existing RSS feeds,
as the baseline to beat) for N minutes, each at its own polite interval,
and records every item's publish time against the moment we first saw it
into sources.db. Then reports, per source: items/hour, median / p90 /
fastest lag, and how fresh the newest item was.

Reads public data only. No exchange calls, no model calls, no keys.

    python tools/measure_sources.py --minutes 20            # measure, then report
    python tools/measure_sources.py --report                # report every run in sources.db
    python tools/measure_sources.py --report --run 3        # one run

Method notes, because the numbers are easy to misread:
  * Items present on a source's FIRST successful poll are the baseline:
    stored (their age measures freshness) but excluded from lag. Their
    "lag" would only measure when this script started.
  * Lag = first seen - published_at. For MLB game events published_at is
    the event's own startTime on the field, so lag there is the whole
    pipeline from the field to us. Resolution is the poll interval.
  * Google News queries (opt-in, --google-news; its robots.txt disallows
    /rss/) use `when:1h`, so their lag is censored at 60 min.
  * Sources with no publish time (MLB transactions, schedule status) count
    toward items/hour only.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import pathlib
import sqlite3
import statistics
import sys
import threading
import time
from contextlib import closing
from datetime import datetime, timezone
from typing import Any

import httpx

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import sources as src  # noqa: E402

log = logging.getLogger("measure_sources")

DB_PATH = str(ROOT / "sources.db")
CAP_OVERHEAD_S = 120        # hard wall-clock cap = minutes + this

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY,
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    minutes     REAL,
    note        TEXT
);
CREATE TABLE IF NOT EXISTS polls (
    id       INTEGER PRIMARY KEY,
    run_id   INTEGER NOT NULL,
    at       TEXT NOT NULL,
    feed     TEXT NOT NULL,
    ok       INTEGER NOT NULL,
    n_items  INTEGER,
    ms       REAL,
    bytes    INTEGER
);
CREATE TABLE IF NOT EXISTS items (
    id            INTEGER PRIMARY KEY,
    run_id        INTEGER NOT NULL,
    feed          TEXT NOT NULL,       -- the Source polled
    source        TEXT NOT NULL,       -- the item's own source label
    kind          TEXT,
    url           TEXT,
    title         TEXT,
    published_at  TEXT,
    first_seen_at TEXT NOT NULL,
    lag_seconds   REAL,                -- first seen - published
    baseline      INTEGER NOT NULL     -- present on the feed's first poll
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_items_ident ON items(run_id, feed, url, title);
"""


def connect(path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def record_items(conn: sqlite3.Connection, run_id: int, feed: str, items: list[dict[str, Any]],
                 baseline: bool, seen_at: datetime | None = None) -> int:
    """Insert new items; returns how many were new. Identity is (feed, url, title)."""
    seen_at = seen_at or datetime.now(timezone.utc)
    new = 0
    for it in items:
        lag = None
        if it.get("published_at"):
            lag = (seen_at - datetime.fromisoformat(it["published_at"])).total_seconds()
        cur = conn.execute(
            "INSERT OR IGNORE INTO items (run_id, feed, source, kind, url, title, published_at,"
            " first_seen_at, lag_seconds, baseline) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (run_id, feed, it["source"], it.get("kind"), it["url"], it["title"],
             it.get("published_at"), seen_at.isoformat(), lag, int(baseline)))
        new += cur.rowcount
    conn.commit()
    return new


def _pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))]


def summarize(conn: sqlite3.Connection, run_id: int | None = None) -> list[dict[str, Any]]:
    """Per feed: polls, errors, new items, items/hour, lag stats, freshness at start."""
    where, args = ("WHERE run_id = ?", (run_id,)) if run_id else ("", ())
    runs = {r["id"]: r for r in conn.execute(f"SELECT * FROM runs {where.replace('run_id', 'id')}", args)}  # noqa: S608
    hours: dict[int, float] = {}
    for rid, r in runs.items():
        # A run cut by the hard cap has no ended_at; its last poll ends it.
        end = r["ended_at"] or conn.execute(
            "SELECT MAX(at) FROM polls WHERE run_id = ?", (rid,)).fetchone()[0]
        if end:
            hours[rid] = (datetime.fromisoformat(end)
                          - datetime.fromisoformat(r["started_at"])).total_seconds() / 3600
    out: dict[str, dict[str, Any]] = {}
    for p in conn.execute(f"SELECT feed, run_id, ok FROM polls {where}", args):  # noqa: S608
        s = out.setdefault(p["feed"], {"feed": p["feed"], "polls": 0, "errors": 0, "runs": set(),
                                       "new": 0, "lags": [], "base_ages": []})
        s["polls"] += 1
        s["errors"] += 0 if p["ok"] else 1
        s["runs"].add(p["run_id"])
    for it in conn.execute(f"SELECT * FROM items {where}", args):  # noqa: S608
        s = out.get(it["feed"])
        if s is None:
            continue
        if it["baseline"]:
            if it["lag_seconds"] is not None:
                s["base_ages"].append(it["lag_seconds"])
            continue
        s["new"] += 1
        if it["lag_seconds"] is not None:
            s["lags"].append(it["lag_seconds"])
    rows = []
    for s in out.values():
        h = sum(hours.get(r, 0.0) for r in s["runs"])
        lags = s["lags"]
        rows.append({
            "feed": s["feed"], "polls": s["polls"], "errors": s["errors"], "new": s["new"],
            "per_hour": (s["new"] / h) if h else None,
            "n_lag": len(lags),
            "median": statistics.median(lags) if lags else None,
            "p90": _pct(lags, 0.9) if lags else None,
            "fastest": min(lags) if lags else None,
            "under_2m": (sum(1 for x in lags if x <= 120) / len(lags)) if lags else None,
            "newest_at_start": min((a for a in s["base_ages"] if a >= -60), default=None),
        })
    return sorted(rows, key=lambda r: (r["median"] is None, r["median"] or 0, r["feed"]))


def _fmt_s(x: float | None) -> str:
    if x is None:
        return "--"
    return f"{x:.0f}s" if abs(x) < 120 else f"{x / 60:.1f}m"


def report(db: str = DB_PATH, run_id: int | None = None) -> None:
    with closing(connect(db)) as conn:
        rows = summarize(conn, run_id)
        runs = conn.execute("SELECT * FROM runs ORDER BY id").fetchall()
    for r in runs:
        if run_id is None or r["id"] == run_id:
            print(f"run {r['id']}: {r['started_at'][:19]}Z  {r['minutes']} min  {r['note'] or ''}")
    print(f"\n  {'feed':<34}{'polls':>6}{'err':>5}{'new':>5}{'/hour':>7}{'n_lag':>6}"
          f"{'median':>8}{'p90':>8}{'fastest':>9}{'<=2m':>6}{'fresh@0':>9}")
    for r in rows:
        ph = f"{r['per_hour']:.1f}" if r["per_hour"] is not None else "--"
        u2 = f"{r['under_2m']:.0%}" if r["under_2m"] is not None else "--"
        print(f"  {r['feed'][:33]:<34}{r['polls']:>6}{r['errors']:>5}{r['new']:>5}{ph:>7}"
              f"{r['n_lag']:>6}{_fmt_s(r['median']):>8}{_fmt_s(r['p90']):>8}"
              f"{_fmt_s(r['fastest']):>9}{u2:>6}{_fmt_s(r['newest_at_start']):>9}")
    print("\n  lag = first seen - published (new items only). fresh@0 = age of the newest item"
          "\n  on the first poll. Resolution is each feed's poll interval (30-300 s).")


# --------------------------------------------------------------------------
# the set of sources measured
# --------------------------------------------------------------------------

async def build_sources(client: httpx.AsyncClient, which: set[str],
                        google_news: bool = False) -> list[src.Source]:
    from fastlane import FEEDS_BY_TAG

    out: list[src.Source] = []
    if "mlb" in which:
        out.append(src.MLBLiveSource())
        out.append(src.MLBTransactionsSource())
        if google_news:
            # One query per game on today's schedule (not final). Explicit
            # local date: before ~10 ET statsapi's default "today" is still
            # yesterday's (all final) slate.
            r = await client.get(f"{src.MLB_API}/v1/schedule",
                                 params={"sportId": 1, "hydrate": "team",
                                         "date": datetime.now().date().isoformat()},
                                 headers={"User-Agent": src.USER_AGENT})
            games = [g for g in src.parse_schedule(r.json()) if g["state"] != "Final"]
            labels = []                  # a doubleheader is one query, not two
            for g in games:
                label = f"{g['away']['short']} at {g['home']['short']}"
                if label not in labels:
                    labels.append(label)
            for label in labels[:8]:
                out.append(src.GoogleNewsSource(label.replace(" at ", " "), label=label))
        out.append(src.BlueskyAuthorSource(src.BSKY_HANDLES["mlb"], name="bluesky_mlb"))
        out.extend(src.RSSSource(f"rss:{n}", u) for n, u in FEEDS_BY_TAG["mlb"].items())
    if "nfl" in which:
        if google_news:
            for q in ("NFL injury", "NFL ruled out"):
                out.append(src.GoogleNewsSource(q))
        out.append(src.BlueskyAuthorSource(src.BSKY_HANDLES["nfl"], name="bluesky_nfl"))
        out.extend(src.RSSSource(f"rss:{n}", u) for n, u in FEEDS_BY_TAG["nfl"].items())
    if "weather" in which:
        out.append(src.NWSObservationSource(src.NWS_STATIONS))
        out.extend(src.NWSAlertsSource(a) for a in ("FL", "TX", "CA", "NY", "IL"))
    if "tech" in which:
        out.extend(src.RSSSource(f"status:{n}", u, min_interval=120, kind="status")
                   for n, u in src.STATUS_FEEDS.items())
        out.append(src.GitHubReleasesSource("openai/openai-python"))
    return out


def _keep_awake() -> None:
    """
    Windows only: ask the OS not to sleep while this process runs. The
    request is per-thread and released when the process exits. Found
    2026-09-23: a smoke run stalled for 8 minutes mid-poll because the
    machine slept, and the numbers from a sleeping machine are fiction.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)  # CONTINUOUS|SYSTEM
    except Exception:  # noqa: BLE001 -- best effort; the gap detector still reports sleep
        pass


async def _poll_loop(s: src.Source, client: httpx.AsyncClient, conn: sqlite3.Connection,
                     run_id: int, deadline: float, gaps: list[float]) -> None:
    """One source, its own cadence: a slow source never delays another."""
    while time.monotonic() < deadline:
        t0 = time.monotonic()
        before = s.polls
        try:
            items = await asyncio.wait_for(s.poll(client), timeout=90)
        except asyncio.TimeoutError:
            items = []
            log.warning("%s: poll timed out", s.name)
        if s.polls != before:
            ok = s.last_ok
            conn.execute("INSERT INTO polls (run_id, at, feed, ok, n_items, ms, bytes)"
                         " VALUES (?,?,?,?,?,?,?)",
                         (run_id, datetime.now(timezone.utc).isoformat(), s.name, int(ok),
                          len(items), s.last_ms, s.last_bytes))
            new = record_items(conn, run_id, s.name, items, baseline=(s.ok_polls == 1 and ok))
            if new and s.ok_polls > 1:
                log.info("%-30s +%d new", s.name[:30], new)
        wait = max(1.0, s.min_interval - (time.monotonic() - t0))
        t1 = time.time()
        await asyncio.sleep(min(wait, max(0.0, deadline - time.monotonic())))
        overslept = time.time() - t1 - wait
        if overslept > 30:
            gaps.append(overslept)            # wall clock jumped: the machine slept


async def measure(minutes: float, which: set[str], db: str = DB_PATH, note: str = "",
                  google_news: bool = False) -> int:
    started = datetime.now(timezone.utc)
    with closing(connect(db)) as conn:
        run_id = conn.execute("INSERT INTO runs (started_at, minutes, note) VALUES (?,?,?)",
                              (started.isoformat(), minutes, note)).lastrowid
        conn.commit()
        gaps: list[float] = []
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            srcs = await build_sources(client, which, google_news)
            log.info("run %d: %d sources for %.0f min: %s", run_id, len(srcs), minutes,
                     ", ".join(s.name for s in srcs))
            deadline = time.monotonic() + minutes * 60
            await asyncio.gather(*(_poll_loop(s, client, conn, run_id, deadline, gaps)
                                   for s in srcs))
        if gaps:
            note = f"{note} [clock gaps {', '.join(f'{g:.0f}s' for g in gaps)}: machine slept?]"
            log.warning("wall-clock gaps during the run: %s", gaps)
        conn.execute("UPDATE runs SET ended_at=?, note=? WHERE id=?",
                     (datetime.now(timezone.utc).isoformat(), note.strip(), run_id))
        conn.commit()
    return run_id


def main() -> None:
    ap = argparse.ArgumentParser(description="Measure publication-to-availability lag per source.")
    ap.add_argument("--minutes", type=float, default=20.0)
    ap.add_argument("--set", default="mlb,nfl,weather,tech",
                    help="comma list of: mlb, nfl, weather, tech")
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--note", default="")
    ap.add_argument("--google-news", action="store_true",
                    help="also poll Google News RSS search. OFF by default: news.google.com"
                         "/robots.txt disallows /rss/ -- an operator decision (see sources.py)")
    ap.add_argument("--report", action="store_true", help="report only, no polling")
    ap.add_argument("--run", type=int, default=None, help="report one run id")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if args.report:
        report(args.db, args.run)
        return
    which = {w.strip() for w in args.set.split(",") if w.strip()}
    cap = args.minutes * 60 + CAP_OVERHEAD_S
    _keep_awake()
    # Belt and braces: asyncio.run() waits for executor threads (a DNS
    # lookup stuck across a sleep) after wait_for cancels, so a thread
    # timer hard-exits if the cap is overrun by a minute.
    watchdog = threading.Timer(cap + 60, lambda: os._exit(4))
    watchdog.daemon = True
    watchdog.start()
    try:
        run_id = asyncio.run(asyncio.wait_for(measure(args.minutes, which, args.db, args.note,
                                                      args.google_news),
                                              timeout=cap))
    except asyncio.TimeoutError:
        log.error("hard cap of %.0f min reached; rows already written are kept", cap / 60)
        run_id = None
    report(args.db, run_id)


if __name__ == "__main__":
    main()
