"""
paper.py -- each lane's paper decisions, replayed into a $100 bankroll.

SHADOW ONLY. This file never calls the order path or any exchange
endpoint. It imports pure functions (sizing, the event cap, the fee
model) and reads the lanes' databases read-only.

Every lane records decisions; none of them records what those decisions
would have done to a bankroll. This replays them in time order through
the SAME code the live path sizes with:

    size_from_signal()  fractional Kelly, net of the fee, where a
                        probability exists
    cap_to_event()      <=10% of bankroll per event, on open exposure
    fees.fee_usd()      charged on entry
    settlement          from each database's resolved_outcome

Sizing inputs per lane (no model output is ever a size; these are the
probability and haircuts the live function takes):

    slow     traces.db   Tier 3 probability, its confidence, the gate score
    fast     fastlane.db the brief's fair price when a scenario matched
                         (confidence = Jev's scenario confidence, gate 1.0);
                         otherwise a FIXED 1-SHARE stake, labelled, because
                         there is no probability to size from
    weather  weather.db  the forecast-implied probability (confidence 1.0,
                         gate 1.0) against the LISTED mid with an ASSUMED
                         1-cent half-spread -- weather.py records no book

Bankroll for sizing mirrors RiskEngine.available_bankroll(): cash less the
hard floor. Open positions are carried at cost (no mark-to-market), so the
curve moves on entry fees and on settlement. Settlement time is not
recorded by these databases; it is estimated as the slug's date + 36 h
(labelled), which is after every US game and weather day has settled.

    python paper.py            # replay every lane, persist to paper.db, print
"""

from __future__ import annotations

import argparse
import heapq
import json
import os
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from closer import SOURCE_DBS, _ro, _ts, event_key
from fees import fee_usd
from risk_engine import RiskLimits
from schemas import Side, SizedOrder, TradeSignal
from swarm import RiskConfig, cap_to_event, size_from_signal

DB_PATH = os.getenv("PAPER_DB", "paper.db")
START_USD = 100.0

FAST_GATE = 1.0                 # the fast lane has no Tier 1 gate score
WEATHER_CONFIDENCE = 1.0        # code model; no self-reported confidence
WEATHER_GATE = 1.0
WEATHER_HALF_SPREAD = 0.01      # ASSUMED: weather.db stores a listed mid only
SETTLE_AFTER_DATE_H = 36        # slug date 00:00 UTC + 36 h: estimated settlement

_SCHEMA = """
CREATE TABLE IF NOT EXISTS lanes (
    lane           TEXT PRIMARY KEY,
    built_at       TEXT NOT NULL,
    start_usd      REAL,
    final_equity   REAL,
    cash           REAL,
    bets           INTEGER,
    events         INTEGER,
    settled        INTEGER,
    open_positions INTEGER,
    fees_usd       REAL,
    max_dd_usd     REAL,
    max_dd_pct     REAL,
    skipped_json   TEXT,
    sizing_json    TEXT
);
CREATE TABLE IF NOT EXISTS decisions (
    lane           TEXT NOT NULL,
    at             TEXT NOT NULL,
    market_slug    TEXT NOT NULL,
    event_slug     TEXT,
    side           TEXT,
    sizing         TEXT,           -- kelly | fixed_1_share
    status         TEXT NOT NULL,  -- opened | skipped:<reason>
    qty            INTEGER,
    price          REAL,           -- price paid per share (NO: 1 - bid)
    notional       REAL,
    fee            REAL,
    outcome        TEXT,
    settle_at      TEXT,
    settle_estimated INTEGER,
    pnl            REAL,           -- after the entry fee; NULL while open
    note           TEXT
);
CREATE TABLE IF NOT EXISTS equity (
    lane           TEXT NOT NULL,
    seq            INTEGER NOT NULL,
    at             TEXT NOT NULL,
    equity         REAL NOT NULL,  -- cash + open positions at cost
    cash           REAL NOT NULL,
    kind           TEXT NOT NULL,  -- start | entry | settle
    market_slug    TEXT
);
"""


@dataclass(frozen=True)
class Decision:
    lane: str
    market_slug: str
    event_slug: str
    at: datetime
    side: str                       # YES | NO
    bid: float
    ask: float
    sizing: str                     # kelly | fixed_1_share
    probability: float | None       # P(the chosen side); None for fixed stakes
    confidence: float = 0.0
    gate_score: float = 1.0
    outcome: str | None = None      # "1" | "0" | None (open)
    settle_at: datetime | None = None
    settle_estimated: bool = True
    note: str = ""


@dataclass
class LaneResult:
    lane: str
    start_usd: float
    cash: float = 0.0
    curve: list[dict[str, Any]] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    fees: float = 0.0

    def summary(self) -> dict[str, Any]:
        opened = [r for r in self.rows if r["status"] == "opened"]
        skipped: dict[str, int] = {}
        sizing: dict[str, int] = {}
        for r in self.rows:
            if r["status"] != "opened":
                skipped[r["status"]] = skipped.get(r["status"], 0) + 1
        for r in opened:
            sizing[r["sizing"]] = sizing.get(r["sizing"], 0) + 1
        dd_usd, dd_pct = max_drawdown([p["equity"] for p in self.curve])
        return {
            "lane": self.lane, "start_usd": self.start_usd,
            "final_equity": self.curve[-1]["equity"] if self.curve else self.start_usd,
            "cash": round(self.cash, 2), "bets": len(opened),
            "events": len({r["event_slug"] for r in opened}),
            "settled": sum(r["pnl"] is not None for r in opened),
            "open_positions": sum(r["pnl"] is None for r in opened),
            "fees_usd": round(self.fees, 2), "max_dd_usd": dd_usd, "max_dd_pct": dd_pct,
            "skipped": skipped, "sizing": sizing,
        }


def max_drawdown(equity: list[float]) -> tuple[float, float]:
    """Largest peak-to-trough fall: (USD, fraction of the peak)."""
    peak, dd_usd, dd_pct = float("-inf"), 0.0, 0.0
    for e in equity:
        peak = max(peak, e)
        if peak > 0 and peak - e > dd_usd:
            dd_usd, dd_pct = peak - e, (peak - e) / peak
    return round(dd_usd, 2), round(dd_pct, 4)


def _fixed_order(d: Decision) -> SizedOrder:
    """One whole share at the touch. The signal carries no probability of its own:
    the placeholder is the price paid, i.e. zero claimed edge."""
    price = d.ask if d.side == "YES" else 1.0 - d.bid
    sig = TradeSignal(market_slug=d.market_slug, side=Side(d.side), probability=price,
                      confidence=0.0, reasoning="fixed 1-share stake; no probability")
    return SizedOrder(market_slug=d.market_slug, side=Side(d.side), limit_price=round(price, 3),
                      quantity=1, notional_usd=max(0.01, round(price, 2)), raw_kelly=0.0,
                      applied_fraction=0.0, edge=0.0, bankroll_at_size=0.0, signal=sig)


def replay(lane: str, decisions: list[Decision], start_usd: float = START_USD,
           risk: RiskConfig | None = None, floor_usd: float | None = None) -> LaneResult:
    """Chronological replay. Pure: no I/O, no exchange, no orders."""
    risk = risk or RiskConfig()
    floor_usd = RiskLimits().hard_floor_usd if floor_usd is None else floor_usd
    res = LaneResult(lane=lane, start_usd=start_usd, cash=start_usd)
    open_cost: dict[str, float] = {}          # event -> USD at cost, unsettled
    pending: list[tuple[datetime, int, dict[str, Any]]] = []
    decisions = sorted(decisions, key=lambda d: d.at)
    end = (decisions[-1].at if decisions else datetime.now(timezone.utc)) + timedelta(days=3650)

    def point(at: datetime, kind: str, slug: str | None) -> None:
        equity = res.cash + sum(open_cost.values())
        res.curve.append({"seq": len(res.curve), "at": at.isoformat(), "equity": round(equity, 4),
                          "cash": round(res.cash, 4), "kind": kind, "market_slug": slug})

    def settle_until(t: datetime) -> None:
        while pending and pending[0][0] <= t:
            at, _, row = heapq.heappop(pending)
            y = float(row["outcome"])
            payout = row["qty"] * (y if row["side"] == "YES" else 1.0 - y)
            res.cash += payout
            open_cost[row["event_slug"]] -= row["notional"]
            if open_cost[row["event_slug"]] <= 1e-9:
                del open_cost[row["event_slug"]]
            row["pnl"] = round(payout - row["notional"] - row["fee"], 4)
            # A settlement at the far-future placeholder is plotted at the last entry.
            point(min(at, decisions[-1].at) if at >= end else at, "settle", row["market_slug"])

    if decisions:
        point(decisions[0].at, "start", None)
    for i, d in enumerate(decisions):
        settle_until(d.at)
        row = {"lane": lane, "at": d.at.isoformat(), "market_slug": d.market_slug,
               "event_slug": d.event_slug, "side": d.side, "sizing": d.sizing, "qty": None,
               "price": None, "notional": None, "fee": None, "outcome": d.outcome,
               "settle_at": None, "settle_estimated": int(d.settle_estimated), "pnl": None,
               "note": d.note}
        res.rows.append(row)
        bankroll = max(0.0, res.cash - floor_usd)
        if d.sizing == "kelly":
            sig = TradeSignal(market_slug=d.market_slug, side=Side(d.side),
                              probability=d.probability, confidence=d.confidence,
                              reasoning="paper replay")
            order = size_from_signal(sig, {"bid": d.bid, "ask": d.ask}, bankroll,
                                     d.gate_score, risk)
            if order is None:
                row["status"] = "skipped:no_edge_or_spread"
                continue
        else:
            if not (0 < d.bid < d.ask < 1) or d.ask - d.bid > risk.max_spread:
                row["status"] = "skipped:spread"
                continue
            order = _fixed_order(d)
        order = cap_to_event(order, open_cost.get(d.event_slug, 0.0), bankroll, risk)
        if order is None:
            row["status"] = "skipped:event_cap"
            continue
        fee = fee_usd(order.limit_price, order.quantity)
        if order.notional_usd + fee > res.cash - floor_usd:
            row["status"] = "skipped:cash"
            continue
        res.cash -= order.notional_usd + fee
        res.fees += fee
        open_cost[d.event_slug] = open_cost.get(d.event_slug, 0.0) + order.notional_usd
        row.update(status="opened", qty=order.quantity, price=order.limit_price,
                   notional=order.notional_usd, fee=fee)
        point(d.at, "entry", d.market_slug)
        if d.outcome in ("0", "1"):
            when = max(d.settle_at, d.at) if d.settle_at else end
            row["settle_at"] = when.isoformat() if when < end else None
            heapq.heappush(pending, (when, i, row))
    settle_until(end)
    return res


# --------------------------------------------------------------------------
# each lane's decisions, read-only
# --------------------------------------------------------------------------

_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def estimated_settle(slug: str) -> datetime | None:
    """The slug's date + 36 h (UTC). Labelled an estimate wherever it is used."""
    m = _DATE.search(slug or "")
    if not m:
        return None
    try:
        d = date.fromisoformat(m.group(1))
    except ValueError:
        return None
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc) + timedelta(hours=SETTLE_AFTER_DATE_H)


def _first_per_market(ds: list[Decision]) -> list[Decision]:
    seen, out = set(), []
    for d in sorted(ds, key=lambda d: d.at):
        if d.market_slug not in seen:
            seen.add(d.market_slug)
            out.append(d)
    return out


def load_decisions(dbs: dict[str, str] | None = None) -> dict[str, list[Decision]]:
    dbs = dbs or SOURCE_DBS
    out: dict[str, list[Decision]] = {"slow": [], "fast": [], "weather": []}
    t = _ro(dbs["traces"])
    if t:
        with closing(t):
            for r in t.execute("SELECT * FROM evaluations WHERE signal_side IS NOT NULL AND"
                               " signal_prob IS NOT NULL AND bid IS NOT NULL AND ask IS NOT NULL"
                               " ORDER BY at"):
                out["slow"].append(Decision(
                    "slow", r["market_slug"], event_key(r["market_slug"], r["event_slug"]),
                    _ts(r["at"]), r["signal_side"], r["bid"], r["ask"], "kelly",
                    r["signal_prob"], r["signal_conf"] or 0.0, r["gate_score"] or 0.0,
                    r["resolved_outcome"], estimated_settle(r["market_slug"]), True,
                    f"mode={r['mode']}"))
    f = _ro(dbs["fastlane"])
    if f:
        with closing(f):
            for r in f.execute("SELECT * FROM signals WHERE acted=1 AND bid0 IS NOT NULL"
                               " AND ask0 IS NOT NULL ORDER BY at"):
                side = "YES" if r["effect"] == "raises" else "NO"
                fair = r["fair_yes"]
                kelly = fair is not None
                out["fast"].append(Decision(
                    "fast", r["market_slug"], event_key(r["market_slug"], r["event_slug"]),
                    _ts(r["at"]), side, r["bid0"], r["ask0"],
                    "kelly" if kelly else "fixed_1_share",
                    (fair if side == "YES" else 1.0 - fair) if kelly else None,
                    (r["scenario_conf"] or 0.0) if kelly else 0.0, FAST_GATE,
                    r["resolved_outcome"], estimated_settle(r["market_slug"]), True,
                    "brief fair price" if kelly else "no fair price: fixed 1-share stake"))
    w = _ro(dbs["weather"])
    if w:
        with closing(w):
            # A listed mid of 0.0 is a market with no book yet (every 9/24 band
            # on 9/22); an assumed spread around it would buy phantom shares.
            for r in w.execute("SELECT * FROM forecasts WHERE market_mid > ? AND market_mid < ?"
                               " AND lead_days >= 1 ORDER BY at",
                               (WEATHER_HALF_SPREAD, 1 - WEATHER_HALF_SPREAD)):
                mid, p = r["market_mid"], r["p_model"]
                side = "YES" if p >= mid else "NO"
                out["weather"].append(Decision(
                    "weather", r["market_slug"], f"temp-{r['city']}-{r['target_date']}",
                    _ts(r["at"]), side, round(max(0.001, mid - WEATHER_HALF_SPREAD), 4),
                    round(min(0.999, mid + WEATHER_HALF_SPREAD), 4), "kelly",
                    p if side == "YES" else 1.0 - p, WEATHER_CONFIDENCE, WEATHER_GATE,
                    r["resolved_outcome"], estimated_settle(r["market_slug"]), True,
                    "listed mid +/- assumed 0.01; lead 0 and bookless mids excluded"))
    return {lane: _first_per_market([d for d in ds if d.at is not None])
            for lane, ds in out.items()}


def build(dbs: dict[str, str] | None = None) -> dict[str, LaneResult]:
    return {lane: replay(lane, ds) for lane, ds in load_decisions(dbs).items()}


def persist(results: dict[str, LaneResult], db: str = DB_PATH) -> None:
    """Rebuild paper.db: the replay is deterministic, so it is a derived cache."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with closing(sqlite3.connect(db)) as conn:
        conn.executescript(_SCHEMA)
        for lane, res in results.items():
            s = res.summary()
            for tbl in ("lanes", "decisions", "equity"):
                conn.execute(f"DELETE FROM {tbl} WHERE lane=?", (lane,))  # noqa: S608 -- constants
            conn.execute(
                "INSERT INTO lanes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (lane, now, s["start_usd"], s["final_equity"], s["cash"], s["bets"], s["events"],
                 s["settled"], s["open_positions"], s["fees_usd"], s["max_dd_usd"],
                 s["max_dd_pct"], json.dumps(s["skipped"]), json.dumps(s["sizing"])))
            conn.executemany(
                "INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(r["lane"], r["at"], r["market_slug"], r["event_slug"], r["side"], r["sizing"],
                  r["status"], r["qty"], r["price"], r["notional"], r["fee"], r["outcome"],
                  r["settle_at"], r["settle_estimated"], r["pnl"], r["note"]) for r in res.rows])
            conn.executemany(
                "INSERT INTO equity VALUES (?,?,?,?,?,?,?)",
                [(lane, p["seq"], p["at"], p["equity"], p["cash"], p["kind"], p["market_slug"])
                 for p in res.curve])
        conn.commit()


def equity_doc(res: LaneResult) -> dict[str, Any]:
    """The dashboard's equity/<lane> document."""
    return {"updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **res.summary(),
            "points": [{"at": p["at"], "equity": p["equity"], "kind": p["kind"]}
                       for p in res.curve],
            "positions": [{k: r[k] for k in ("at", "market_slug", "event_slug", "side", "sizing",
                                             "qty", "price", "fee", "outcome", "pnl")}
                          for r in res.rows if r["status"] == "opened"],
            "note": "Open positions at cost. Settlement times estimated (slug date + 36h). "
                    "fixed_1_share = no probability to size from."}


def main() -> None:
    ap = argparse.ArgumentParser(description="Paper portfolios per lane. No orders, ever.")
    ap.add_argument("--no-persist", action="store_true")
    args = ap.parse_args()
    results = build()
    if not args.no_persist:
        persist(results)
    print(f"  {'lane':<9}{'bets':>5}{'events':>7}{'settled':>8}{'open':>6}{'fees':>7}"
          f"{'equity':>9}{'max dd':>9}   skipped / sizing")
    for lane, res in results.items():
        s = res.summary()
        print(f"  {lane:<9}{s['bets']:>5}{s['events']:>7}{s['settled']:>8}{s['open_positions']:>6}"
              f"{s['fees_usd']:>7.2f}{s['final_equity']:>9.2f}{s['max_dd_pct']:>9.1%}   "
              f"{json.dumps(s['skipped'])} {json.dumps(s['sizing'])}")


if __name__ == "__main__":
    main()
