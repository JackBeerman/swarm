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

from polymarket_us import AsyncPolymarketUS

from adapters import (
    PriceTracker,
    QuoteFetcher,
    fetch_events_across_tags,
    interleave_by_event,
    iter_event_markets,
    normalize_market,
)
from schemas import TriageVerdict
from questions import GateThresholds, StructuralLimits
from swarm import JevTriage, apply_gate, price_triage

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
    "objective_resolution",
    "self_contained",
    "research_would_help",
    "research_confidence",
]


#: Columns added after the first schema shipped. CREATE TABLE IF NOT
#: EXISTS does not alter an existing table, so a database from an earlier
#: run keeps the old shape and every insert fails on the new columns.
_MIGRATIONS = {
    "is_sports": "INTEGER",
    "stat_aggregation": "REAL",
    "pregame_information_edge": "REAL",
    "sports_market_type": "TEXT",
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
        "SELECT DISTINCT market_slug FROM verdicts WHERE seen_at >= ?",
        (cutoff,),
    ).fetchall()
    return {r["market_slug"] for r in rows}


def store(conn: sqlite3.Connection, v: TriageVerdict, market: dict, bbo: dict) -> None:
    bid, ask = bbo.get("bid"), bbo.get("ask")
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
            input_tokens, output_tokens
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            datetime.now(timezone.utc).isoformat(),
            v.market_slug,
            market.get("question"),
            market.get("volume_usd"),
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
        ),
    )
    conn.commit()


# --------------------------------------------------------------------------
# collection
# --------------------------------------------------------------------------

async def collect(
    min_volume: float = 250.0,
    limit: int = 200,
    concurrency: int = 2,
    events: int = 50,
    cooldown_hours: float = 20.0,
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
    limits = StructuralLimits(min_volume_usd=min_volume)
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
        evs = await fetch_events_across_tags(pm, per_tag=max(1, events // 7))
        pairs = iter_event_markets({"events": evs})
        total = len(pairs)
        # Interleave before truncating. Events flatten into long runs of
        # near-identical legs, so the head of the list is one sport --
        # a first sweep took 40 markets and every one was MLB.
        pairs = interleave_by_event(
            [p for p in pairs if p[0].get("slug") not in skip]
        )[:limit]
        log.info("%d events across tags -> %d open markets -> %d this sweep",
                 len(evs), total, len(pairs))

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
        v = TriageVerdict(
            market_slug=r["market_slug"],
            outcome_type=r["outcome_type"] or "unknown",
            outcome_type_confidence=r["outcome_type_confidence"] or 0.0,
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
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s"
    )

    if args.collect:
        asyncio.run(collect(
            min_volume=args.min_volume,
            limit=args.limit,
            concurrency=args.concurrency,
            events=args.events,
            cooldown_hours=args.cooldown_hours,
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
