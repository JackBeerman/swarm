"""
shadow.py -- triage-only calibration runner. Places no orders, ever.

Purpose: the thresholds in GateConfig are guesses. This runs Tier 1 against
live markets, stores every raw Jev probability, and lets you re-score the
gate offline at any threshold without spending another cent.

That last property is the point. Because the verdicts are stored as raw
probabilities rather than booleans, `--analyze` can answer "what would my
escalation rate have been at min_fresh_catalyst=0.85?" from data you
already paid for. Sweep thresholds on disk, not against the API.

    python shadow.py --collect --hours 24       # gather verdicts
    python shadow.py --analyze                  # escalation rate + cost model
    python shadow.py --sweep fresh_catalyst     # threshold sensitivity

Target escalation rate is roughly 3-6%. Above ~10% the gate is too loose
and Tier 2/3 will eat the treasury before you learn anything.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sqlite3
import statistics
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Any

# Before `from swarm import ...`: swarm.py reads TYPESAFE_DEFAULT_MODEL at
# import time, so the pin in .env is ignored unless it is loaded first.
# Without this, `python shadow.py --collect` from a fresh terminal either
# raises on a missing TYPESAFE_API_KEY or -- worse -- runs against
# jev-latest, which floats, and quietly recalibrates every threshold.
from config import load_dotenv_if_present

load_dotenv_if_present()

from polymarket_us import AsyncPolymarketUS  # noqa: E402

from adapters import (
    PriceTracker,
    QuoteFetcher,
    DEFAULT_TAG_MIX,
    fetch_events_across_tags,
    derive_notionals,
    interleave_by_event,
    price_band_reject,
    iter_event_markets,
    normalize_market,
)
from schemas import TriageVerdict
from questions import GateThresholds, StructuralLimits
from swarm import JevTriage, _hours_until, apply_gate, price_triage

log = logging.getLogger("shadow")

DB_PATH = os.getenv("SHADOW_DB", "shadow.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS verdicts (
    id                       INTEGER PRIMARY KEY,
    seen_at                  TEXT NOT NULL,
    market_slug              TEXT NOT NULL,
    question                 TEXT,
    volume_usd               REAL,
    bid                      REAL,
    ask                      REAL,
    spread                   REAL,
    federal_policy_outcome      REAL,
    defense_or_military         REAL,
    us_election_or_appointment  REAL,
    objective_resolution        REAL,
    self_contained              REAL,
    research_would_help         REAL,
    research_confidence         REAL,
    outcome_type                TEXT,
    outcome_type_confidence     REAL,
    is_sports                   INTEGER,
    stat_aggregation            REAL,
    pregame_information_edge    REAL,
    sports_market_type          TEXT,
    structural_reject           TEXT,
    escalate                 INTEGER NOT NULL,
    veto_reason              TEXT,
    gate_score               REAL,
    latency_ms               REAL,
    input_tokens             INTEGER,
    output_tokens            INTEGER,
    -- what --score and later analysis need and cannot reconstruct.
    -- volume/liquidity are derived from the quote at triage time and
    -- exist nowhere else; model is which Jev served the answer, since an
    -- unpinned id floats; period/hours_to_event prove pre-game vs in-play.
    liquidity_usd            REAL,
    bid_shares               REAL,
    ask_shares               REAL,
    model                    TEXT,
    outcome                  TEXT,
    event_title              TEXT,
    period                   TEXT,
    hours_to_event           REAL,
    -- what Jev actually saw. Without these a stored verdict cannot be
    -- REPLAYED against a rewritten question, and a learning loop that
    -- proposes question edits has nothing to validate them on.
    description              TEXT,
    tags                     TEXT,
    -- resolution is backfilled later; this is what makes the data
    -- worth anything. A gate you never scored against outcomes is
    -- just a rate limiter.
    resolved_outcome         TEXT,
    resolved_at              TEXT
);
CREATE INDEX IF NOT EXISTS idx_slug ON verdicts(market_slug);
CREATE INDEX IF NOT EXISTS idx_seen ON verdicts(seen_at);
"""

FIELDS = [
    "federal_policy_outcome",
    "defense_or_military",
    "us_election_or_appointment",
    "politics_or_government",
    "objective_resolution",
    "self_contained",
    "research_would_help",
    "research_confidence",
]


#: Columns added after the first schema shipped. CREATE TABLE IF NOT
#: EXISTS does not alter an existing table, so a database from an earlier
#: run keeps the old shape and every insert fails on the new columns.
_MIGRATIONS = {
    "event_slug": "TEXT",
    "category": "TEXT",
    "market_type": "TEXT",
    "politics_or_government": "REAL",
    "is_sports": "INTEGER",
    "stat_aggregation": "REAL",
    "pregame_information_edge": "REAL",
    "sports_market_type": "TEXT",
    "liquidity_usd": "REAL",
    "bid_shares": "REAL",
    "ask_shares": "REAL",
    "model": "TEXT",
    "outcome": "TEXT",
    "event_title": "TEXT",
    "period": "TEXT",
    "hours_to_event": "REAL",
    "description": "TEXT",
    "tags": "TEXT",
}


def connect(path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    have = {r["name"] for r in conn.execute("PRAGMA table_info(verdicts)")}
    for col, decl in _MIGRATIONS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE verdicts ADD COLUMN {col} {decl}")
            log.info("shadow.db: added column %s", col)
    conn.commit()
    return conn


def recently_seen(conn: sqlite3.Connection, hours: float) -> set[str]:
    """
    Slugs triaged within the last `hours`.

    Without this, every sweep re-triages the same markets. At the daemon's
    300s poll that is 288 evaluations per market per day -- the same
    market, the same description, the same answer, 288 times. Triage is
    cheap per call and ruinous in aggregate.
    """
    if hours <= 0:
        return set()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    rows = conn.execute(
        # Structural rejects never reached Jev, so re-checking them costs
        # only a quote -- and a Saturday-night prop book rejected as
        # spread=0.06 is exactly the one that tightens by Sunday morning.
        # Cooling those down skipped tomorrow's best candidates.
        """SELECT DISTINCT market_slug FROM verdicts
           WHERE seen_at >= ? AND structural_reject IS NULL""",
        (cutoff,),
    ).fetchall()
    return {r["market_slug"] for r in rows}


def store(conn: sqlite3.Connection, v: TriageVerdict, market: dict, bbo: dict) -> None:
    bid, ask = bbo.get("bid"), bbo.get("ask")
    # The normalized market carries volume_usd=None by design (the field
    # is not on the wire); it is derived from the quote. Reading it off the
    # market wrote NULL into every row and made --sweep of the volume floor
    # impossible offline.
    n = derive_notionals(bbo)
    conn.execute(
        """INSERT INTO verdicts (
            seen_at, market_slug, question, volume_usd, bid, ask, spread,
            federal_policy_outcome, defense_or_military,
            us_election_or_appointment, objective_resolution, self_contained,
            research_would_help, research_confidence, outcome_type,
            outcome_type_confidence,
            is_sports, stat_aggregation, pregame_information_edge,
            sports_market_type,
            structural_reject,
            escalate, veto_reason, gate_score, latency_ms,
            input_tokens, output_tokens,
            liquidity_usd, bid_shares, ask_shares, model, outcome,
            event_title, period, hours_to_event, description, tags,
            politics_or_government, event_slug, category, market_type
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                  ?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            datetime.now(timezone.utc).isoformat(),
            v.market_slug,
            market.get("question"),
            n["volume_usd"],
            bid,
            ask,
            (float(ask) - float(bid)) if bid and ask else None,
            v.federal_policy_outcome,
            v.defense_or_military,
            v.us_election_or_appointment,
            v.objective_resolution,
            v.self_contained,
            v.research_would_help,
            v.research_confidence,
            v.outcome_type,
            v.outcome_type_confidence,
            int(v.is_sports),
            v.stat_aggregation,
            v.pregame_information_edge,
            v.sports_market_type,
            v.structural_reject,
            int(v.escalate),
            v.veto_reason,
            v.gate_score,
            v.latency_ms,
            v.input_tokens,
            v.output_tokens,
            n["liquidity_usd"],
            bbo.get("bid_shares"),
            bbo.get("ask_shares"),
            v.model,
            market.get("outcome"),
            market.get("event_title"),
            market.get("period"),
            _hours_until(market.get("event_at")),
            (market.get("description") or "")[:1500],
            json.dumps(market.get("tags") or []),
            v.politics_or_government,
            market.get("event_slug"),
            market.get("category"),
            market.get("market_type"),
        ),
    )
    conn.commit()


# --------------------------------------------------------------------------
# collection
# --------------------------------------------------------------------------

def _price_band_reject(market: dict[str, Any], limits: StructuralLimits) -> bool:
    """Prescreen on the listed prices; lives in adapters so inplay.py shares it."""
    return price_band_reject(market, limits.min_price, limits.max_price)


async def collect(
    min_volume: float = 250.0,
    limit: int = 200,
    concurrency: int = 2,
    events: int = 50,
    cooldown_hours: float = 20.0,
    tags: "tuple[str, ...] | None" = None,
    min_hours_to_event: float | None = None,
    start_window_hours: float | None = None,
    max_spread: float | None = None,
) -> None:
    """
    One sweep. Triage only. No orders, no Tier 2/3.

    `limit` bounds MARKETS, not events. It previously went straight to
    events.list, where a value of 200 meant 200 events -- and 50 events
    already flatten to ~10,800 markets, so the documented `--limit 200`
    would have attempted tens of thousands of quote requests.
    """
    conn = connect()
    jev = JevTriage()
    tracker = PriceTracker()
    gate = GateThresholds()
    # --min-volume overrides the structural floor rather than a query
    # parameter the gateway ignores. Volume is derived from the quote, so
    # it cannot be applied before the bbo fetch.
    # --min-hours overrides the research window for THIS run only; the
    # default in questions.py is untouched. The 6h default was set for
    # general markets with a Tier 2/3 research step ahead of them. For a
    # pre-game sweep run the morning of, kickoff is ~4h out, and 6h
    # rejects the entire early slate as "too soon".
    limit_kwargs: dict[str, Any] = {"min_volume_usd": min_volume}
    if min_hours_to_event is not None:
        limit_kwargs["min_hours_to_close"] = min_hours_to_event
    # The 0.04 default was calibrated on 176 markets across every tag.
    # Pre-game NFL prop books are wider than that on a Saturday morning:
    # the first sweep sent 97 markets to the filter and 95 came back,
    # almost all spread > 0.04. Per-run override so a wider sweep can be
    # collected without changing the default in questions.py.
    if max_spread is not None:
        limit_kwargs["max_spread"] = max_spread
    limits = StructuralLimits(**limit_kwargs)
    seen = skipped = 0

    skip = recently_seen(conn, cooldown_hours)
    if skip:
        log.info("cooldown: %d markets triaged in the last %.0fh will be "
                 "skipped", len(skip), cooldown_hours)

    async with AsyncPolymarketUS() as pm:  # public endpoints, no auth needed
        # Collect via EVENTS, not markets.list: closes_at and tags live on
        # the event, and both change how triage should read a price level.
        # `closed: False` is what selects open markets. `active: True`
        # does NOT -- it returns resolved markets, which stay active=True.
        # Sample across tags rather than taking the default listing page,
        # which is ~half sports. Every market in the first calibration
        # sweep came back `contested_event`, which makes a threshold
        # sweep a cliff rather than a curve.
        tag_mix = tags or DEFAULT_TAG_MIX
        # A start-time window is what actually selects THIS week's games.
        # The nfl tag page in default order is 25 season futures and awards
        # -- MVP, division winners, sacks leader -- and none of Sunday's 15
        # game events. Verified live: startTimeMin/Max around the weekend
        # returns exactly the game events, all period=NS, startTime=kickoff.
        extra: dict[str, Any] = {}
        if start_window_hours is not None:
            now = datetime.now(timezone.utc)
            extra = {
                "startTimeMin": (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "startTimeMax": (now + timedelta(hours=start_window_hours)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        evs = await fetch_events_across_tags(
            pm, tags=tag_mix, per_tag=max(1, events // len(tag_mix)),
            extra=extra,
        )
        pairs = iter_event_markets({"events": evs})
        total = len(pairs)
        # Interleave before truncating. Events flatten into long runs of
        # near-identical legs, so the head of the list is one sport --
        # a first sweep took 40 markets and every one was MLB.
        # Pre-screen on the events payload BEFORE spending a paced quote.
        # A game event carries ~800 markets, and the ones listed first are
        # extreme alt-lines -- "cover 17.5" at 0.985/0.99 -- that the price
        # band rejects anyway. Without this, a 200-quote budget goes almost
        # entirely to 0.98 lines and the main lines near 0.50 are never
        # reached. `outcomePrices` is a JSON string of two prices; whether
        # they are [bid, ask] of the primary side or [yes, no] is not yet
        # confirmed, so this rejects only when BOTH sit outside the band on
        # the same side, which is correct under either reading.
        prescreened = [p for p in pairs if p[0].get("slug") not in skip]
        kept = []
        for m, ev in prescreened:
            if _price_band_reject(m, limits):
                continue
            kept.append((m, ev))
        pairs = interleave_by_event(kept)[:limit]
        log.info("%d events across tags -> %d open -> %d past prescreen "
                 "-> %d this sweep",
                 len(evs), total, len(kept), len(pairs))

        quotes = QuoteFetcher(pm, concurrency=concurrency)

        async def one(market: dict[str, Any], event: dict[str, Any]) -> None:
            nonlocal seen, skipped
            slug = market.get("slug")
            if not slug:
                skipped += 1
                return
            bbo = await quotes.bbo(slug)
            if bbo is None or bbo["bid"] is None or bbo["ask"] is None:
                skipped += 1
                return
            try:
                tracker.observe(slug, (bbo["bid"] + bbo["ask"]) / 2.0)
                norm = normalize_market(market, event)
                v = await jev.evaluate(norm, bbo, gate, tracker, limits)
            except Exception as exc:
                log.warning("triage failed on %s: %s", slug, exc)
                return
            store(conn, v, norm, bbo)
            conn.commit()
            seen += 1
            flag = "ESCALATE" if v.escalate else "-"
            log.info("%-8s %-45s %s", flag, slug[:45], v.veto_reason or "")

        await asyncio.gather(*(one(m, e) for m, e in pairs))
        log.info("%s", quotes.report())

    await jev.aclose()
    conn.commit()
    conn.close()
    log.info("triaged %d markets (%d skipped)", seen, skipped)


# --------------------------------------------------------------------------
# resolution backfill
# --------------------------------------------------------------------------

async def backfill(limit: int = 500, pause: float = 2.0) -> None:
    """
    Fill `resolved_outcome` for markets that have since settled.

    This is the step that turns collected verdicts into an answer. Without
    it you can measure how OFTEN the gate escalates but never whether it
    escalated the right markets -- a gate you never scored against
    outcomes is just a rate limiter.

    `markets.settlement(slug)` returns {"settlement": 1|0} once a market
    resolves and 404s while it is still open, so a NotFoundError here
    means "not yet", not "broken".
    """
    conn = connect()
    rows = conn.execute(
        """SELECT DISTINCT market_slug FROM verdicts
           WHERE resolved_outcome IS NULL
           ORDER BY seen_at DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    slugs = [r["market_slug"] for r in rows]
    log.info("%d markets awaiting resolution", len(slugs))

    filled = pending = failed = 0
    async with AsyncPolymarketUS() as pm:
        for slug in slugs:
            res = None
            terminal = False
            delay = pause
            # Two different failures wearing the same shape. NotFoundError
            # is the API answering "not settled yet" and is final for this
            # pass. RateLimitError is Cloudflare returning an HTML block
            # page -- retryable, and it arrived at 1.5s spacing, so the
            # settlement endpoint is tighter than the quote endpoint.
            for attempt in range(4):
                try:
                    res = await pm.markets.settlement(slug)
                    break
                except Exception as exc:
                    name = type(exc).__name__
                    if name == "NotFoundError":
                        terminal = True
                        break
                    if attempt == 3:
                        failed += 1
                        log.debug("settlement failed for %s: %s",
                                  slug, str(exc)[:80])
                        break
                    await asyncio.sleep(delay)
                    delay *= 2
            if terminal:
                pending += 1
                await asyncio.sleep(pause)
                continue
            if res is None:
                await asyncio.sleep(pause)
                continue

            settlement = res.get("settlement")
            try:
                sval = float(settlement) if settlement is not None else None
            except (TypeError, ValueError):
                sval = None
            if sval is None or sval not in (0.0, 1.0):
                # A push on a whole-number line (settlement 0.5) or an
                # unexpected shape. int(0.5) would silently record a loss;
                # a non-numeric string would raise and abort the loop.
                # Record nothing and move on.
                pending += 1
                if settlement is not None:
                    log.warning("non-binary settlement %r for %s; skipped",
                                settlement, slug)
            else:
                conn.execute(
                    """UPDATE verdicts
                       SET resolved_outcome = ?, resolved_at = ?
                       WHERE market_slug = ? AND resolved_outcome IS NULL""",
                    (str(int(sval)),
                     datetime.now(timezone.utc).isoformat(), slug),
                )
                conn.commit()
                filled += 1
                log.info("resolved %-46s -> %s", slug[:46], settlement)
            await asyncio.sleep(pause)

    conn.close()
    log.info("backfill: %d resolved, %d still open, %d errors",
             filled, pending, failed)


def _event_key(slug: str) -> str:
    """
    The game/event a market belongs to, from its slug. Markets inside one
    event resolve TOGETHER -- a 9-3 final loses every low "over" at once --
    so the honest sample size is the number of events, not markets.
    """
    import re
    m = re.search(r"([a-z0-9]+-[a-z0-9]+-[a-z0-9]+-\d{4}-\d{2}-\d{2})", slug or "")
    return m.group(1) if m else (slug or "").rsplit("-", 2)[0]


def _row_event(r: sqlite3.Row) -> str:
    """The exchange's event slug when stored; the slug regex for old rows."""
    return r["event_slug"] or _event_key(r["market_slug"])


def calibrate(db: str = DB_PATH, max_spread: float = 0.10) -> None:
    """
    The most general thing the betting slips can teach: when the market
    says X, how often does it happen?

    Pools EVERY settled market with a quote -- escalated or not, any
    category -- so it needs no model and grows with every market
    collected. It is arithmetic, so it lives in code. A persistent gap
    between price and frequency is a bias you can trade without research.

    Read the `events` column before the `gap` column. On 2026-09-20 the
    0.92-0.96 band showed 94% priced vs 82% realized over 93 markets and
    +164% for buying NO -- and nearly all 93 were "overs" from ~15 games
    on one low-scoring Sunday. That is a weekend, not a bias, until it
    survives many days and many categories.
    """
    with closing(connect(db)) as conn:
        rows = conn.execute(
            """SELECT market_slug, bid, ask, resolved_outcome, event_slug
               FROM verdicts
               WHERE resolved_outcome IS NOT NULL
                 AND bid IS NOT NULL AND ask IS NOT NULL
               GROUP BY market_slug"""
        ).fetchall()
    rows = [r for r in rows if (r["ask"] - r["bid"]) <= max_spread]
    print("=" * 78)
    print(f"  calibration over {len(rows)} settled markets "
          f"({len({_row_event(r) for r in rows})} events), "
          f"spread <= {max_spread}")
    print("=" * 78)
    if not rows:
        print("\n  nothing settled yet. Run --backfill.")
        return
    buckets = [(0.0, 0.10), (0.10, 0.30), (0.30, 0.50), (0.50, 0.70),
               (0.70, 0.85), (0.85, 0.92), (0.92, 0.96), (0.96, 1.01)]
    print(f"\n  {'price':<11}{'mkts':>5}{'events':>7}{'priced':>8}{'won':>7}"
          f"{'gap':>8}{'YES@ask':>10}{'NO@1-bid':>10}")
    for lo, hi in buckets:
        rs = [r for r in rows if lo <= (r["bid"] + r["ask"]) / 2 < hi]
        if not rs:
            continue
        n = len(rs)
        ev = len({_row_event(r) for r in rs})
        priced = sum(r["ask"] for r in rs) / n
        won = sum(r["resolved_outcome"] == "1" for r in rs) / n
        yes = sum((1 - r["ask"]) if r["resolved_outcome"] == "1" else -r["ask"]
                  for r in rs) / sum(r["ask"] for r in rs)
        no_stake = sum(1 - r["bid"] for r in rs)
        no = (sum(r["bid"] if r["resolved_outcome"] == "0" else -(1 - r["bid"])
                  for r in rs) / no_stake) if no_stake else 0.0
        print(f"  {lo:.2f}-{min(hi, 1.0):.2f}  {n:>5}{ev:>7}{priced:>8.3f}{won:>7.0%}"
              f"{won - priced:>+8.3f}{yes:>+10.0%}{no:>+10.0%}")
    print("\n  markets in one event resolve together; `events` is the real n.\n"
          "  A gap is a hypothesis until it holds across days AND categories.")


def score(db: str = DB_PATH) -> None:
    """
    Was the gate escalating the RIGHT markets?

    Tier 1 produces no probability of its own, so the thing to score is
    the market: for each resolved verdict, how far was the mid-price from
    what actually happened. Brier = (mid - outcome)^2.

    A gate that is working escalates markets whose price was MORE wrong
    than average -- that is where edge lives. If escalated markets score
    the same as rejected ones, the gate is selecting on something that
    does not predict mispricing, and no threshold sweep will fix that.
    """
    with closing(connect(db)) as conn:
        rows = conn.execute(
            """SELECT escalate, bid, ask, gate_score, is_sports,
                      resolved_outcome, veto_reason
               FROM verdicts
               WHERE resolved_outcome IS NOT NULL
                 AND bid IS NOT NULL AND ask IS NOT NULL
                 AND structural_reject IS NULL"""
        ).fetchall()

    print("=" * 62)
    print(f"  {len(rows)} resolved markets with a quote at triage time")
    print("=" * 62)
    if not rows:
        print("\n  nothing resolved yet. Run --backfill after markets settle;")
        print("  until then the gate is unscored and every threshold in")
        print("  questions.py is still a guess.")
        return

    def brier(rs):
        vals = []
        for r in rs:
            mid = (float(r["bid"]) + float(r["ask"])) / 2.0
            vals.append((mid - float(r["resolved_outcome"])) ** 2)
        return vals

    esc = [r for r in rows if r["escalate"]]
    rej = [r for r in rows if not r["escalate"]]

    print(f"\n  {'bucket':<16} {'n':>5} {'market brier':>13} {'base rate':>11}")
    for name, rs in (("escalated", esc), ("rejected", rej), ("all", rows)):
        if not rs:
            print(f"  {name:<16} {0:>5}")
            continue
        b = brier(rs)
        base = sum(float(r["resolved_outcome"]) for r in rs) / len(rs)
        print(f"  {name:<16} {len(rs):>5} {statistics.mean(b):>13.4f} "
              f"{base:>11.2f}")

    if esc and rej:
        be, br = statistics.mean(brier(esc)), statistics.mean(brier(rej))
        print()
        if be > br:
            print(f"  escalated markets were MORE mispriced "
                  f"({be:.4f} vs {br:.4f}) -- the gate is selecting for "
                  f"the thing you want.")
        else:
            print(f"  escalated markets were no more mispriced "
                  f"({be:.4f} vs {br:.4f}). The gate is not finding "
                  f"mispricing; sweeping min_gate_score will not fix that.")

    sports = [r for r in rows if r["is_sports"]]
    if sports:
        print(f"\n  sports subset: {len(sports)} resolved, "
              f"market brier {statistics.mean(brier(sports)):.4f}")


# --------------------------------------------------------------------------
# offline analysis
# --------------------------------------------------------------------------

def _rescore(rows: list[sqlite3.Row], gate: GateThresholds) -> list[TriageVerdict]:
    """Re-run the gate over stored probabilities. Costs nothing."""
    out = []
    for r in rows:
        # Structurally rejected markets never reached Jev, so they cannot
        # be re-scored -- carry them through as permanent rejections.
        if r["structural_reject"]:
            out.append(TriageVerdict(market_slug=r["market_slug"],
                                     structural_reject=r["structural_reject"],
                                     veto_reason=f"structural: {r['structural_reject']}"))
            continue
        keys = r.keys()
        v = TriageVerdict(
            market_slug=r["market_slug"],
            outcome_type=r["outcome_type"] or "unknown",
            outcome_type_confidence=r["outcome_type_confidence"] or 0.0,
            # The sports fields MUST be carried. Without them every stored
            # sports row re-scored as non-sports: research_would_help sat
            # at its 0.0 default, hit the floor, and --analyze reported ~0%
            # escalation with research_wont_help as the top reason -- the
            # database was right and the analysis was wrong.
            is_sports=bool(r["is_sports"]) if "is_sports" in keys else False,
            stat_aggregation=(r["stat_aggregation"] or 0.0)
            if "stat_aggregation" in keys else 0.0,
            pregame_information_edge=(r["pregame_information_edge"] or 0.0)
            if "pregame_information_edge" in keys else 0.0,
            sports_market_type=(r["sports_market_type"] or "unknown")
            if "sports_market_type" in keys else "unknown",
            **{f: (r[f] or 0.0) for f in FIELDS},
        )
        out.append(apply_gate(v, gate))
    return out


def analyze(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT * FROM verdicts").fetchall()
    if not rows:
        print("no verdicts collected yet -- run --collect first")
        return

    n = len(rows)
    verdicts = _rescore(rows, GateThresholds())
    esc = [v for v in verdicts if v.escalate]
    rate = len(esc) / n

    print(f"\n{'=' * 62}")
    print(f"  {n} markets triaged")
    print(f"{'=' * 62}\n")

    print(f"  escalation rate      {rate:>8.1%}   ({len(esc)}/{n})")
    if rate > 0.10:
        print("                       ^ TOO LOOSE -- tighten before spending")
    elif rate < 0.01:
        print("                       ^ very tight; you may see no trades at all")
    else:
        print("                       ^ in the workable band")

    vetoes: dict[str, int] = {}
    for v in verdicts:
        if v.veto_reason:
            key = v.veto_reason.split(":")[0].split("=")[0].strip()
            vetoes[key] = vetoes.get(key, 0) + 1
    print("\n  rejection reasons")
    for k, c in sorted(vetoes.items(), key=lambda kv: -kv[1]):
        print(f"    {k:<28} {c:>5}  ({c / n:>5.1%})")

    print("\n  signal distributions (p10 / median / p90)")
    for f in FIELDS:
        vals = sorted(r[f] for r in rows)
        p10 = vals[int(0.10 * len(vals))]
        p90 = vals[int(0.90 * len(vals)) - 1]
        print(f"    {f:<28} {p10:>5.2f} / {statistics.median(vals):>5.2f} / {p90:>5.2f}")

    lat = [r["latency_ms"] for r in rows if r["latency_ms"]]
    if lat:
        lat.sort()
        print(
            f"\n  jev latency          median {statistics.median(lat):.0f}ms"
            f"   p95 {lat[int(0.95 * len(lat)) - 1]:.0f}ms"
        )

    # ---- cost projection -------------------------------------------
    tin = sum(r["input_tokens"] or 0 for r in rows)
    tout = sum(r["output_tokens"] or 0 for r in rows)
    triage_usd = price_triage(tin, tout)
    per_escalation = 0.20  # measured Tier 2+3 cost; update from real runs
    # Jev is priced at $0.042/M in, $0 out -- triage is effectively free.

    print(f"\n  {'-' * 58}")
    print("  cost model")
    print(f"    triage, this sample      ${triage_usd:>8.4f}  ({n} calls)")
    if tin == 0:
        print("      (no token counts stored for this sample)")
    else:
        print(f"      (${triage_usd / n * 1000:.4f} per 1,000 markets triaged)")
    print(f"    would-be tier 2+3        ${len(esc) * per_escalation:>8.2f}"
          f"  ({len(esc)} x ${per_escalation})")
    total = triage_usd + len(esc) * per_escalation
    print(f"    total                    ${total:>8.2f}")
    if total > 0:
        print(f"\n    at this rate, $90 of usable treasury buys ~{90 / total:.0f}"
              f" sweeps of {n} markets")
        print(f"    or roughly {int(90 / per_escalation)} full evaluations before"
              " the floor")
    print()


def sweep(conn: sqlite3.Connection, field: str) -> None:
    """
    Escalation rate as `min_gate_score` moves. This is the one knob.

    An earlier version swept individual signal thresholds. It reported 0%
    at every value including 0.0, because the other thresholds still bound
    -- it isolated nothing. The gate is now a single composite score, so
    there is exactly one curve worth looking at.
    """
    rows = conn.execute("SELECT * FROM verdicts").fetchall()
    if not rows:
        print("no verdicts collected yet")
        return
    n = len(rows)

    if field not in ("gate_score", "gate"):
        print(f"  note: the gate is a single composite score; ignoring "
              f"{field!r} and sweeping min_gate_score.\n")

    print(f"\n  sweeping min_gate_score   (n={n})")
    print(f"  target escalation band: 3-6%\n")
    print(f"    {'threshold':>10}  {'escalation':>11}  {'count':>6}   "
          f"{'est. $ per 1k markets':>21}")

    per_escalation = 0.20
    best = None
    for i in range(51):
        t = i / 50
        esc = sum(1 for v in _rescore(rows, GateThresholds(min_gate_score=t))
                  if v.escalate)
        rate = esc / n
        cost = rate * 1000 * per_escalation
        bar = "#" * int(50 * rate)
        marker = ""
        if 0.03 <= rate <= 0.06 and best is None:
            best = t
            marker = "  <-- in band"
        print(f"    {t:>10.2f}  {rate:>10.1%}  {esc:>6}   ${cost:>20.2f} {bar}{marker}")

    print()
    if best is not None:
        print(f"  suggested min_gate_score = {best:.2f}")
    else:
        print("  no threshold lands in the 3-6% band on this sample.")
        print("  either collect more markets or revisit the signal weights.")
    print()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--collect", action="store_true")
    ap.add_argument("--analyze", action="store_true")
    ap.add_argument("--sweep", metavar="FIELD")
    ap.add_argument("--backfill", action="store_true",
                    help="fill resolved_outcome for settled markets")
    ap.add_argument("--calibrate", action="store_true",
                    help="price vs realized frequency over every settled "
                         "market, with event counts")
    ap.add_argument("--score", action="store_true",
                    help="was the gate escalating the RIGHT markets?")
    ap.add_argument("--tags", default=None,
                    help="comma-separated tag slugs to sample (e.g. sports)")
    ap.add_argument("--min-volume", type=float,
                    default=StructuralLimits().min_volume_usd,
                    help="override the structural volume floor")
    ap.add_argument("--limit", type=int, default=200,
                    help="max MARKETS to triage this sweep (not events)")
    ap.add_argument("--events", type=int, default=50,
                    help="events to fetch; 50 flattens to ~10,800 markets")
    ap.add_argument("--concurrency", type=int, default=2,
                    help="concurrent quote requests; >2 gets rate-limited")
    ap.add_argument("--cooldown-hours", type=float, default=20.0,
                    help="skip markets triaged this recently; 0 disables")
    ap.add_argument("--min-hours", type=float, default=None,
                    help="research window on the EVENT clock for this run; "
                         "default keeps questions.py (6h, which rejects a "
                         "same-morning kickoff)")
    ap.add_argument("--start-window", type=float, default=None,
                    help="only events starting within this many hours "
                         "(startTimeMin/Max). The nfl tag page alone is "
                         "season futures; 48 selects the weekend's games")
    ap.add_argument("--max-spread", type=float, default=None,
                    help="structural spread ceiling for this run; default "
                         "keeps questions.py (0.04, which rejected ~98%% "
                         "of Saturday-morning NFL props)")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s"
    )

    if args.backfill:
        asyncio.run(backfill())
        return 0
    if args.score:
        score()
        return 0
    if args.calibrate:
        calibrate()
        return 0
    if args.collect:
        asyncio.run(collect(
            min_volume=args.min_volume,
            limit=args.limit,
            concurrency=args.concurrency,
            events=args.events,
            cooldown_hours=args.cooldown_hours,
            tags=tuple(t.strip() for t in args.tags.split(",")) if args.tags else None,
            min_hours_to_event=args.min_hours,
            start_window_hours=args.start_window,
            max_spread=args.max_spread,
        ))
    if args.analyze:
        with closing(connect()) as conn:
            analyze(conn)
    if args.sweep:
        with closing(connect()) as conn:
            sweep(conn, args.sweep)
    if not (args.collect or args.analyze or args.sweep):
        ap.print_help()


if __name__ == "__main__":
    main()
