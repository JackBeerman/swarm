"""
traces.py -- what the expensive tiers believed, kept so it can be scored.

Until this existed, a paper or live cycle remembered nothing but its cost:
the only tables written were `spend` and `halts`. Every evaluation built a
full PipelineResult -- three gatherers' facts, Tier 3's probability and
confidence, the sized order -- and dropped it. A run could spend real money
on research and leave no way to ask, afterwards, whether Tier 3's 0.89 was
any good.

This is the raw layer a learning loop stands on (cf. WikiSkill,
arXiv:2608.27454: immutable execution traces, scored against ground
truth). Nothing here is read by the pipeline; it is written once per
escalated evaluation and resolved later by a settlement backfill.

One row per escalated evaluation. Non-escalated markets cost nothing past
Tier 1 and are already in shadow.db when collected there.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import statistics
from contextlib import closing
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("traces")

TRACES_DB = os.getenv("TRACES_DB", "traces.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS evaluations (
    id              INTEGER PRIMARY KEY,
    at              TEXT NOT NULL,
    mode            TEXT NOT NULL,          -- paper | live
    market_slug     TEXT NOT NULL,
    question        TEXT,
    outcome         TEXT,
    event_title     TEXT,
    description     TEXT,                   -- what the models saw, for replay
    tags            TEXT,
    bid             REAL,
    ask             REAL,
    gate_score      REAL,
    facts_json      TEXT,                   -- Tier 2: every gatherer's summary
    n_sources       INTEGER,
    signal_side     TEXT,                   -- Tier 3
    signal_prob     REAL,
    signal_conf     REAL,
    order_side      TEXT,                   -- sizing, in code
    order_qty       INTEGER,
    order_price     REAL,
    order_notional  REAL,
    order_edge      REAL,
    halted_at       TEXT,                   -- why it stopped, if it did
    cost_usd        REAL,
    resolved_outcome TEXT                   -- filled by a settlement backfill
);
CREATE INDEX IF NOT EXISTS idx_eval_slug ON evaluations(market_slug);
"""


def connect(path: str = TRACES_DB) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def record(conn: sqlite3.Connection, mode: str, result: Any,
           market: dict[str, Any], bbo: dict[str, Any]) -> None:
    """
    Persist one PipelineResult. Never raises into the trading loop: a
    failure to write a trace must not stop an evaluation, so it is logged
    and swallowed -- the one place in this codebase where that is right.
    """
    try:
        facts = [f.model_dump(mode="json") for f in (result.facts or [])]
        sig, order, tri = result.signal, result.order, result.triage
        conn.execute(
            """INSERT INTO evaluations (
                   at, mode, market_slug, question, outcome, event_title,
                   description, tags, bid, ask, gate_score, facts_json,
                   n_sources, signal_side, signal_prob, signal_conf,
                   order_side, order_qty, order_price, order_notional,
                   order_edge, halted_at, cost_usd)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                datetime.now(timezone.utc).isoformat(), mode,
                result.market_slug, market.get("question"),
                market.get("outcome"), market.get("event_title"),
                (market.get("description") or "")[:1500],
                json.dumps(market.get("tags") or []),
                bbo.get("bid"), bbo.get("ask"),
                tri.gate_score if tri else None,
                json.dumps(facts),
                sum(len(f.get("sources") or []) for f in facts),
                sig.side.value if sig else None,
                sig.probability if sig else None,
                sig.confidence if sig else None,
                order.side.value if order else None,
                order.quantity if order else None,
                order.limit_price if order else None,
                order.notional_usd if order else None,
                order.edge if order else None,
                result.halted_at,
                result.total_cost_usd,
            ),
        )
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        log.warning("could not record trace for %s: %s",
                    getattr(result, "market_slug", "?"), exc)


def score(db: str = TRACES_DB) -> None:
    """
    Was Tier 3 worth paying for?

    Brier of Tier 3's probability vs the outcome, beside the Brier of the
    market's own mid at evaluation time, over the same resolved rows. The
    research tiers cost ~$0.10 an evaluation; if they do not beat the
    price they were shown, they are a cost and nothing else.
    """
    with closing(connect(db)) as conn:
        rows = conn.execute(
            """SELECT signal_side, signal_prob, bid, ask, resolved_outcome,
                      cost_usd, order_qty
               FROM evaluations
               WHERE resolved_outcome IS NOT NULL AND signal_prob IS NOT NULL
                 AND bid IS NOT NULL AND ask IS NOT NULL"""
        ).fetchall()
    print("=" * 62)
    print(f"  {len(rows)} resolved evaluations with a Tier 3 signal")
    print("=" * 62)
    if not rows:
        print("\n  nothing resolved yet.")
        return
    bm, bk = [], []
    for r in rows:
        y = float(r["resolved_outcome"])
        # signal_prob is P(the chosen side). Put it on the YES scale.
        p_yes = r["signal_prob"] if r["signal_side"] == "YES" else 1 - r["signal_prob"]
        bm.append((p_yes - y) ** 2)
        bk.append(((r["bid"] + r["ask"]) / 2 - y) ** 2)
    m, k = statistics.mean(bm), statistics.mean(bk)
    spent = sum(r["cost_usd"] or 0 for r in rows)
    print(f"\n  brier  tier3 {m:.4f}   market {k:.4f}   "
          f"({'tier3 better' if m < k else 'market better'})")
    print(f"  research spend on these rows: ${spent:.2f}  "
          f"({sum(1 for r in rows if r['order_qty'])} were sized)")
