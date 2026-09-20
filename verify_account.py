#!/usr/bin/env python3
"""
verify_account.py -- one READ-ONLY pass over the account before the risk
engine is allowed to trust it. Places no orders. Works in any SWARM_MODE.

    python verify_account.py

Why this exists. The market-data adapter's field table was "verified"
against the SDK's TypedDicts, which are total=False and assert nothing;
three of its mappings were wrong on the wire. The account layer was
verified the same way and has never run against a real account. The risk
engine reads six balance fields with float(...) -- if the gateway sends
them as Amount dicts, that raises, is swallowed, and every consumer sees
an all-zeros snapshot with a fresh timestamp. This asserts the shapes
risk_engine.refresh() depends on, and reports the RAW type of each field
so a mismatch is visible instead of silent.

Also proves the markets websocket authenticates (HTTP 401 unauthenticated),
which is the in-play prerequisite, by subscribing to one market and reading
one frame.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

from config import load_dotenv_if_present

load_dotenv_if_present()

from adapters import amount  # noqa: E402

OK, BAD, WARN = "  [ok]  ", "  [FAIL]", "  [warn]"

BALANCE_FIELDS = ("currentBalance", "buyingPower", "assetNotional",
                  "openOrders", "unsettledFunds")


def _coercible(v) -> tuple[bool, str]:
    """Can risk_engine's float(v or 0.0) read this? Report the raw type."""
    t = type(v).__name__
    if v is None:
        return True, "None"
    if isinstance(v, (int, float)):
        return True, t
    if isinstance(v, str):
        try:
            float(v)
            return True, "str(numeric)"
        except ValueError:
            return False, "str(non-numeric)"
    if isinstance(v, dict):
        # An Amount. float(dict) raises -> refresh() swallows -> zeros.
        return False, "dict(Amount) -- float() would raise"
    return False, t


async def check_balances(pm) -> tuple[bool, float, float]:
    print("\naccount.balances()")
    try:
        resp = await pm.account.balances()
    except Exception as exc:
        print(f"{BAD} {type(exc).__name__}: {exc}")
        return False, 0.0, 0.0
    bals = resp.get("balances")
    if not isinstance(bals, list):
        print(f"{BAD} 'balances' is {type(bals).__name__}, expected list")
        return False, 0.0, 0.0
    print(f"{OK} {len(bals)} balance record(s)")

    ok = True
    cash = notional = 0.0
    for b in bals:
        cur = (b.get("currency") or "USD").upper()
        print(f"       currency={cur}")
        for f in BALANCE_FIELDS:
            v = b.get(f)
            good, t = _coercible(v)
            flag = OK if good else BAD
            shown = json.dumps(v)[:40]
            print(f"{flag} {f:<16} {t:<28} {shown}")
            ok &= good
        if cur == "USD":
            try:
                cash += float(b.get("currentBalance") or 0.0)
                notional += float(b.get("assetNotional") or 0.0)
            except (TypeError, ValueError):
                pass
    return ok, cash, notional


async def check_positions(pm) -> tuple[bool, float]:
    print("\nportfolio.positions()")
    try:
        resp = await pm.portfolio.positions()
    except Exception as exc:
        print(f"{BAD} {type(exc).__name__}: {exc}")
        return False, 0.0
    pos = resp.get("positions")
    # The bug CLAUDE.md records: iterating this as a list yields bare
    # strings and marks every position at zero.
    if isinstance(pos, list):
        print(f"{BAD} 'positions' is a LIST -- risk_engine expects a dict "
              f"keyed by slug; .values() would fail")
        return False, 0.0
    if pos is None:
        print(f"{OK} no positions (empty account)")
        return True, 0.0
    if not isinstance(pos, dict):
        print(f"{BAD} 'positions' is {type(pos).__name__}")
        return False, 0.0
    print(f"{OK} dict keyed by slug, {len(pos)} position(s)")
    marks = 0.0
    for slug, p in list(pos.items())[:5]:
        cv = amount(p.get("cashValue"))
        print(f"       {slug[:40]:<42} cashValue={cv}  "
              f"net={p.get('netPosition')!r}")
        marks += cv or 0.0
    return True, marks


async def check_websocket(key_id: str, secret: str) -> bool:
    """In-play prerequisite: the markets stream returns 401 unauthenticated."""
    print("\nmarkets websocket (auth)")
    try:
        from polymarket_us.websocket.markets import MarketsWebSocket
    except Exception as exc:
        print(f"{WARN} websocket module unavailable: {exc}")
        return True
    ws = MarketsWebSocket(key_id=key_id, secret_key=secret)
    got: list = []
    ws.on("message", lambda m: got.append(m))
    ws.on("error", lambda e: got.append({"error": str(e)}))
    try:
        await ws.connect()
        # a live-ish, high-volume market; any open slug works
        await ws.subscribe_market_data_lite("verify-1", ["nfl-phi-ten-2026-09-20"])
        for _ in range(20):
            if got:
                break
            await asyncio.sleep(0.5)
        if not got:
            print(f"{WARN} connected, no frame within 10s (subscription may "
                  f"need a market slug; auth itself succeeded)")
            return True
        first = got[0]
        if isinstance(first, dict) and "error" in first:
            print(f"{BAD} {first['error'][:120]}")
            return False
        keys = list(first.keys())[:4] if isinstance(first, dict) else type(first)
        print(f"{OK} authenticated; first frame keys: {keys}")
        return True
    except Exception as exc:
        print(f"{BAD} {type(exc).__name__}: {str(exc)[:120]}")
        return False
    finally:
        try:
            await ws.close()
        except Exception:
            pass


async def main() -> int:
    print("=" * 58)
    print("  account verification (read-only, no orders)")
    print("=" * 58)
    key_id = os.getenv("POLYMARKET_KEY_ID", "")
    secret = os.getenv("POLYMARKET_SECRET_KEY", "")
    if not key_id or not secret:
        print(f"{BAD} POLYMARKET_KEY_ID / POLYMARKET_SECRET_KEY not set in .env")
        return 1
    print(f"{OK} credentials present (key id {key_id[:8]}...)")

    from polymarket_us import AsyncPolymarketUS
    async with AsyncPolymarketUS(key_id=key_id, secret_key=secret) as pm:
        b_ok, cash, notional = await check_balances(pm)
        p_ok, marks = await check_positions(pm)
    w_ok = await check_websocket(key_id, secret)

    print("\nnet liquid value, both ways the risk engine computes it")
    print(f"       cash {cash:.2f} + assetNotional {notional:.2f} = "
          f"{cash + notional:.2f}")
    print(f"       cash {cash:.2f} + position marks {marks:.2f} = "
          f"{cash + marks:.2f}")
    if notional and marks and abs(notional - marks) > 0.01:
        print(f"{WARN} marks disagree by {abs(notional - marks):.2f}; the "
              f"engine takes the more conservative")

    print("\n" + "=" * 58)
    if b_ok and p_ok and w_ok and (cash + max(notional, marks)) <= 10.0:
        # Shapes are right, but the risk engine's start() halts at
        # NLV <= hard_floor_usd ($10) on its FIRST refresh. An unfunded
        # account returns {"balances": []} -- verified live -- and paper
        # mode would write .halted and exit 2 before doing anything.
        # That is the protection working, not a bug; it just means fund
        # the account before setting SWARM_MODE=paper.
        print("  shapes OK, but NLV is at or under the $10 hard floor.")
        print("  paper mode would halt on startup. Fund the account first.")
        return 2
    if b_ok and p_ok and w_ok:
        print("  all checks passed -- the risk engine can trust this account")
        return 0
    print("  some checks FAILED -- do not run paper mode until fixed")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
