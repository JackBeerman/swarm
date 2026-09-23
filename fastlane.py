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
import json
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
    RESTRICTED_QUESTIONS,
    FastLaneThresholds,
    GateThresholds,
    fastlane_market_questions,
    fastlane_scenario_questions,
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

# Free, no key, keyed by the exchange tag they cover. Probed 2026-09-22:
# every one answers in under 500 ms; freshness varies from minutes
# (Yahoo, CoinDesk, Deadline) to half a day (TechCrunch, Google blog).
FEEDS_BY_TAG: dict[str, dict[str, str]] = {
    "nfl": {
        "yahoo_nfl": "https://sports.yahoo.com/nfl/rss/",
        "espn_nfl": "https://www.espn.com/espn/rss/nfl/news",
        "pft": "https://profootballtalk.nbcsports.com/feed/",
        "cbs_nfl": "https://www.cbssports.com/rss/headlines/nfl/",
        "rotowire_nfl": "https://www.rotowire.com/rss/news.php?sport=NFL",
    },
    "mlb": {
        "yahoo_mlb": "https://sports.yahoo.com/mlb/rss/",
        "espn_mlb": "https://www.espn.com/espn/rss/mlb/news",
        "cbs_mlb": "https://www.cbssports.com/rss/headlines/mlb/",
        "rotowire_mlb": "https://www.rotowire.com/rss/news.php?sport=MLB",
        "mlbtr": "https://www.mlbtraderumors.com/feed",
    },
    "tech": {
        "verge": "https://www.theverge.com/rss/index.xml",
        "techcrunch_ai": "https://techcrunch.com/category/artificial-intelligence/feed/",
        "arstechnica": "https://feeds.arstechnica.com/arstechnica/technology-lab",
        "google_blog": "https://blog.google/rss/",
        "openai_news": "https://openai.com/news/rss.xml",
        "hn_front": "https://hnrss.org/frontpage",
    },
    "crypto": {
        "coindesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
        "cointelegraph": "https://cointelegraph.com/rss",
    },
    "entertainment": {
        "variety": "https://variety.com/feed/",
        "deadline": "https://deadline.com/feed/",
    },
    "music": {
        "billboard": "https://www.billboard.com/feed/",
        "variety": "https://variety.com/feed/",
    },
    "esports": {
        "hltv": "https://www.hltv.org/rss/news",
        "dotesports": "https://dotesports.com/feed",
    },
}
FEEDS_BY_TAG["sports"] = {**FEEDS_BY_TAG["nfl"], **FEEDS_BY_TAG["mlb"]}
FEEDS_BY_TAG["economics"] = FEEDS_BY_TAG["business"] = FEEDS_BY_TAG["tech"]
FEEDS: dict[str, str] = FEEDS_BY_TAG["nfl"]          # default, and what tests import

#: Tags whose events are games with a start time. Everything else is a
#: standing market (a product release, a chart position) that has no
#: kickoff, so the watchlist is built from open markets by tag instead.
GAME_TAGS = {"nfl", "mlb", "nba", "nhl", "sports", "esports", "soccer", "mls", "cfb", "ufc", "mma"}


def feeds_for(tags: tuple[str, ...]) -> dict[str, str]:
    out: dict[str, str] = {}
    for t in tags:
        out.update(FEEDS_BY_TAG.get(t, {}))
    return out or FEEDS

FOLLOW_UPS = (("mid_1m", 60), ("mid_5m", 300), ("mid_30m", 1800))

# Found 2026-09-22: a 240-minute NFL run was still alive 24 hours later,
# recording stale headlines hours apart -- a network call waited on a dead
# connection (the machine slept) and the deadline was only checked between
# batches. Every await on the hot path is now bounded, and main() puts a
# hard wall-clock cap on the whole run.
HANDLE_TIMEOUT_S = 180
RUN_OVERHEAD_MIN = 50       # setup + briefs + the 30-min follow-up tail
MAX_MARKETS_PER_EVENT = 6

_SCHEMA = """
CREATE TABLE IF NOT EXISTS headlines (
    id            INTEGER PRIMARY KEY,
    seen_at       TEXT NOT NULL,
    published_at  TEXT,
    lag_seconds   REAL,                 -- seen - published: the feed's delay
    source        TEXT NOT NULL,
    url           TEXT,
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
    repeat            REAL,             -- same fact as one already acted on
    scenario          TEXT,             -- brief scenario id Jev matched, or none_of_these
    scenario_conf     REAL,
    contradicts       REAL,
    fair_yes          REAL,             -- the brief's pre-written price for that scenario
    edge              REAL,             -- fair_yes - ask (YES) or bid - fair_yes (NO), in code
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
CREATE TABLE IF NOT EXISTS briefs (
    id            INTEGER PRIMARY KEY,
    written_at    TEXT NOT NULL,
    event_slug    TEXT NOT NULL,
    model         TEXT,
    brief_json    TEXT NOT NULL,        -- teams, facts, scenarios: what Jev was shown
    cost_usd      REAL
);
-- A "live updates" item keeps its URL and changes its title as news
-- breaks (seen: Yahoo, 2026-09-21). Identity is URL + title.
CREATE UNIQUE INDEX IF NOT EXISTS idx_head_url_title ON headlines(url, title);
"""


def connect(path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    have = {r["name"] for r in conn.execute("PRAGMA table_info(signals)")}
    for col, typ in (("repeat", "REAL"), ("scenario", "TEXT"), ("scenario_conf", "REAL"),
                     ("contradicts", "REAL"), ("fair_yes", "REAL"), ("edge", "REAL"),
                     ("resolved_outcome", "TEXT")):
        if col not in have:
            conn.execute(f"ALTER TABLE signals ADD COLUMN {col} {typ}")  # noqa: S608 -- constants
    conn.commit()
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

#: Slug prefix -> rank; lower is watched first. Measured on Padres-Dodgers
#: 2026-09-22: moneyline (aec) and run line (asc) spreads 0.005, totals
#: (tsc) 0.01, inning outcomes (atc) 0.01, player props (astatc, 360 of
#: 415 markets) 0.98. The props' listed 0.01/0.99 averages to exactly
#: 0.50, so "nearest 0.50" used to rank empty books first.
KIND_RANK = {"aec": 0, "asc": 1, "cks": 1, "tsc": 2, "atc": 3}


def market_kind_rank(market: dict[str, Any]) -> int:
    return KIND_RANK.get(str(market.get("slug") or "").split("-")[0], 9)


def listed_width(market: dict[str, Any]) -> float:
    """Gap between the two listed prices; 0 when only one is listed."""
    raw = market.get("outcomePrices")
    try:
        vals = [float(v) for v in (json.loads(raw) if isinstance(raw, str) else raw or [])][:2]
    except (TypeError, ValueError):
        return 1.0
    return abs(vals[0] - vals[1]) if len(vals) == 2 else 0.0


_PERIOD_TOKENS = {"1h", "2h", "1q", "2q", "3q", "4q", "1p", "2p", "3p", "h1", "h2",
                  "q1", "q2", "q3", "q4", "1st", "2nd", "3rd", "4th"}


def is_period_market(market: dict[str, Any]) -> bool:
    """A quarter/half/period line, as opposed to the full-game line."""
    slug_tokens = set(str(market.get("slug") or "").lower().split("-"))
    if slug_tokens & _PERIOD_TOKENS:
        return True
    text = f"{market.get('question') or ''} {market.get('outcome') or ''}".lower()
    return any(w in text for w in ("half", "quarter", "1st period", "2nd period", "3rd period"))


async def build_watchlist(
    pm: Any, tags: tuple[str, ...], start_window_hours: float, per_event: int,
    max_events: int = 6,
) -> dict[str, dict[str, Any]]:
    """
    {event_slug: {"title", "markets": [normalized, ...]}}.

    Games: events starting within the window. Standing markets (tech,
    crypto, music): open events by tag, no time filter, since a product
    release or a chart position has no kickoff.
    """
    now = datetime.now(timezone.utc)
    extra: dict[str, Any] = {}
    if any(t in GAME_TAGS for t in tags):
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
        if listed_width(market) > 0.20:
            continue                      # 0.01/0.99 is an empty book, not a 0.50 market
        slot = by_event.setdefault(
            str(n.get("event_slug")), {"title": n.get("event_title"), "markets": []})
        # Full-game lines first. Measured 2026-09-21: the three period
        # markets nearest 0.50 (2H, Q3, Q4 spreads) did not move a tick in
        # 30 minutes after a starting QB left the game; the full-game
        # spread moved 0.53 -> 0.37. Thin derivative books do not reprice
        # on news, so watching them measures nothing.
        slot["markets"].append(((market_kind_rank(n), is_period_market(n)), abs(mid - 0.5), n))
    for slot in by_event.values():
        slot["markets"] = [n for _, _, n in sorted(slot["markets"], key=lambda t: t[:2])[:per_event]]
    # Most markets nearest 0.50 first; standing-market tags list dozens of
    # events and one brief costs ~$0.03, so the count is capped.
    ranked = sorted(((k, v) for k, v in by_event.items() if v["markets"]),
                    key=lambda kv: -len(kv[1]["markets"]))
    return dict(ranked[:max_events])


async def book_verdict(quotes: Any, slug: str, max_spread: float = 0.06,
                       attempts: int = 3, pause: float = 35.0) -> tuple[str, dict[str, Any]]:
    """
    "tight", "wide", "one_sided", or "no_quote" -- never conflated.

    A failed quote is retried after the shared block pause, because the
    usual cause is a temporary Cloudflare block on the whole endpoint, not
    anything about the market. Dropping it as illiquid hid exactly that.
    """
    for i in range(attempts):
        bbo = await quotes.bbo(slug)
        if bbo is not None:
            if bbo.get("bid") is None or bbo.get("ask") is None:
                return "one_sided", bbo
            return ("tight" if bbo["ask"] - bbo["bid"] <= max_spread else "wide"), bbo
        if i < attempts - 1:
            await asyncio.sleep(pause)
    return "no_quote", {}


async def drop_restricted(jev: JevTriage, watch: dict[str, dict[str, Any]],
                          max_restricted: float = GateThresholds().max_restricted) -> None:
    """
    The same veto the slow lane applies, on every watched MARKET, before a
    brief is written or a headline judged. Found 2026-09-22: the tech
    watchlist picked up IPO markets that resolve on an SEC filing -- the
    slow lane had vetoed them at 0.83 on federal_policy_outcome the day
    before, and the fast lane was checking only the political tag and the
    headline. Max of the four Nouls, never the mean; a Jev failure counts
    as a veto, because an unchecked market must not be watched.
    """
    for slug, ev in list(watch.items()):
        kept = []
        for m in ev["markets"]:
            state = {k: m.get(k) for k in ("question", "outcome", "description", "tags")}
            state["event"] = ev.get("title")
            try:
                body = await jev._post({"model": jev._model, "state": state,
                                        "questions": RESTRICTED_QUESTIONS})
                worst = max(((name, float(body["answers"][name]["noul"]))
                             for name in RESTRICTED_QUESTIONS), key=lambda t: t[1])
            except Exception as exc:  # noqa: BLE001 -- unchecked is vetoed
                log.warning("restricted check failed on %s, dropping: %s", m["slug"], exc)
                continue
            if worst[1] > max_restricted:
                log.info("   VETO %s: %s=%.2f", m["slug"][:44], worst[0], worst[1])
                continue
            kept.append(m)
        ev["markets"] = kept
        if not kept:
            del watch[slug]


# --------------------------------------------------------------------------
# the brief -- the slow lane feeding the fast one
# --------------------------------------------------------------------------

_BRIEF_PROMPT = """You are writing a pre-game brief that a fast classifier will read during the game. Search the web for the CURRENT rosters, the latest injury report and the weather for this game: {event}.

The markets being watched, with the current YES price for each:
{markets}

Return ONLY a JSON object, no prose, of this exact shape:
{{
  "teams": {{"<full team name>": ["<player> (<position>)", ...], "<other team>": [...]}},
  "facts": ["<one dated fact per string, e.g. 'Nacua listed questionable (ankle), 9/21 report'>", ...],
  "scenarios": [
    {{"id": "<short_snake_case>", "trigger": "<a concrete in-game development a headline could report, e.g. 'Giants starting QB leaves the game injured'>",
      "affects": {{"<market key>": <fair YES probability 0-1>, ...}}}}
  ]
}}

Rules:
- teams: up to 14 players each, starting quarterback first, then key skill players, pass rushers, kicker. Include injured or doubtful players; they are the ones headlines name. A partial list is fine.
- facts: 4-10 strings, each dated, each something a headline could later contradict.
- scenarios: 4-8. Each trigger must be a specific event that either happens or does not (a player leaves injured, a player returns, a starter is ruled out pregame, a lead of 14+ at half, severe weather at kickoff). For each scenario give your fair YES probability for EVERY market key listed above under "affects", including ones the trigger barely moves (repeat the current price for those).
- Never apologise or explain. If a search fails, use what you know. The reply must start with {{ and end with }}."""


_BRIEF_PROMPT_GENERAL = """You are writing a pre-event brief that a fast classifier will read as news arrives. Search the web for the current state of play on this question: {event}.

The markets being watched, with the current YES price for each:
{markets}

Return ONLY a JSON object, no prose, of this exact shape:
{{
  "teams": {{"<party or entity>": ["<key person, product, or detail>", ...], ...}},
  "facts": ["<one dated fact per string, e.g. 'Google said Gemini 3.5 is in testing, 9/18'>", ...],
  "scenarios": [
    {{"id": "<short_snake_case>", "trigger": "<a concrete development a headline could report, e.g. 'Google officially announces Gemini 3.5 Pro general availability'>",
      "affects": {{"<market key>": <fair YES probability 0-1>, ...}}}}
  ]
}}

Rules:
- teams: the entities headlines will name (companies, products, artists, people), with the details that identify them. Up to 6 entities, up to 10 details each.
- facts: 4-10 strings, each dated, each something a headline could later contradict.
- scenarios: 4-8. Each trigger must be a specific event that either happens or does not (an official announcement, a release, a delay, a denial, a rival shipping first). NO trigger may depend on a government, legislature, regulator, court, election, or political figure: those headlines are never acted on, so a scenario built on them is dead weight. For each scenario give your fair YES probability for EVERY market key listed above under "affects", including ones the trigger barely moves (repeat the current price for those).
- Never apologise or explain. If a search fails, use what you know. The reply must start with {{ and end with }}."""


async def build_brief(client: httpx.AsyncClient, event_title: str,
                      markets: list[dict[str, Any]] | None = None,
                      quotes: dict[str, dict[str, Any]] | None = None,
                      sport: bool = True) -> dict[str, Any]:
    """
    Who plays for whom, written by an LLM BEFORE any headline arrives.

    Found on the first smoke test (2026-09-21): for "Puka Nacua ruled out",
    Jev answered that it RAISES the Rams' chance of covering. Nothing in
    the state said Nacua is a Ram, and Jev does not know rosters -- a
    question about what the model cannot see, answered from weights, and
    wrong. This is the division of labour the fast lane depends on: the
    LLM is slow and knows things, so it runs once, ahead of time; Jev is
    fast and reads what it is given, so it runs at the moment of news.

    v2 (docs/BRIEF.md): the brief also carries dated `facts` and
    `scenarios`, each with the LLM's fair YES price per watched market.
    The only model-written probability in the fast lane is produced
    here, offline, before any headline exists, so no headline can steer
    it. Jev later recognises which scenario a headline realises; code
    looks up the price.

    One searched Haiku call per event (~$0.03). Returns {} without a key
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
            json={"model": SEARCH_MODEL, "max_tokens": 3000,
                  "messages": [{"role": "user",
                                "content": (_BRIEF_PROMPT if sport else _BRIEF_PROMPT_GENERAL).format(
                                    event=event_title,
                                    markets=_markets_block(markets or [], quotes or {}))}],
                  "tools": [{"type": WEB_SEARCH_TOOL, "name": "web_search", "max_uses": 3}]})
        r.raise_for_status()
        content = r.json().get("content")
        text = ""
        if isinstance(content, list):
            text = "".join(b.get("text", "") for b in content
                           if isinstance(b, dict) and b.get("type") == "text")
        raw = _loads_loose(text)
        keys = {m["slug"] for m in (markets or [])}
        return _clean_brief(raw, keys)
    except Exception as exc:  # noqa: BLE001 -- a failed brief degrades the run, it does not stop it
        log.warning("brief failed for %s: %s", event_title, exc)
        return {}


def _markets_block(markets: list[dict[str, Any]], quotes: dict[str, dict[str, Any]]) -> str:
    lines = []
    for m in markets:
        q = quotes.get(m["slug"]) or {}
        mid = ((q["bid"] + q["ask"]) / 2) if q.get("bid") is not None and q.get("ask") is not None else None
        lines.append(f'- key "{m["slug"]}": {m.get("question")} / YES = {m.get("outcome")}'
                     f'  (current YES price {mid:.2f})' if mid is not None else
                     f'- key "{m["slug"]}": {m.get("question")} / YES = {m.get("outcome")}')
    return "\n".join(lines) or "- (none)"


def _clean_brief(raw: Any, market_keys: set[str]) -> dict[str, Any]:
    """Keep only what the schema promised; drop anything malformed rather than trusting it."""
    if not isinstance(raw, dict):
        return {}
    teams = {str(t): [str(x)[:60] for x in ps][:16]
             for t, ps in (raw.get("teams") or {}).items() if isinstance(ps, list)}
    facts = [str(f)[:200] for f in (raw.get("facts") or []) if isinstance(f, str)][:10]
    scenarios = []
    seen: set[str] = set()
    for sc in (raw.get("scenarios") or [])[:8]:
        if not isinstance(sc, dict):
            continue
        sid = re.sub(r"[^a-z0-9_]", "_", str(sc.get("id") or "").lower())[:40]
        trig = str(sc.get("trigger") or "")[:200]
        affects = {}
        for k, v in (sc.get("affects") or {}).items():
            if k in market_keys and isinstance(v, (int, float)) and 0.0 <= v <= 1.0:
                affects[k] = float(v)
        if sid and trig and affects and sid not in seen and sid != "none_of_these":
            seen.add(sid)
            scenarios.append({"id": sid, "trigger": trig, "affects": affects})
    return {"teams": teams, "facts": facts, "scenarios": scenarios,
            "written_at": datetime.now(timezone.utc).isoformat()}


# --------------------------------------------------------------------------
# the fast decision
# --------------------------------------------------------------------------

def build_request(headline: dict[str, Any], event: dict[str, Any], model: str) -> dict[str, Any]:
    questions = dict(FASTLANE_HEADLINE_QUESTIONS)
    brief = event.get("brief") or {}
    questions.update(fastlane_scenario_questions(brief.get("scenarios") or []))
    for i in range(len(event["markets"])):
        questions.update(fastlane_market_questions(i))
    return {
        "model": model,
        "state": {
            "headline": {"title": headline["title"], "summary": headline["summary"],
                         "source": headline["source"]},
            "event": event["title"],
            # What this event has already acted on, newest first, so a
            # rewrite of the same injury is recognised as a repeat.
            "already_acted": list(event.get("already_acted") or [])[:8],
            # Who plays for whom. Without it the direction of an injury
            # headline is a guess. See build_brief().
            "teams": event.get("teams") or brief.get("teams") or {},
            # Dated facts and pre-priced scenarios. Jev recognises; code prices.
            "brief": {"written_at": brief.get("written_at"),
                      "facts": brief.get("facts") or [],
                      "scenarios": [{"id": sc["id"], "trigger": sc["trigger"]}
                                    for sc in brief.get("scenarios") or []]},
            "markets": [{"question": m.get("question"), "outcome": m.get("outcome")}
                        for m in event["markets"]],
        },
        "questions": questions,
    }


def would_act(row: dict[str, Any], th: FastLaneThresholds) -> bool:
    """Every condition is a branch, not a product. See CLAUDE.md on multiplied factors."""
    return (
        row["political"] <= th.max_political
        and row.get("repeat", 0.0) <= th.max_repeat
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
        "repeat": noul("repeats_acted_fact"),
    }
    if "scenario" in a:
        sc = a["scenario"]
        if not isinstance(sc.get("choice"), str):
            raise ValueError(f"malformed scenario: {sc!r}")
        head["scenario"] = sc["choice"]
        head["scenario_conf"] = float(sc.get("confidence") or 0.0)
        head["contradicts"] = noul("contradicts_brief")
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
        log.info("%4.0fms  fact=%.2f event=%.2f pol=%.2f rep=%.2f  %s  [%s]", ms,
                 head["reports_new_fact"], head["concerns_event"], head["political"],
                 head["repeat"], h["title"][:70], h["source"])
        if not relevant:
            return                        # not our event: no quotes spent, nothing to follow

        matched = None
        if (head.get("scenario") and head["scenario"] != "none_of_these"
                and head.get("scenario_conf", 0.0) >= self.th.min_scenario_confidence
                and head.get("contradicts", 0.0) <= self.th.max_contradiction):
            matched = next((sc for sc in (ev.get("brief") or {}).get("scenarios") or []
                            if sc["id"] == head["scenario"]), None)
            if matched:
                log.info("   SCENARIO %s (conf %.2f): %s", matched["id"],
                         head["scenario_conf"], matched["trigger"][:60])

        for m, pm_ans in zip(ev["markets"], per_market, strict=True):
            row = {**head, **pm_ans}
            acted = would_act(row, self.th)
            bbo = await self.quotes.bbo(m["slug"]) or {}
            fair = matched["affects"].get(m["slug"]) if matched else None
            edge = None
            if fair is not None and bbo.get("bid") is not None and bbo.get("ask") is not None:
                # The number the slip would use, computed here and nowhere else.
                edge = (fair - bbo["ask"]) if pm_ans["effect"] == "raises" else (bbo["bid"] - fair)
            cur = self.conn.execute(
                "INSERT INTO signals (headline_id, at, event_slug, market_slug, question, outcome,"
                " reports_new_fact, concerns_event, political, repeat, effect, effect_conf,"
                " size, acted, jev_ms, model, bid0, ask0, scenario, scenario_conf,"
                " contradicts, fair_yes, edge)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (hid, datetime.now(timezone.utc).isoformat(), event_slug, m["slug"],
                 m.get("question"), m.get("outcome"), head["reports_new_fact"],
                 head["concerns_event"], head["political"], head["repeat"], pm_ans["effect"],
                 pm_ans["effect_conf"], pm_ans["size"], int(acted), ms,
                 body.get("model"), bbo.get("bid"), bbo.get("ask"),
                 head.get("scenario"), head.get("scenario_conf"), head.get("contradicts"),
                 fair, edge))
            self.conn.commit()
            if acted:
                self.stats["acted"] += 1
                acted_list = ev.setdefault("already_acted", [])
                if h["title"] not in acted_list:
                    acted_list.insert(0, h["title"][:120])
                log.info("   WOULD ACT  %-7s size=%.2f conf=%.2f  %s / %s  @ %s/%s%s",
                         pm_ans["effect"], pm_ans["size"], pm_ans["effect_conf"],
                         (m.get("question") or "")[:40], (m.get("outcome") or "")[:24],
                         bbo.get("bid"), bbo.get("ask"),
                         f"  fair={fair:.2f} edge={edge:+.3f}" if edge is not None else "")
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
              brief: bool = True, fast_sources: bool = False) -> None:
    th = FastLaneThresholds()
    feeds = feeds_for(tags)
    extra: list[Any] = []                 # sources.Source objects; see --fast-sources
    sport = any(t in GAME_TAGS for t in tags)
    async with AsyncPolymarketUS() as pm, httpx.AsyncClient(
        timeout=10, follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 (research; rss reader)"},
    ) as client:
        watch = await build_watchlist(pm, tags, start_window, per_event)
        if not watch:
            log.error("no upcoming events for tags=%s within %sh", tags, start_window)
            return
        jev = JevTriage()
        await drop_restricted(jev, watch)
        if not watch:
            log.error("every watched market was vetoed")
            await jev.aclose()
            return
        quotes = QuoteFetcher(pm, concurrency=2)
        counts: dict[str, int] = {}
        for slug, ev in list(watch.items()):
            # The listed price can sit at 0.50 over an empty book (seen:
            # 0.01/0.99). A market that wide cannot show drift.
            live = []
            for m in ev["markets"]:
                verdict, bbo = await book_verdict(quotes, m["slug"])
                counts[verdict] = counts.get(verdict, 0) + 1
                if verdict == "tight":
                    live.append(m)
            ev["markets"] = live
            if not live:
                del watch[slug]
                continue
            if brief:
                bq = {}
                for m in live:
                    bq[m["slug"]] = await quotes.bbo(m["slug"]) or {}
                ev["brief"] = await build_brief(client, ev["title"], live, bq, sport=sport)
                ev["teams"] = ev["brief"].get("teams") or {}
                if ev["brief"]:
                    with closing(connect()) as bconn:
                        bconn.execute(
                            "INSERT INTO briefs (written_at, event_slug, model, brief_json, cost_usd)"
                            " VALUES (?,?,?,?,?)",
                            (ev["brief"]["written_at"], slug, SEARCH_MODEL,
                             json.dumps(ev["brief"]), 0.03))
                        bconn.commit()
            b = ev.get("brief") or {}
            log.info("books: %s", counts)
            log.info("watching %s: %s (%d markets; brief: %s, %d facts, %d scenarios)",
                     slug, ev["title"], len(live),
                     ", ".join(f"{t} x{len(ps)}" for t, ps in (ev.get("teams") or {}).items())
                     or "NO ROSTER -- directions on player news are unreliable",
                     len(b.get("facts") or []), len(b.get("scenarios") or []))
            for sc in b.get("scenarios") or []:
                log.info("   scenario %-24s %s", sc["id"][:24], sc["trigger"][:70])
        if not watch:
            # Say WHICH: 2026-09-22 the whole MLB slate was dropped as "not
            # tight" when the quote endpoint was blocking us; the books were fine.
            log.error("no watched market to follow: %s", counts)
            return

        if fast_sources:
            # Opt-in: sources.py adds MLB game events and Bluesky beat
            # accounts (Google News only if sources_for is asked for it; its
            # robots.txt disallows /rss/). Each Source throttles itself, so
            # polling it every `poll_seconds` stays polite.
            from sources import sources_for
            extra = sources_for(tags, [ev["title"] for ev in watch.values()])
            log.info("fast sources: %s", ", ".join(x.name for x in extra))

        async def poll_all() -> list[list[dict[str, Any]]]:
            return await asyncio.gather(*(poll_feed(client, s, u) for s, u in feeds.items()),
                                        *(x.poll(client) for x in extra))

        with closing(connect()) as conn:
            rec = Recorder(conn, jev, quotes, watch, th)

            first = [h for batch in await poll_all() for h in batch]
            seen = {(h["url"], h["title"]) for h in first}
            log.info("baseline: %d existing items across %d feeds (%s)", len(first), len(feeds),
                     ", ".join(feeds))
            if replay:
                newest = sorted(first, key=lambda h: h["published_at"] or "", reverse=True)
                for h in newest[:replay]:
                    await rec.handle(h)

            deadline = time.monotonic() + minutes * 60
            while time.monotonic() < deadline and not replay:
                await asyncio.sleep(poll_seconds)
                batches = await poll_all()
                for h in (h for b in batches for h in b):
                    if time.monotonic() >= deadline:
                        break
                    key = (h["url"], h["title"])
                    if key in seen:
                        continue
                    seen.add(key)
                    try:
                        await asyncio.wait_for(rec.handle(h), timeout=HANDLE_TIMEOUT_S)
                    except asyncio.TimeoutError:
                        log.warning("headline timed out after %ds, skipped: %s",
                                    HANDLE_TIMEOUT_S, h["title"][:60])

            if rec.pending and not replay:
                log.info("waiting on %d price follow-ups (up to 30 min)...", len(rec.pending))
                try:
                    await asyncio.wait_for(asyncio.gather(*rec.pending, return_exceptions=True),
                                           timeout=35 * 60)
                except asyncio.TimeoutError:
                    log.warning("follow-ups did not finish in 35 min; abandoning them")
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
    # Which QUESTION earns its place? Signed +5m drift, per headline, split
    # at each question's threshold, among rows Jev gave a direction. A
    # question whose high side does not beat its low side is dead weight
    # on the live path. This is the reward signal for the question set.
    th = FastLaneThresholds()
    directional = [r for r in rows if r["effect"] in ("raises", "lowers") and r["mid_5m"] is not None]
    if directional:
        print(f"\n  {'question':<20}{'split':>8}{'above: heads':>14}{'drift':>9}"
              f"{'below: heads':>14}{'drift':>9}")
        for name, col, cut in (("reports_new_fact", "reports_new_fact", th.min_new_fact),
                               ("effect_conf", "effect_conf", th.min_effect_confidence),
                               ("size", "size", th.min_size)):
            sides = []
            for above in (True, False):
                per: dict[int, list[float]] = {}
                for r in directional:
                    if (r[col] >= cut) == above:
                        d = (r["mid_5m"] - (r["bid0"] + r["ask0"]) / 2) * (1 if r["effect"] == "raises" else -1)
                        per.setdefault(r["headline_id"], []).append(d)
                means = [statistics.mean(v) for v in per.values()]
                sides.append((len(means), f"{statistics.mean(means):+.4f}" if means else "     --"))
            print(f"  {name:<20}{cut:>8.2f}{sides[0][0]:>14}{sides[0][1]:>9}{sides[1][0]:>14}{sides[1][1]:>9}")
    print("\n  Positive signed drift means the book moved the way Jev said AFTER we saw the\n"
          "  headline. It has to beat the spread (~0.02) and the control's drift to matter,\n"
          "  and it needs dozens of acted headlines across several days before it is a finding.")
    score_scenarios(db)


def score_scenarios(db: str = DB_PATH) -> None:
    """
    Is the brief worth anything? docs/BRIEF.md step 3.

    For rows where Jev matched a pre-priced scenario: did the book move
    TOWARD the brief's fair price (share of the gap closed at +5 and +30
    min), and once settled, was fair_yes closer to the outcome than the
    price at the signal? Unit is the headline; within one, rows average.
    """
    with closing(connect(db)) as conn:
        rows = conn.execute(
            "SELECT * FROM signals WHERE fair_yes IS NOT NULL"
            " AND bid0 IS NOT NULL AND ask0 IS NOT NULL").fetchall()
    print(f"\n  scenario matches with a pre-written fair price: {len(rows)} rows, "
          f"{len({r['headline_id'] for r in rows})} headlines")
    if not rows:
        return
    by_sc: dict[str, list] = {}
    for r in rows:
        by_sc.setdefault(r["scenario"], []).append(r)
    print(f"  {'scenario':<28}{'heads':>6}{'gap0':>8}{'closed+5m':>11}{'closed+30m':>12}"
          f"{'brier fair':>12}{'brier mkt':>11}")
    for sc, rs in sorted(by_sc.items(), key=lambda kv: -len(kv[1])):
        def closed(col, rs=rs):
            per: dict[int, list[float]] = {}
            for r in rs:
                m0 = (r["bid0"] + r["ask0"]) / 2
                gap = r["fair_yes"] - m0
                if r[col] is None or abs(gap) < 0.01:
                    continue
                per.setdefault(r["headline_id"], []).append((r[col] - m0) / gap)
            v = [statistics.mean(x) for x in per.values()]
            return f"{statistics.mean(v):+.2f}" if v else "   --"
        gap0 = statistics.mean(abs(r["fair_yes"] - (r["bid0"] + r["ask0"]) / 2) for r in rs)
        res = [r for r in rs if r["resolved_outcome"] in ("0", "1")]
        if res:
            bf = statistics.mean((r["fair_yes"] - float(r["resolved_outcome"])) ** 2 for r in res)
            bm = statistics.mean(((r["bid0"] + r["ask0"]) / 2 - float(r["resolved_outcome"])) ** 2
                                 for r in res)
            bfs, bms = f"{bf:.4f}", f"{bm:.4f}"
        else:
            bfs = bms = "--"
        print(f"  {str(sc)[:27]:<28}{len({r['headline_id'] for r in rs}):>6}{gap0:>8.3f}"
              f"{closed('mid_5m'):>11}{closed('mid_30m'):>12}{bfs:>12}{bms:>11}")
    print("  closed = share of the gap between the price and the brief's fair price that\n"
          "  the book closed afterwards (1.0 = moved all the way to fair, 0 = did not move,\n"
          "  negative = moved away). Brier columns fill after --backfill.")


async def backfill(db: str = DB_PATH, pause: float = 2.0) -> None:
    """Fill resolved_outcome on signals from the settlement endpoint. 404 means not yet."""
    with closing(connect(db)) as conn:
        slugs = [r[0] for r in conn.execute(
            "SELECT DISTINCT market_slug FROM signals WHERE resolved_outcome IS NULL")]
        filled = 0
        async with AsyncPolymarketUS() as pm:
            for slug in slugs:
                try:
                    res = await pm.markets.settlement(slug)
                except Exception:  # noqa: BLE001 -- NotFoundError: still open
                    await asyncio.sleep(pause)
                    continue
                v = res.get("settlement")
                if v in (0, 1, "0", "1"):
                    conn.execute("UPDATE signals SET resolved_outcome=? WHERE market_slug=?",
                                 (str(int(v)), slug))
                    conn.commit()
                    filled += 1
                await asyncio.sleep(pause)
    log.info("backfill: %d of %d markets settled", filled, len(slugs))


def main() -> None:
    ap = argparse.ArgumentParser(description="Shadow-only recorder: Jev reads headlines, prices are followed.")
    ap.add_argument("--tags", default="nfl",
                    help="exchange tags; also selects the news feeds: " + ", ".join(FEEDS_BY_TAG))
    ap.add_argument("--start-window", type=float, default=6.0, help="hours ahead to look for events")
    ap.add_argument("--minutes", type=float, default=240.0, help="how long to poll")
    ap.add_argument("--poll", type=float, default=20.0, help="seconds between feed polls")
    ap.add_argument("--per-event", type=int, default=MAX_MARKETS_PER_EVENT)
    ap.add_argument("--replay", type=int, default=0,
                    help="smoke test: judge the newest N existing items, then exit")
    ap.add_argument("--no-brief", action="store_true",
                    help="skip the LLM roster brief (no Anthropic spend; directions unreliable)")
    ap.add_argument("--fast-sources", action="store_true",
                    help="also poll sources.py (MLB live game events, Bluesky; Google News is off by default)")
    ap.add_argument("--score", action="store_true")
    ap.add_argument("--backfill", action="store_true", help="fill settled outcomes on signals")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if args.backfill:
        asyncio.run(backfill())
        if not args.score:
            return
    if args.score:
        score()
        return
    cap = (args.minutes + RUN_OVERHEAD_MIN) * 60
    try:
        asyncio.run(asyncio.wait_for(
            run(tuple(t.strip() for t in args.tags.split(",")), args.start_window,
                args.minutes, args.poll, args.replay, args.per_event, not args.no_brief,
                args.fast_sources),
            timeout=cap))
    except asyncio.TimeoutError:
        log.error("hard cap of %.0f min reached; exiting. Rows already written are kept.",
                  cap / 60)


if __name__ == "__main__":
    main()
