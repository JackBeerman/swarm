"""
closer.py -- closing line value: did we get a better price than the close?

SHADOW ONLY. Places no orders; reads public quotes only.

Win/loss needs hundreds of bets to separate skill from luck. Closing line
value (CLV) -- entry price against the market's last price before the
event starts -- has far less variance and says something after dozens.
This file captures the close and scores every entry against it.

    python closer.py --capture             # one pass; schedule every ~10 min
    python closer.py --report              # CLV by lane and source, by EVENT

THE CLOSE. For a game it is the last quote before `event_at` (kickoff).
For weather `event_at` is the END of the observation day (adapters.py),
and by then the market has watched the thermometer all day: the line
closes when the observation day STARTS, so `line_close_time()` uses
`starts_at` for the climate category. A quote taken after the close is an
in-play price, which is a different market. It is never stored as a
close: a pass that finds a tracked market already started writes a
`missed` row with no price, and the report excludes it. Label, exclude,
never mix.

CLV is on the YES scale, in probability points:
    YES entry at ask a:          clv = close_mid - a
    NO entry at bid b (costs 1-b): clv = b - close_mid
                                     = (1 - close_mid) - (1 - b)
so positive always means we bought cheaper than the close.

Markets in one event move together, so the unit is the EVENT: bets are
averaged within an event first, and the bootstrap resamples events.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import re
import sqlite3
import statistics
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from adapters import game_state, normalize_market

log = logging.getLogger("closer")

DB_PATH = os.getenv("CLOSES_DB", "closes.db")

#: Where the evaluation databases live. Read-only here, always.
SOURCE_DBS = {
    "shadow": os.getenv("SHADOW_DB", "shadow.db"),
    "traces": os.getenv("TRACES_DB", "traces.db"),
    "fastlane": os.getenv("FASTLANE_DB", "fastlane.db"),
    "weather": os.getenv("WEATHER_DB", "weather.db"),
}

WINDOW_MIN = 12.0          # quote markets whose line closes within this many minutes
LOOKBACK_H = 6.0           # also list events that started this recently, to mark misses
MAX_CALLS = 20             # exchange calls per pass, listing included
CALL_SPACING_S = 2.0       # a live recorder shares Cloudflare's rate limit
PASS_CAP_S = 300.0         # hard wall-clock cap on one pass
LIST_LIMIT = 100           # events per listing page

_SCHEMA = """
CREATE TABLE IF NOT EXISTS closes (
    market_slug          TEXT PRIMARY KEY,
    event_slug           TEXT,
    status               TEXT NOT NULL,   -- pre_start | missed (missed carries no price)
    closed_at            TEXT,            -- when the line closed: event start
                                          -- (weather: start of the observation day)
    bid                  REAL,
    ask                  REAL,
    captured_at          TEXT NOT NULL,   -- when the quote (or the miss) was recorded
    minutes_before_start REAL,            -- closed_at - captured_at; NULL for a miss
    sources              TEXT             -- which databases tracked the market
);
"""


def connect(path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def _ro(path: str) -> sqlite3.Connection | None:
    """Open a source database read-only, or None if it does not exist."""
    if not os.path.exists(path):
        return None
    conn = sqlite3.connect(f"file:{os.path.abspath(path)}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _ts(s: Any) -> datetime | None:
    """ISO string (with Z or offset) -> aware UTC datetime, or None."""
    if not s or not isinstance(s, str):
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


_EVENT_RE = re.compile(r"([a-z0-9]+-[a-z0-9]+-[a-z0-9]+-\d{4}-\d{2}-\d{2})")


def event_key(market_slug: str, event_slug: str | None = None) -> str:
    """The exchange's event slug when known; the shadow.py slug regex otherwise."""
    if event_slug:
        return event_slug
    m = _EVENT_RE.search(market_slug or "")
    return m.group(1) if m else (market_slug or "").rsplit("-", 2)[0]


# --------------------------------------------------------------------------
# when does the line close?
# --------------------------------------------------------------------------

def line_close_time(norm: dict[str, Any]) -> datetime | None:
    """
    The moment after which a quote is in-play, from a normalize_market dict.

    Climate: `event_at` is the window END; the market reads observations
    all day, so the line closes at `starts_at`. Everything else: event_at.
    """
    if norm.get("category") == "climate":
        return _ts(norm.get("starts_at")) or _ts(norm.get("event_at"))
    return _ts(norm.get("event_at"))


def classify_capture(close_at: datetime | None, now: datetime, state: str,
                     window_min: float = WINDOW_MIN) -> str:
    """
    What a pass should do with one tracked market:
      'quote'  -- line closes within the window and play has not begun
      'missed' -- the line already closed (or the game is live): record no price
      'wait'   -- too early, or no start time known
    """
    if state in ("in_play", "finished"):
        return "missed"
    if close_at is None:
        return "wait"
    if close_at <= now:
        return "missed"
    if close_at - now <= timedelta(minutes=window_min):
        return "quote"
    return "wait"


def close_row(slug: str, event_slug: str | None, close_at: datetime | None,
              bbo: dict[str, Any] | None, captured_at: datetime,
              sources: Iterable[str]) -> dict[str, Any]:
    """
    One closes row. A quote that finished at or after the close is an
    in-play price and is written as `missed` with no bid/ask -- a slow
    quote must not smuggle an in-play price into the closing line.
    """
    base = {"market_slug": slug, "event_slug": event_slug,
            "closed_at": close_at.isoformat() if close_at else None,
            "captured_at": captured_at.isoformat(), "sources": ",".join(sorted(set(sources)))}
    ok = (bbo is not None and close_at is not None and captured_at < close_at
          and bbo.get("bid") is not None and bbo.get("ask") is not None)
    if not ok:
        return {**base, "status": "missed", "bid": None, "ask": None,
                "minutes_before_start": None}
    return {**base, "status": "pre_start", "bid": float(bbo["bid"]), "ask": float(bbo["ask"]),
            "minutes_before_start": round((close_at - captured_at).total_seconds() / 60, 2)}


def store_close(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    """
    Insert once. A pre_start close is never overwritten by anything; a
    `missed` row may be upgraded by a later pre_start quote, because the
    fallback marks misses from a stored start hint, and a postponed game
    must not be locked out of its real close by a stale one.
    """
    conn.execute(
        "INSERT INTO closes (market_slug, event_slug, status, closed_at, bid, ask,"
        " captured_at, minutes_before_start, sources) VALUES (?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(market_slug) DO UPDATE SET event_slug=excluded.event_slug,"
        " status=excluded.status, closed_at=excluded.closed_at, bid=excluded.bid,"
        " ask=excluded.ask, captured_at=excluded.captured_at,"
        " minutes_before_start=excluded.minutes_before_start, sources=excluded.sources"
        " WHERE closes.status = 'missed' AND excluded.status = 'pre_start'",
        (row["market_slug"], row["event_slug"], row["status"], row["closed_at"], row["bid"],
         row["ask"], row["captured_at"], row["minutes_before_start"], row["sources"]))
    conn.commit()


# --------------------------------------------------------------------------
# what we are tracking
# --------------------------------------------------------------------------

def tracked_markets(dbs: dict[str, str] | None = None) -> dict[str, dict[str, Any]]:
    """
    slug -> {"event_slug", "sources", "start_hint"} over every market any
    lane evaluated or signalled. `start_hint` is only known for shadow
    rows (seen_at + hours_to_event) and is used to mark misses.
    """
    dbs = dbs or SOURCE_DBS
    out: dict[str, dict[str, Any]] = {}

    def add(slug, ev, source, hint=None):
        if not slug:
            return
        t = out.setdefault(slug, {"event_slug": None, "sources": set(), "start_hint": None})
        t["event_slug"] = t["event_slug"] or ev
        t["sources"].add(source)
        if hint is not None and t["start_hint"] is None:
            t["start_hint"] = hint

    s = _ro(dbs["shadow"])
    if s:
        with closing(s):
            for r in s.execute("SELECT market_slug, event_slug, seen_at, hours_to_event FROM verdicts"):
                seen = _ts(r["seen_at"])
                hint = (seen + timedelta(hours=r["hours_to_event"])
                        if seen and r["hours_to_event"] is not None else None)
                add(r["market_slug"], r["event_slug"], "shadow", hint)
    t = _ro(dbs["traces"])
    if t:
        with closing(t):
            for r in t.execute("SELECT market_slug, event_slug FROM evaluations"):
                add(r["market_slug"], r["event_slug"], "traces")
    f = _ro(dbs["fastlane"])
    if f:
        with closing(f):
            for r in f.execute("SELECT DISTINCT market_slug, event_slug FROM signals"):
                add(r["market_slug"], r["event_slug"], "fastlane")
    w = _ro(dbs["weather"])
    if w:
        with closing(w):
            for r in w.execute("SELECT DISTINCT market_slug FROM forecasts"):
                add(r["market_slug"], None, "weather")
    return out


# --------------------------------------------------------------------------
# the capture pass
# --------------------------------------------------------------------------

async def capture(db: str = DB_PATH, window_min: float = WINDOW_MIN,
                  lookback_h: float = LOOKBACK_H, max_calls: int = MAX_CALLS,
                  tags: tuple[str, ...] = (), pm: Any = None,
                  dbs: dict[str, str] | None = None) -> dict[str, int]:
    """
    One pass. Lists events starting in [now, now + window] (paged, per tag
    or untagged), then quotes each TRACKED market whose line closes within
    the window, once. With budget to spare, one page of events that started
    in the last `lookback_h` marks tracked markets there `missed`, no price.

    Verified live 2026-09-23: startTimeMin/Max is honoured without a tag
    (40 events in a 12-minute window) and `offset` pages (159 events over
    6 h at limit 100). Every exchange call is paced CALL_SPACING_S apart
    and counted against `max_calls`; quoting stops when the budget is spent.
    """
    from adapters import QuoteFetcher

    stats = {"events": 0, "tracked_in_window": 0, "quoted": 0, "missed": 0,
             "budget_skipped": 0, "quote_failed": 0, "calls": 0}
    tracked = tracked_markets(dbs)
    with closing(connect(db)) as conn:
        have = {r[0] for r in conn.execute("SELECT market_slug FROM closes")}
        done = {r[0] for r in conn.execute(
            "SELECT market_slug FROM closes WHERE status = 'pre_start'")}
        now = datetime.now(timezone.utc)

        async def run(pm):
            fmt = "%Y-%m-%dT%H:%M:%SZ"
            events: dict[str, dict] = {}

            async def listing(lo: datetime, hi: datetime, max_pages: int) -> None:
                for tag in (tags or (None,)):
                    for page_no in range(max_pages):
                        if stats["calls"] >= max_calls:
                            return
                        p = {"closed": False, "limit": LIST_LIMIT, "offset": page_no * LIST_LIMIT,
                             "startTimeMin": lo.strftime(fmt), "startTimeMax": hi.strftime(fmt),
                             **({"tagSlug": tag} if tag else {})}
                        stats["calls"] += 1
                        page = (await pm.events.list(p)).get("events", []) or []
                        for ev in page:
                            events[ev.get("slug") or str(id(ev))] = ev
                        await asyncio.sleep(CALL_SPACING_S)
                        if len(page) < LIST_LIMIT:
                            break

            # Upcoming first: that is where prices are captured. Misses are
            # bookkeeping and get whatever budget is left, one page.
            await listing(now, now + timedelta(minutes=window_min), max_pages=3)
            if stats["calls"] < max_calls // 2:
                await listing(now - timedelta(hours=lookback_h), now, max_pages=1)
            stats["events"] = len(events)
            # No retries: every attempt is a budgeted call. A failed quote is
            # not a miss -- the market is left for the next pass.
            quotes = QuoteFetcher(pm, concurrency=1, min_interval=CALL_SPACING_S, max_retries=0)
            todo = []
            for ev in events.values():
                for m in ev.get("markets") or []:
                    slug = m.get("slug")
                    if slug not in tracked or slug in done:
                        continue
                    norm = normalize_market(m, ev)
                    close_at = line_close_time(norm)
                    action = classify_capture(close_at, datetime.now(timezone.utc),
                                              game_state(norm), window_min)
                    if action == "wait" or (action == "missed" and slug in have):
                        continue
                    stats["tracked_in_window"] += 1
                    todo.append((action, slug, norm.get("event_slug"), close_at))
            # Soonest first: if the budget runs out, the late ones get the next pass.
            todo.sort(key=lambda x: x[3] or now)
            for action, slug, ev_slug, close_at in todo:
                srcs = tracked[slug]["sources"]
                bbo = None
                if action == "quote":
                    if stats["calls"] + quotes.attempts >= max_calls:
                        stats["budget_skipped"] += 1
                        continue
                    bbo = await quotes.bbo(slug)
                    if bbo is None:
                        stats["quote_failed"] += 1
                        continue
                row = close_row(slug, ev_slug or tracked[slug]["event_slug"], close_at, bbo,
                                datetime.now(timezone.utc), srcs)
                store_close(conn, row)
                have.add(slug)
                stats["quoted" if row["status"] == "pre_start" else "missed"] += 1
            stats["calls"] += quotes.attempts

        if pm is None:
            from polymarket_us import AsyncPolymarketUS
            async with AsyncPolymarketUS() as client:   # public endpoints, no keys
                await run(client)
        else:
            await run(pm)

        # Fallback without the exchange: a shadow row whose recorded start
        # has passed and never got a close is a miss. Write the label so
        # the report can say so, with no price.
        now = datetime.now(timezone.utc)
        for slug, t in tracked.items():
            if slug in have or t["start_hint"] is None or t["start_hint"] > now:
                continue
            store_close(conn, close_row(slug, t["event_slug"], t["start_hint"], None, now,
                                        t["sources"]))
            have.add(slug)
            stats["missed"] += 1
    return stats


# --------------------------------------------------------------------------
# CLV
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Bet:
    lane: str            # slow | fast | weather
    source: str          # finer grouping inside a lane
    market_slug: str
    event_slug: str
    side: str            # YES | NO
    entry_yes: float     # YES-scale entry: ask for a YES buy, bid for a NO buy
    entry_at: datetime


def clv(side: str, entry_yes: float, close_mid: float) -> float:
    """Positive = bought cheaper than the close. Probability points."""
    return (close_mid - entry_yes) if side == "YES" else (entry_yes - close_mid)


def score_bet(bet: Bet, close: dict[str, Any] | None) -> tuple[str, float | None]:
    """
    (label, clv). Only 'ok' carries a number. Everything else is counted
    and excluded, never averaged in:
      no_close           -- no close captured (yet)
      close_missed       -- the pass arrived after the start; no price stored
      entry_after_start  -- the entry itself was in-play; a pre-game close
                            is not its closing line
    """
    if close is None:
        return "no_close", None
    closed_at = _ts(close.get("closed_at"))
    if closed_at is not None and bet.entry_at >= closed_at:
        return "entry_after_start", None
    if close.get("status") != "pre_start" or close.get("bid") is None or close.get("ask") is None:
        return "close_missed", None
    mid = (float(close["bid"]) + float(close["ask"])) / 2
    return "ok", round(clv(bet.side, bet.entry_yes, mid), 4)


def event_means(scored: list[tuple[Bet, float]]) -> dict[str, float]:
    """Average CLV within each event first: one event, one data point."""
    per: dict[str, list[float]] = {}
    for b, v in scored:
        per.setdefault(b.event_slug, []).append(v)
    return {e: statistics.mean(v) for e, v in per.items()}


def bootstrap_ci(values: list[float], n: int = 2000, alpha: float = 0.05,
                 seed: int = 7) -> tuple[float, float] | None:
    """Percentile CI of the mean, resampling the given units (events)."""
    if len(values) < 2:
        return None
    rng = random.Random(seed)
    k = len(values)
    means = sorted(statistics.mean(rng.choices(values, k=k)) for _ in range(n))
    lo = means[int(alpha / 2 * n)]
    hi = means[min(n - 1, int((1 - alpha / 2) * n))]
    return round(lo, 4), round(hi, 4)


def summarize(bets: list[Bet], closes: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per (lane, source): counts, excluded labels, event-level mean and CI."""
    groups: dict[tuple[str, str], list[Bet]] = {}
    for b in bets:
        groups.setdefault((b.lane, b.source), []).append(b)
    out = []
    for (lane, source), bs in sorted(groups.items()):
        labels: dict[str, int] = {}
        ok: list[tuple[Bet, float]] = []
        for b in bs:
            label, v = score_bet(b, closes.get(b.market_slug))
            labels[label] = labels.get(label, 0) + 1
            if v is not None:
                ok.append((b, v))
        em = event_means(ok)
        vals = list(em.values())
        out.append({
            "lane": lane, "source": source, "bets": len(bs),
            "events": len({b.event_slug for b in bs}),
            "scored_bets": len(ok), "scored_events": len(vals),
            "mean_clv": round(statistics.mean(vals), 4) if vals else None,
            "ci95": bootstrap_ci(vals),
            "positive_events": sum(v > 0 for v in vals),
            "excluded": {k: v for k, v in labels.items() if k != "ok"},
        })
    return out


# --------------------------------------------------------------------------
# entries, from each lane's own records
# --------------------------------------------------------------------------

def _first_per_market(bets: list[Bet]) -> list[Bet]:
    """One bet per (lane, source, market): later rows on it are the same bet."""
    seen, out = set(), []
    for b in sorted(bets, key=lambda b: b.entry_at):
        k = (b.lane, b.source, b.market_slug)
        if k not in seen:
            seen.add(k)
            out.append(b)
    return out


def load_bets(dbs: dict[str, str] | None = None) -> list[Bet]:
    """
    Every side-taking decision the lanes recorded:
      slow/tier3       traces.db signals (YES at ask, NO at bid), sized or not
      fast/acted       fastlane.db rows the fast lane would have fired on
      fast/flagged     rows with a direction that stayed below the bar
      weather/model    weather.db, side = sign(p_model - listed mid), entry at
                       the LISTED mid (no book was recorded) -- labelled
    Shadow verdicts take no side and so are not bets; see line_moves().
    """
    dbs = dbs or SOURCE_DBS
    bets: list[Bet] = []
    t = _ro(dbs["traces"])
    if t:
        with closing(t):
            for r in t.execute("SELECT * FROM evaluations WHERE signal_side IS NOT NULL"
                               " AND bid IS NOT NULL AND ask IS NOT NULL"):
                side = r["signal_side"]
                bets.append(Bet("slow", "tier3", r["market_slug"],
                                event_key(r["market_slug"], r["event_slug"]), side,
                                r["ask"] if side == "YES" else r["bid"], _ts(r["at"])))
    f = _ro(dbs["fastlane"])
    if f:
        with closing(f):
            for r in f.execute("SELECT * FROM signals WHERE bid0 IS NOT NULL AND ask0 IS NOT NULL"
                               " AND effect IN ('raises', 'lowers')"):
                side = "YES" if r["effect"] == "raises" else "NO"
                bets.append(Bet("fast", "acted" if r["acted"] else "flagged", r["market_slug"],
                                event_key(r["market_slug"], r["event_slug"]), side,
                                r["ask0"] if side == "YES" else r["bid0"], _ts(r["at"])))
    w = _ro(dbs["weather"])
    if w:
        with closing(w):
            for r in w.execute("SELECT * FROM forecasts WHERE market_mid > 0 AND market_mid < 1"):
                side = "YES" if r["p_model"] >= r["market_mid"] else "NO"
                bets.append(Bet("weather", "model_listed_mid", r["market_slug"],
                                f"temp-{r['city']}-{r['target_date']}", side, r["market_mid"],
                                _ts(r["at"])))
    return _first_per_market([b for b in bets if b.entry_at is not None])


def line_moves(closes: dict[str, dict[str, Any]], dbs: dict[str, str] | None = None
               ) -> list[dict[str, Any]]:
    """
    Shadow verdicts take no side, so they get no CLV. What they give is
    the CONTROL: how far lines move from triage to close on their own,
    escalated vs rejected, as mean |close_mid - triage_mid| over events.
    A CLV smaller than this is noise.
    """
    dbs = dbs or SOURCE_DBS
    s = _ro(dbs["shadow"])
    if not s:
        return []
    groups: dict[str, dict[str, list[float]]] = {"escalated": {}, "rejected": {}}
    with closing(s):
        rows = s.execute("SELECT market_slug, event_slug, seen_at, bid, ask, escalate FROM verdicts"
                         " WHERE bid IS NOT NULL AND ask IS NOT NULL ORDER BY seen_at").fetchall()
    done = set()
    for r in rows:
        c = closes.get(r["market_slug"])
        if r["market_slug"] in done or not c or c.get("status") != "pre_start":
            continue
        seen, closed_at = _ts(r["seen_at"]), _ts(c.get("closed_at"))
        if seen is None or closed_at is None or seen >= closed_at:
            continue
        done.add(r["market_slug"])
        move = abs((c["bid"] + c["ask"]) / 2 - (r["bid"] + r["ask"]) / 2)
        g = groups["escalated" if r["escalate"] else "rejected"]
        g.setdefault(event_key(r["market_slug"], r["event_slug"]), []).append(move)
    out = []
    for name, per in groups.items():
        vals = [statistics.mean(v) for v in per.values()]
        out.append({"group": name, "markets": sum(len(v) for v in per.values()),
                    "events": len(vals),
                    "mean_abs_move": round(statistics.mean(vals), 4) if vals else None})
    return out


def load_closes(db: str = DB_PATH) -> dict[str, dict[str, Any]]:
    c = _ro(db)
    if not c:
        return {}
    with closing(c):
        return {r["market_slug"]: dict(r) for r in c.execute("SELECT * FROM closes")}


def report_data(db: str = DB_PATH, dbs: dict[str, str] | None = None) -> dict[str, Any]:
    """Everything the report prints and the dashboard shows, as plain data."""
    closes = load_closes(db)
    bets = load_bets(dbs)
    status: dict[str, int] = {}
    for c in closes.values():
        status[c["status"]] = status.get(c["status"], 0) + 1
    return {
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "closes": status,
        "groups": summarize(bets, closes),
        "line_moves": line_moves(closes, dbs),
        "unit": "event",
        "note": "CLV = closing mid - entry (YES), entry - closing mid (NO), probability points. "
                "In-play entries and missed closes are counted and excluded.",
    }


def report(db: str = DB_PATH) -> None:
    d = report_data(db)
    print("=" * 86)
    print(f"  closing line value   closes: {d['closes'] or 'none captured yet'}")
    print("=" * 86)
    print(f"\n  {'lane/source':<24}{'bets':>5}{'events':>7}{'scored ev':>10}{'mean clv':>10}"
          f"{'95% CI (events)':>20}   excluded")
    for g in d["groups"]:
        ci = f"[{g['ci95'][0]:+.3f}, {g['ci95'][1]:+.3f}]" if g["ci95"] else "--"
        m = f"{g['mean_clv']:+.4f}" if g["mean_clv"] is not None else "--"
        print(f"  {g['lane'] + '/' + g['source']:<24}{g['bets']:>5}{g['events']:>7}"
              f"{g['scored_events']:>10}{m:>10}{ci:>20}   {json.dumps(g['excluded'])}")
    print("\n  control: shadow line moves, triage -> close (no side taken)")
    for lm in d["line_moves"]:
        v = f"{lm['mean_abs_move']:.4f}" if lm["mean_abs_move"] is not None else "--"
        print(f"    {lm['group']:<10} markets {lm['markets']:>4}  events {lm['events']:>3}  "
              f"mean |move| {v}")
    print("\n  The unit is the event. A mean inside its CI of zero, or smaller than the\n"
          "  control's move, is not a finding.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Closing line value. Shadow only; no orders.")
    ap.add_argument("--capture", action="store_true", help="one capture pass (schedule ~10 min)")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--window", type=float, default=WINDOW_MIN, help="minutes ahead to quote")
    ap.add_argument("--max-calls", type=int, default=MAX_CALLS)
    ap.add_argument("--tags", default="", help="comma-separated tags; default one untagged listing")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if args.capture:
        tags = tuple(t.strip() for t in args.tags.split(",") if t.strip())
        t0 = time.monotonic()
        try:
            stats = asyncio.run(asyncio.wait_for(
                capture(window_min=args.window, max_calls=args.max_calls, tags=tags),
                timeout=PASS_CAP_S))
            log.info("pass done in %.0fs: %s", time.monotonic() - t0, stats)
        except asyncio.TimeoutError:
            log.error("hard cap of %.0fs reached; rows already written are kept", PASS_CAP_S)
    if args.report or not args.capture:
        report()


if __name__ == "__main__":
    main()
