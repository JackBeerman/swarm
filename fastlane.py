"""
fastlane.py -- can Jev read news faster than the book reprices?

SHADOW ONLY. This file places no orders and imports nothing that can.

The slow lane uses Jev to save money: it filters markets so the LLM tiers
run rarely. That wastes the other half of what Jev is -- a ~300 ms reader.
A fast judgment followed by a minute of LLM research is a slow pipeline.

The fast lane inverts it. A headline arrives; ONE Jev request decides
whether it is a new fact, whether it concerns an event we watch, and for
each watched market which way it pushes YES and how much. No LLM is on
the path.

Whether that is worth anything is an empirical question with a fast
answer, which is the point of this recorder. It does not ask "did the bet
win" (days, and ~100 events before it means anything). It asks: after Jev
flagged a headline, did the price move the way Jev said, over the next 1,
5 and 30 minutes? Every headline is a data point and it resolves in
minutes.

Two honest unknowns this measures rather than assumes:
  * feed lag -- `published_at` vs `seen_at`. If RSS runs minutes behind
    the book, no model speed helps, and the numbers will say so.
  * whether the book lags text news at all.

    python fastlane.py --tags nfl --start-window 6 --minutes 240
    python fastlane.py --replay 8        # smoke test on the newest items
    python fastlane.py --score
"""

from __future__ import annotations

import argparse
import asyncio
import email.utils
import html
import logging
import os
import re
import sqlite3
import statistics
import time
import xml.etree.ElementTree as ET
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from config import load_dotenv_if_present

load_dotenv_if_present()  # before swarm, which reads the model id at import

from polymarket_us import AsyncPolymarketUS  # noqa: E402

from adapters import (  # noqa: E402
    QuoteFetcher,
    fetch_events_across_tags,
    iter_event_markets,
    listed_mid,
    normalize_market,
)
from questions import (  # noqa: E402
    FASTLANE_HEADLINE_QUESTIONS,
    FastLaneThresholds,
    fastlane_market_questions,
    political_tag,
)
from search import (  # noqa: E402
    ANTHROPIC_API_URL,
    ANTHROPIC_VERSION,
    SEARCH_MODEL,
    WEB_SEARCH_TOOL,
)
from swarm import JevTriage, _loads_loose  # noqa: E402

log = logging.getLogger("fastlane")

DB_PATH = os.getenv("FASTLANE_DB", "fastlane.db")

# Free, no key. Measured 2026-09-21: all answer in under 700 ms.
FEEDS: dict[str, str] = {
    "yahoo_nfl": "https://sports.yahoo.com/nfl/rss/",
    "espn_nfl": "https://www.espn.com/espn/rss/nfl/news",
    "pft": "https://profootballtalk.nbcsports.com/feed/",
    "cbs_nfl": "https://www.cbssports.com/rss/headlines/nfl/",
    "rotowire_nfl": "https://www.rotowire.com/rss/news.php?sport=NFL",
}

FOLLOW_UPS = (("mid_1m", 60), ("mid_5m", 300), ("mid_30m", 1800))
MAX_MARKETS_PER_EVENT = 6

_SCHEMA = """
CREATE TABLE IF NOT EXISTS headlines (
    id            INTEGER PRIMARY KEY,
    seen_at       TEXT NOT NULL,
    published_at  TEXT,
    lag_seconds   REAL,                 -- seen - published: the feed's delay
    source        TEXT NOT NULL,
    url           TEXT UNIQUE,
    title         TEXT,
    summary       TEXT
);
CREATE TABLE IF NOT EXISTS signals (
    id                INTEGER PRIMARY KEY,
    headline_id       INTEGER NOT NULL REFERENCES headlines(id),
    at                TEXT NOT NULL,
    event_slug        TEXT,
    market_slug       TEXT NOT NULL,
    question          TEXT,
    outcome           TEXT,
    reports_new_fact  REAL,
    concerns_event    REAL,
    political         REAL,
    effect            TEXT,             -- raises | lowers | no_clear_effect
    effect_conf       REAL,
    size              REAL,             -- 0-2 Score
    acted             INTEGER NOT NULL, -- would the fast lane have fired
    jev_ms            REAL,
    model             TEXT,
    bid0              REAL,
    ask0              REAL,
    mid_1m            REAL,
    mid_5m            REAL,
    mid_30m           REAL
);
CREATE INDEX IF NOT EXISTS idx_sig_headline ON signals(headline_id);
"""


def connect(path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


# --------------------------------------------------------------------------
# feeds
# --------------------------------------------------------------------------

_TAG = re.compile(r"<[^>]+>")


def _clean(text: str | None, limit: int) -> str:
    return html.unescape(_TAG.sub(" ", text or "")).strip()[:limit]


def parse_feed(xml_text: str, source: str) -> list[dict[str, Any]]:
    """RSS 2.0 or Atom -> [{source, url, title, summary, published_at}]."""
    try:
        root = ET.fromstring(xml_text.strip())
    except ET.ParseError:
        return []
    out = []
    for el in root.iter():
        if el.tag.split("}")[-1] not in ("item", "entry"):
            continue
        f: dict[str, str] = {}
        for child in el:
            name = child.tag.split("}")[-1]
            if name == "link" and not (child.text or "").strip():
                f.setdefault("link", child.attrib.get("href", ""))
            else:
                f.setdefault(name, child.text or "")
        title = _clean(f.get("title"), 300)
        url = (f.get("link") or f.get("guid") or f.get("id") or "").strip()
        if not title or not url:
            continue
        published = None
        raw = f.get("pubDate") or f.get("published") or f.get("updated")
        if raw:
            try:
                published = email.utils.parsedate_to_datetime(raw)
            except (TypeError, ValueError):
                try:
                    published = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                except ValueError:
                    published = None
            if published and published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
        out.append({
            "source": source, "url": url, "title": title,
            "summary": _clean(f.get("description") or f.get("summary"), 600),
            "published_at": published.isoformat() if published else None,
        })
    return out


async def poll_feed(client: httpx.AsyncClient, source: str, url: str) -> list[dict[str, Any]]:
    try:
        r = await client.get(url)
        r.raise_for_status()
    except Exception as exc:  # noqa: BLE001 -- a dead feed must not stop the others
        log.debug("feed %s failed: %s", source, exc)
        return []
    return parse_feed(r.text, source)


# --------------------------------------------------------------------------
# watchlist
# --------------------------------------------------------------------------

async def build_watchlist(
    pm: Any, tags: tuple[str, ...], start_window_hours: float, per_event: int,
) -> dict[str, dict[str, Any]]:
    """{event_slug: {"title", "markets": [normalized, ...]}} for upcoming events."""
    now = datetime.now(timezone.utc)
    extra = {
        "startTimeMin": (now - timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "startTimeMax": (now + timedelta(hours=start_window_hours)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    events = await fetch_events_across_tags(pm, tags=tags, per_tag=20, extra=extra)
    by_event: dict[str, dict[str, Any]] = {}
    for market, ev in iter_event_markets({"events": events}):
        n = normalize_market(market, ev)
        if political_tag(n.get("tags")):
            continue
        mid = listed_mid(market)
        if mid is None or not (0.15 <= mid <= 0.85):
            continue                      # a price pinned at an extreme cannot show drift
        slot = by_event.setdefault(
            str(n.get("event_slug")), {"title": n.get("event_title"), "markets": []})
        slot["markets"].append((abs(mid - 0.5), n))
    for slot in by_event.values():
        slot["markets"] = [n for _, n in sorted(slot["markets"], key=lambda t: t[0])[:per_event]]
    return {k: v for k, v in by_event.items() if v["markets"]}


# --------------------------------------------------------------------------
# the brief -- the slow lane feeding the fast one
# --------------------------------------------------------------------------

_BRIEF_PROMPT = """Search the web for the CURRENT rosters and the latest injury report for this game: {event}.

Return ONLY a JSON object, no prose:
{{"teams": {{"<full team name>": ["<player> (<position>)", ...], "<other team>": [...]}}}}

List each team's most important players for betting purposes, up to 14: the starting quarterback first, then the top running backs, receivers, tight end, pass rushers and kicker. Prefer what the search results say; fill gaps from what you already know of these rosters. A PARTIAL LIST IS FINE AND EXPECTED. Include players who are injured or ruled out -- they are the ones headlines will name. Never apologise or explain: if you found only five names per team, return those five. The reply must start with {{ and end with }}."""


async def build_brief(client: httpx.AsyncClient, event_title: str) -> dict[str, list[str]]:
    """
    Who plays for whom, written by an LLM BEFORE any headline arrives.

    Found on the first smoke test (2026-09-21): for "Puka Nacua ruled out",
    Jev answered that it RAISES the Rams' chance of covering. Nothing in
    the state said Nacua is a Ram, and Jev does not know rosters -- a
    question about what the model cannot see, answered from weights, and
    wrong. This is the division of labour the fast lane depends on: the
    LLM is slow and knows things, so it runs once, ahead of time; Jev is
    fast and reads what it is given, so it runs at the moment of news.

    One searched Haiku call per event (~$0.02). Returns {} without a key
    or on any failure, and the run continues with directions unreliable --
    which the log says loudly.
    """
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return {}
    try:
        r = await client.post(
            ANTHROPIC_API_URL, timeout=90,
            headers={"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION,
                     "content-type": "application/json"},
            json={"model": SEARCH_MODEL, "max_tokens": 1500,
                  "messages": [{"role": "user",
                                "content": _BRIEF_PROMPT.format(event=event_title)}],
                  "tools": [{"type": WEB_SEARCH_TOOL, "name": "web_search", "max_uses": 3}]})
        r.raise_for_status()
        content = r.json().get("content")
        text = ""
        if isinstance(content, list):
            text = "".join(b.get("text", "") for b in content
                           if isinstance(b, dict) and b.get("type") == "text")
        teams = _loads_loose(text).get("teams") or {}
        return {str(t): [str(x)[:60] for x in ps][:16]
                for t, ps in teams.items() if isinstance(ps, list)}
    except Exception as exc:  # noqa: BLE001 -- a failed brief degrades the run, it does not stop it
        log.warning("brief failed for %s: %s", event_title, exc)
        return {}


# --------------------------------------------------------------------------
# the fast decision
# --------------------------------------------------------------------------

def build_request(headline: dict[str, Any], event: dict[str, Any], model: str) -> dict[str, Any]:
    questions = dict(FASTLANE_HEADLINE_QUESTIONS)
    for i in range(len(event["markets"])):
        questions.update(fastlane_market_questions(i))
    return {
        "model": model,
        "state": {
            "headline": {"title": headline["title"], "summary": headline["summary"],
                         "source": headline["source"]},
            "event": event["title"],
            # Who plays for whom. Without it the direction of an injury
            # headline is a guess. See build_brief().
            "teams": event.get("teams") or {},
            "markets": [{"question": m.get("question"), "outcome": m.get("outcome")}
                        for m in event["markets"]],
        },
        "questions": questions,
    }


def would_act(row: dict[str, Any], th: FastLaneThresholds) -> bool:
    """Every condition is a branch, not a product. See CLAUDE.md on multiplied factors."""
    return (
        row["political"] <= th.max_political
        and row["reports_new_fact"] >= th.min_new_fact
        and row["concerns_event"] >= th.min_concerns_event
        and row["effect"] in ("raises", "lowers")
        and row["effect_conf"] >= th.min_effect_confidence
        and row["size"] >= th.min_size
    )


def read_answers(body: dict[str, Any], n_markets: int) -> tuple[dict[str, float], list[dict[str, Any]]]:
    """Strict: a malformed answer raises. 0.0 would read as 'not political'."""
    a = body["answers"]

    def noul(name: str) -> float:
        v = a[name]["noul"]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 1:
            raise ValueError(f"malformed {name}: {v!r}")
        return float(v)

    head = {
        "reports_new_fact": noul("reports_new_fact"),
        "concerns_event": noul("concerns_event"),
        "political": noul("headline_political"),
    }
    per_market = []
    for i in range(n_markets):
        eff, size = a[f"effect_{i}"], a[f"size_{i}"]
        if eff["choice"] not in ("raises", "lowers", "no_clear_effect"):
            raise ValueError(f"malformed effect_{i}: {eff['choice']!r}")
        per_market.append({"effect": eff["choice"], "effect_conf": float(eff["confidence"]),
                           "size": float(size["score"])})
    return head, per_market


# --------------------------------------------------------------------------
# recorder
# --------------------------------------------------------------------------

class Recorder:
    def __init__(self, conn: sqlite3.Connection, jev: JevTriage, quotes: QuoteFetcher,
                 watch: dict[str, dict[str, Any]], th: FastLaneThresholds):
        self.conn, self.jev, self.quotes, self.watch, self.th = conn, jev, quotes, watch, th
        self.pending: set[asyncio.Task] = set()
        self.stats = {"headlines": 0, "requests": 0, "acted": 0, "jev_ms": []}

    def _store_headline(self, h: dict[str, Any]) -> int | None:
        seen = datetime.now(timezone.utc)
        lag = None
        if h["published_at"]:
            lag = (seen - datetime.fromisoformat(h["published_at"])).total_seconds()
        try:
            cur = self.conn.execute(
                "INSERT INTO headlines (seen_at, published_at, lag_seconds, source, url, title, summary)"
                " VALUES (?,?,?,?,?,?,?)",
                (seen.isoformat(), h["published_at"], lag, h["source"], h["url"],
                 h["title"], h["summary"]))
        except sqlite3.IntegrityError:
            return None                   # already recorded, e.g. by another feed
        self.conn.commit()
        return cur.lastrowid

    async def handle(self, h: dict[str, Any]) -> None:
        hid = self._store_headline(h)
        if hid is None:
            return
        self.stats["headlines"] += 1
        await asyncio.gather(*(self._judge(hid, h, slug, ev) for slug, ev in self.watch.items()))

    async def _judge(self, hid: int, h: dict[str, Any], event_slug: str, ev: dict[str, Any]) -> None:
        t0 = time.perf_counter()
        try:
            body = await self.jev._post(build_request(h, ev, self.jev._model))
            head, per_market = read_answers(body, len(ev["markets"]))
        except Exception as exc:  # noqa: BLE001 -- skip the headline, keep the feed loop alive
            log.warning("jev failed on %r: %s", h["title"][:50], exc)
            return
        ms = (time.perf_counter() - t0) * 1000
        self.stats["requests"] += 1
        self.stats["jev_ms"].append(ms)

        relevant = (head["concerns_event"] >= self.th.min_concerns_event
                    and head["political"] <= self.th.max_political)
        log.info("%4.0fms  fact=%.2f event=%.2f pol=%.2f  %s  [%s]", ms,
                 head["reports_new_fact"], head["concerns_event"], head["political"],
                 h["title"][:70], h["source"])
        if not relevant:
            return                        # not our event: no quotes spent, nothing to follow

        for m, pm_ans in zip(ev["markets"], per_market, strict=True):
            row = {**head, **pm_ans}
            acted = would_act(row, self.th)
            bbo = await self.quotes.bbo(m["slug"]) or {}
            cur = self.conn.execute(
                "INSERT INTO signals (headline_id, at, event_slug, market_slug, question, outcome,"
                " reports_new_fact, concerns_event, political, effect, effect_conf, size, acted,"
                " jev_ms, model, bid0, ask0) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (hid, datetime.now(timezone.utc).isoformat(), event_slug, m["slug"],
                 m.get("question"), m.get("outcome"), head["reports_new_fact"],
                 head["concerns_event"], head["political"], pm_ans["effect"],
                 pm_ans["effect_conf"], pm_ans["size"], int(acted), ms,
                 body.get("model"), bbo.get("bid"), bbo.get("ask")))
            self.conn.commit()
            if acted:
                self.stats["acted"] += 1
                log.info("   WOULD ACT  %-7s size=%.2f conf=%.2f  %s / %s  @ %s/%s",
                         pm_ans["effect"], pm_ans["size"], pm_ans["effect_conf"],
                         (m.get("question") or "")[:40], (m.get("outcome") or "")[:24],
                         bbo.get("bid"), bbo.get("ask"))
            task = asyncio.create_task(self._follow(cur.lastrowid, m["slug"]))
            self.pending.add(task)
            task.add_done_callback(self.pending.discard)

    async def _follow(self, signal_id: int, slug: str) -> None:
        start = time.monotonic()
        for column, after in FOLLOW_UPS:
            await asyncio.sleep(max(0.0, after - (time.monotonic() - start)))
            bbo = await self.quotes.bbo(slug) or {}
            if bbo.get("bid") is None or bbo.get("ask") is None:
                continue
            self.conn.execute(f"UPDATE signals SET {column}=? WHERE id=?",  # noqa: S608 -- column is a constant
                              ((bbo["bid"] + bbo["ask"]) / 2, signal_id))
            self.conn.commit()


async def run(tags: tuple[str, ...], start_window: float, minutes: float,
              poll_seconds: float, replay: int, per_event: int,
              brief: bool = True) -> None:
    th = FastLaneThresholds()
    async with AsyncPolymarketUS() as pm, httpx.AsyncClient(
        timeout=10, follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 (research; rss reader)"},
    ) as client:
        watch = await build_watchlist(pm, tags, start_window, per_event)
        if not watch:
            log.error("no upcoming events for tags=%s within %sh", tags, start_window)
            return
        quotes = QuoteFetcher(pm, concurrency=2)
        for slug, ev in list(watch.items()):
            # The listed price can sit at 0.50 over an empty book (seen:
            # 0.01/0.99). A market that wide cannot show drift.
            live = []
            for m in ev["markets"]:
                bbo = await quotes.bbo(m["slug"]) or {}
                if (bbo.get("bid") is not None and bbo.get("ask") is not None
                        and bbo["ask"] - bbo["bid"] <= 0.06):
                    live.append(m)
            ev["markets"] = live
            if not live:
                del watch[slug]
                continue
            if brief:
                ev["teams"] = await build_brief(client, ev["title"])
            log.info("watching %s: %s (%d markets, brief: %s)", slug, ev["title"], len(live),
                     ", ".join(f"{t} x{len(ps)}" for t, ps in (ev.get("teams") or {}).items())
                     or "NONE -- directions on player news are unreliable")
        if not watch:
            log.error("no watched market has a tight enough book")
            return

        jev = JevTriage()
        with closing(connect()) as conn:
            rec = Recorder(conn, jev, quotes, watch, th)

            first = [h for batch in await asyncio.gather(
                *(poll_feed(client, s, u) for s, u in FEEDS.items())) for h in batch]
            seen = {h["url"] for h in first}
            log.info("baseline: %d existing items across %d feeds", len(first), len(FEEDS))
            if replay:
                newest = sorted(first, key=lambda h: h["published_at"] or "", reverse=True)
                for h in newest[:replay]:
                    await rec.handle(h)

            deadline = time.monotonic() + minutes * 60
            while time.monotonic() < deadline and not replay:
                await asyncio.sleep(poll_seconds)
                batches = await asyncio.gather(
                    *(poll_feed(client, s, u) for s, u in FEEDS.items()))
                for h in (h for b in batches for h in b):
                    if h["url"] in seen:
                        continue
                    seen.add(h["url"])
                    await rec.handle(h)

            if rec.pending and not replay:
                log.info("waiting on %d price follow-ups (up to 30 min)...", len(rec.pending))
                await asyncio.gather(*rec.pending, return_exceptions=True)
            for t in rec.pending:
                t.cancel()
            await jev.aclose()
            ms = rec.stats["jev_ms"]
            log.info("done: %d headlines, %d jev requests (median %.0f ms), %d would-act signals",
                     rec.stats["headlines"], rec.stats["requests"],
                     statistics.median(ms) if ms else 0, rec.stats["acted"])


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------

def score(db: str = DB_PATH) -> None:
    """
    Did the price move the way Jev said?

    Signed drift = (mid_later - mid_0) x (+1 raises, -1 lowers). The
    independent unit is the HEADLINE: six markets on one headline move
    together, so drift is averaged within a headline first and the count
    that matters is headlines, not rows. Rows Jev did not flag are the
    control: their absolute drift is what the book does on its own.
    """
    with closing(connect(db)) as conn:
        rows = conn.execute(
            "SELECT s.*, h.lag_seconds FROM signals s JOIN headlines h ON h.id = s.headline_id"
            " WHERE s.bid0 IS NOT NULL AND s.ask0 IS NOT NULL").fetchall()
        lags = [r["lag_seconds"] for r in conn.execute(
            "SELECT lag_seconds FROM headlines WHERE lag_seconds IS NOT NULL AND lag_seconds >= 0")]
        n_head = conn.execute("SELECT COUNT(*) FROM headlines").fetchone()[0]
    print("=" * 74)
    print(f"  fast lane: {n_head} headlines seen, {len(rows)} (headline, market) rows quoted")
    print("=" * 74)
    if lags:
        print(f"\n  feed lag (seen - published): median {statistics.median(lags) / 60:.1f} min, "
              f"fastest {min(lags) / 60:.1f} min   <- if this is minutes, the feed is the bottleneck")
    ms = [r["jev_ms"] for r in rows if r["jev_ms"]]
    if ms:
        print(f"  jev decision time: median {statistics.median(ms):.0f} ms")
    if not rows:
        print("\n  nothing recorded yet.")
        return
    print(f"\n  {'group':<22}{'headlines':>10}{'rows':>6}{'+1m':>9}{'+5m':>9}{'+30m':>9}")
    for label, pick, signed in (
        ("would act (signed)", lambda r: r["acted"], True),
        ("flagged, below bar", lambda r: not r["acted"] and r["effect"] != "no_clear_effect", True),
        ("control (abs drift)", lambda r: r["effect"] == "no_clear_effect", False),
    ):
        rs = [r for r in rows if pick(r)]
        cells = []
        for col in ("mid_1m", "mid_5m", "mid_30m"):
            per_headline: dict[int, list[float]] = {}
            for r in rs:
                if r[col] is None:
                    continue
                d = r[col] - (r["bid0"] + r["ask0"]) / 2
                d = d * (1 if r["effect"] == "raises" else -1) if signed else abs(d)
                per_headline.setdefault(r["headline_id"], []).append(d)
            means = [statistics.mean(v) for v in per_headline.values()]
            cells.append(f"{statistics.mean(means):+.4f}" if means else "      --")
        print(f"  {label:<22}{len({r['headline_id'] for r in rs}):>10}{len(rs):>6}"
              + "".join(f"{c:>9}" for c in cells))
    print("\n  Positive signed drift means the book moved the way Jev said AFTER we saw the\n"
          "  headline. It has to beat the spread (~0.02) and the control's drift to matter,\n"
          "  and it needs dozens of acted headlines across several days before it is a finding.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Shadow-only recorder: Jev reads headlines, prices are followed.")
    ap.add_argument("--tags", default="nfl")
    ap.add_argument("--start-window", type=float, default=6.0, help="hours ahead to look for events")
    ap.add_argument("--minutes", type=float, default=240.0, help="how long to poll")
    ap.add_argument("--poll", type=float, default=20.0, help="seconds between feed polls")
    ap.add_argument("--per-event", type=int, default=MAX_MARKETS_PER_EVENT)
    ap.add_argument("--replay", type=int, default=0,
                    help="smoke test: judge the newest N existing items, then exit")
    ap.add_argument("--no-brief", action="store_true",
                    help="skip the LLM roster brief (no Anthropic spend; directions unreliable)")
    ap.add_argument("--score", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if args.score:
        score()
        return
    asyncio.run(run(tuple(t.strip() for t in args.tags.split(",")), args.start_window,
                    args.minutes, args.poll, args.replay, args.per_event,
                    not args.no_brief))


if __name__ == "__main__":
    main()
