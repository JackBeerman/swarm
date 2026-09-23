"""
dashboard_export.py -- turn the local databases into dashboard documents.

Writes one JSON file per document under OUT/<collection>/<doc_id>.json
plus OUT/manifest.json. Claude pushes them to the shared dashboard with
the ArtifactData tool; nothing here talks to claude.ai.

Real bets come from the exchange's own activity ledger (read-only), so
the dashboard shows what the account did, not what the code believes.
Paper bets come from traces.db (slow lane) and fastlane.db (fast lane).

Never exported: keys, the deposit's payment details, anything personal.

    python tools/dashboard_export.py [OUT]
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sqlite3
import statistics
import sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from config import load_dotenv_if_present  # noqa: E402

load_dotenv_if_present()

OUT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / ".dashboard")

#: What each real order was, in words. The ledger knows prices, not reasons.
NOTES = {
    "phi-ten-2026-09-20-total-24pt5": ("Round-trip test", "Eagles vs Titans: over 24.5 total points",
                                       "First live order, to prove the order path end to end."),
    "nyg-lar-2026-09-21-2h-pos-3pt5": ("Fast-lane signal, wrong side", "Giants vs Rams: Giants +3.5, 2nd half",
                                       "Jev said the Giants QB injury hurt the Giants (right: the Rams covered). "
                                       "The exchange title named the Rams, so YES was bought on the Giants. Fixed 9/23."),
    "nyg-lar-2026-09-21-3q-pos-1pt5": ("Fast-lane signal, wrong side", "Giants vs Rams: Giants +1.5, 3rd quarter",
                                       "Same headline, same inverted title. The Rams covered; NO would have won."),
    "nyg-lar-2026-09-21-4q-pos-1pt5": ("Fast-lane signal, wrong side", "Giants vs Rams: Giants +1.5, 4th quarter",
                                       "Same headline, same inverted title."),
}


def _amt(x) -> float:
    if isinstance(x, dict):
        x = x.get("value")
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _key(slug: str) -> str:
    for k in NOTES:
        if slug.endswith(k):
            return k
    return slug


def _db(path: str) -> sqlite3.Connection | None:
    if not (ROOT / path).exists():
        return None
    c = sqlite3.connect(ROOT / path)
    c.row_factory = sqlite3.Row
    return c


async def real_bets() -> tuple[list[dict], dict]:
    from polymarket_us import AsyncPolymarketUS
    orders: dict[str, dict] = {}
    deposits = 0.0
    cash = None
    async with AsyncPolymarketUS(key_id=os.environ["POLYMARKET_KEY_ID"],
                                 secret_key=os.environ["POLYMARKET_SECRET_KEY"]) as pm:
        acts, cur = [], None
        for _ in range(20):
            r = await pm.portfolio.activities({"cursor": cur} if cur else {})
            acts += r.get("activities", [])
            if r.get("eof") or not r.get("nextCursor"):
                break
            cur = r["nextCursor"]
            await asyncio.sleep(1.2)
        bal = (await pm.account.balances()).get("balances") or [{}]
        cash = bal[0].get("currentBalance")
    for a in acts:
        t = a.get("type")
        if t == "ACTIVITY_TYPE_ACCOUNT_DEPOSIT":
            deposits += _amt(a["accountBalanceChange"].get("amount"))
        elif t == "ACTIVITY_TYPE_TRADE":
            x = a["trade"]
            o = orders.setdefault(x["marketSlug"], {"shares": 0.0, "notional": 0.0, "charged": 0.0,
                                                    "placed_at": x.get("createTime")})
            q = float(x.get("qtyDecimal") or x.get("qty") or 0)
            ex = x.get("aggressorExecution") or {}
            shares = float(ex.get("lastShares") or q)
            o["shares"] += shares
            o["notional"] += shares * _amt(x.get("price"))
            o["charged"] += _amt(x.get("cost"))
            o["placed_at"] = min(t for t in (o["placed_at"], x.get("createTime")) if t)
        elif t == "ACTIVITY_TYPE_POSITION_RESOLUTION":
            x = a["positionResolution"]
            o = orders.setdefault(x["marketSlug"], {"shares": 0.0, "notional": 0.0, "charged": 0.0,
                                                    "placed_at": None})
            o["realized"] = _amt(x["afterPosition"].get("realized"))
            o["resolved_at"] = x.get("updateTime")
    rows = []
    for slug, o in orders.items():
        k = _key(slug)
        source, label, note = NOTES.get(k, ("Unlabelled", slug, ""))
        fees = round(o["charged"] - o["notional"], 4)
        realized = o.get("realized")
        net = None if realized is None else round(realized - fees, 4)
        rows.append({
            "id": k, "market_slug": slug, "label": label, "source": source, "note": note,
            "placed_at": o["placed_at"], "resolved_at": o.get("resolved_at"),
            "shares": round(o["shares"], 4),
            "avg_price": round(o["notional"] / o["shares"], 4) if o["shares"] else None,
            "cost_usd": round(o["notional"], 4), "fees_usd": fees,
            "status": "open" if realized is None else ("won" if realized > 0 else "lost"),
            "realized_usd": realized, "net_usd": net,
        })
    rows.sort(key=lambda r: r["placed_at"] or "")
    return rows, {"deposits_usd": deposits, "cash_usd": cash}


def paper_bets() -> list[dict]:
    out = []
    t = _db("traces.db")
    if t:
        for r in t.execute("SELECT * FROM evaluations WHERE mode='paper' ORDER BY id"):
            out.append({
                "id": f"slow-{r['id']}", "lane": "slow", "at": r["at"],
                "market_slug": r["market_slug"],
                "label": f"{r['question'] or ''} / {r['outcome'] or ''}".strip(" /"),
                "bid": r["bid"], "ask": r["ask"], "gate": r["gate_score"],
                "call": (f"{r['signal_side']} @ {r['signal_prob']:.2f}"
                         if r["signal_side"] and r["signal_prob"] is not None else None),
                "sized": r["order_qty"], "halted": r["halted_at"],
                "research_usd": r["cost_usd"], "resolved": r["resolved_outcome"],
                "pnl_per_share": None,
            })
    f = _db("fastlane.db")
    if f:
        cols = {c["name"] for c in f.execute("PRAGMA table_info(signals)")}
        res = "s.resolved_outcome" if "resolved_outcome" in cols else "NULL"
        seen = set()
        for r in f.execute(
                f"SELECT s.*, {res} AS res, h.title FROM signals s JOIN headlines h"
                " ON h.id = s.headline_id WHERE s.acted=1 ORDER BY s.id"):
            if r["bid0"] is None or r["ask0"] is None:
                continue
            # One paper position per market: the first signal opens it, and
            # later headlines (eight rewrites of one QB injury on 9/21) are
            # the same bet, not new ones.
            key = r["market_slug"]
            if key in seen:
                continue
            seen.add(key)
            entry = r["ask0"] if r["effect"] == "raises" else 1 - r["bid0"]
            pnl = None
            if r["res"] in ("0", "1"):
                y = float(r["res"])
                pnl = round((y - r["ask0"]) if r["effect"] == "raises" else (r["bid0"] - y), 4)
            m0 = (r["bid0"] + r["ask0"]) / 2
            sign = 1 if r["effect"] == "raises" else -1
            out.append({
                "id": f"fast-{r['id']}", "lane": "fast", "at": r["at"],
                "market_slug": r["market_slug"],
                "label": f"{r['question'] or ''} / {r['outcome'] or ''}".strip(" /"),
                "headline": r["title"], "call": f"{'YES' if sign > 0 else 'NO'} ({r['effect']})",
                "entry": round(entry, 4),
                "drift_5m": None if r["mid_5m"] is None else round((r["mid_5m"] - m0) * sign, 4),
                "drift_30m": None if r["mid_30m"] is None else round((r["mid_30m"] - m0) * sign, 4),
                "jev_ms": r["jev_ms"], "resolved": r["res"], "pnl_per_share": pnl,
            })
    return out


def feed() -> dict:
    f = _db("fastlane.db")
    if not f:
        return {"headlines": []}
    rows = f.execute(
        "SELECT h.seen_at, h.title, h.source, h.lag_seconds, MAX(s.jev_ms) jev_ms,"
        " MAX(s.reports_new_fact) fact, MAX(s.concerns_event) ev, MAX(s.acted) acted"
        " FROM headlines h JOIN signals s ON s.headline_id = h.id"
        " GROUP BY h.id ORDER BY h.id DESC LIMIT 120").fetchall()
    lags = [r["lag_seconds"] for r in f.execute(
        "SELECT lag_seconds FROM headlines WHERE lag_seconds >= 0")]
    ms = [r["jev_ms"] for r in f.execute("SELECT jev_ms FROM signals WHERE jev_ms IS NOT NULL")]
    return {
        "headlines": [{"at": r["seen_at"], "title": r["title"], "source": r["source"],
                       "lag_min": None if r["lag_seconds"] is None else round(r["lag_seconds"] / 60, 1),
                       "jev_ms": r["jev_ms"] and round(r["jev_ms"]),
                       "fact": r["fact"] and round(r["fact"], 2), "event": r["ev"] and round(r["ev"], 2),
                       "acted": bool(r["acted"])} for r in rows],
        "lag_median_min": round(statistics.median(lags) / 60, 1) if lags else None,
        "lag_fastest_min": round(min(lags) / 60, 1) if lags else None,
        "jev_ms_median": round(statistics.median(ms)) if ms else None,
        "headlines_total": f.execute("SELECT COUNT(*) FROM headlines").fetchone()[0],
    }


def calibration() -> dict:
    s = _db("shadow.db")
    if not s:
        return {"bands": []}
    rows = s.execute(
        "SELECT market_slug, bid, ask, resolved_outcome, event_slug FROM verdicts"
        " WHERE resolved_outcome IS NOT NULL AND bid IS NOT NULL AND ask IS NOT NULL"
        " AND ask - bid <= 0.10 GROUP BY market_slug").fetchall()
    bands = [(0.0, 0.3), (0.3, 0.5), (0.5, 0.7), (0.7, 0.85), (0.85, 0.92), (0.92, 0.96), (0.96, 1.01)]
    out = []
    for lo, hi in bands:
        rs = [r for r in rows if lo <= (r["bid"] + r["ask"]) / 2 < hi]
        if not rs:
            continue
        ev = {r["event_slug"] or r["market_slug"].rsplit("-", 2)[0] for r in rs}
        out.append({"lo": lo, "hi": min(hi, 1.0), "markets": len(rs), "events": len(ev),
                    "priced": round(statistics.mean((r["bid"] + r["ask"]) / 2 for r in rs), 3),
                    "won": round(sum(r["resolved_outcome"] == "1" for r in rs) / len(rs), 3)})
    return {"bands": out, "markets": len(rows)}


def weather() -> dict:
    w = _db("weather.db")
    if not w:
        return {"rows": []}
    rows = w.execute(
        "SELECT city, target_date, lead_days, forecast_high, band_lo, band_hi, p_model, market_mid,"
        " resolved_outcome, MAX(at) at FROM forecasts WHERE market_mid IS NOT NULL"
        " GROUP BY market_slug ORDER BY target_date DESC, city, band_lo").fetchall()
    return {"rows": [dict(r) for r in rows][:200]}


def research_spend() -> float:
    t = _db("traces.db")
    return round(sum(r[0] or 0 for r in t.execute("SELECT cost_usd FROM evaluations")), 2) if t else 0.0


def clv_and_equity() -> list[tuple[str, str, dict]]:
    """
    `clv/summary` (closer.py) and `equity/<lane>` (paper.py). Both read the
    lane databases read-only and compute fresh; neither touches the exchange.
    """
    import closer
    import paper
    docs = [("clv", "summary", closer.report_data())]
    docs += [("equity", lane, paper.equity_doc(res)) for lane, res in paper.build().items()]
    return docs


def main() -> None:
    real, acct = asyncio.run(real_bets())
    paper = paper_bets()
    fd = feed()
    cal = calibration()
    wx = weather()
    settled = [r for r in real if r["status"] != "open"]
    fast_settled = [p for p in paper if p["lane"] == "fast" and p["pnl_per_share"] is not None]
    summary = {
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "deposits_usd": acct["deposits_usd"], "cash_usd": acct["cash_usd"],
        "real_orders": len(real), "real_won": sum(r["status"] == "won" for r in real),
        "real_lost": sum(r["status"] == "lost" for r in real),
        "real_net_usd": round(sum(r["net_usd"] or 0 for r in settled), 2),
        "real_fees_usd": round(sum(r["fees_usd"] for r in real), 2),
        "paper_slow": sum(p["lane"] == "slow" for p in paper),
        "paper_fast": sum(p["lane"] == "fast" for p in paper),
        "paper_fast_settled": len(fast_settled),
        "paper_fast_pnl_per_share": round(sum(p["pnl_per_share"] for p in fast_settled), 3),
        "research_usd": research_spend(),
        "jev_ms_median": fd.get("jev_ms_median"), "feed_lag_median_min": fd.get("lag_median_min"),
        "headlines_total": fd.get("headlines_total"),
        "calibration_markets": cal.get("markets"),
        "weather_rows": len(wx["rows"]),
    }
    docs = [("summary", "current", summary), ("feed", "recent", fd),
            ("calibration", "nfl", cal), ("weather", "latest", wx)]
    docs += [("real", r["id"], r) for r in real]
    docs += [("paper", p["id"], p) for p in paper]
    docs += clv_and_equity()
    manifest = []
    for coll, did, body in docs:
        p = OUT / coll / f"{did}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(body, indent=1, default=str), encoding="utf-8")
        manifest.append({"collection": coll, "doc_id": did, "file_path": str(p)})
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    print(f"{len(manifest)} documents -> {OUT}")
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
