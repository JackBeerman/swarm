#!/usr/bin/env python3
"""
verify_setup.py -- confirm the environment works before spending anything.

Checks imports, credentials, and makes exactly ONE live Jev call (cost:
about $0.00007) to prove the key and the response shape are both real.

    python verify_setup.py
"""
from __future__ import annotations

import asyncio
import os
import sys

OK, BAD, WARN = "  [ok]  ", "  [FAIL]", "  [warn]"


def check_imports() -> bool:
    print("\ndependencies")
    ok = True
    for mod, why in [
        ("pydantic", "tier contracts"),
        ("httpx", "jev client"),
        ("litellm", "tier 2/3"),
        ("polymarket_us", "exchange SDK"),
        ("dotenv", "loads .env"),
    ]:
        try:
            __import__(mod)
            print(f"{OK} {mod:<16} {why}")
        except ImportError:
            print(f"{BAD} {mod:<16} MISSING -- pip install -r requirements.txt")
            ok = False
    return ok


def check_local() -> bool:
    print("\nproject modules")
    ok = True
    for mod in ("schemas", "questions", "adapters", "swarm",
                "risk_engine", "config", "shadow", "daemon"):
        try:
            __import__(mod)
            print(f"{OK} {mod}")
        except Exception as exc:
            print(f"{BAD} {mod}: {exc}")
            ok = False
    return ok


def check_config() -> bool:
    from config import Config
    print("\nconfiguration")
    cfg = Config.from_env()
    print(cfg.describe())
    problems = cfg.validate()
    for p in problems:
        print(f"{BAD} {p}")
    if not problems:
        print(f"{OK} valid for mode={cfg.mode}")
    if cfg.jev_model.endswith("latest"):
        print(f"{WARN} {cfg.jev_model} floats -- pin a dated id before live")
    return not problems


async def check_jev() -> bool:
    """One real call. Proves the key works AND the response shape matches."""
    from swarm import JevTriage, price_triage
    print("\nlive Jev call")
    if not os.getenv("TYPESAFE_API_KEY"):
        print(f"{BAD} TYPESAFE_API_KEY not set -- skipping")
        return False

    jev = JevTriage()
    try:
        body = await jev._post({
            "model": jev._model,
            "state": {"question": "Will the Fed cut rates in October?",
                      "description": "Resolves YES if the FOMC lowers the "
                                     "target range, per the official statement."},
            "questions": {
                "objective_resolution": {
                    "type": "noul",
                    "instructions": "Is the resolution condition objective?",
                },
            },
        })
        ans = body.get("answers", {}).get("objective_resolution", {})
        usage = body.get("usage", {})
        if "noul" not in ans:
            print(f"{BAD} unexpected response shape: {body}")
            return False
        cost = price_triage(usage.get("input_tokens", 0),
                            usage.get("output_tokens", 0))
        print(f"{OK} model      {body.get('model')}")
        print(f"{OK} noul       {ans['noul']}")
        print(f"{OK} tokens     in={usage.get('input_tokens')} "
              f"out={usage.get('output_tokens')}")
        print(f"{OK} cost       ${cost:.7f}")
        if body.get("model") != jev._model:
            print(f"{WARN} requested {jev._model}, served {body.get('model')}"
                  f" -- this is the float. Pin it before live.")
        return True
    except Exception as exc:
        print(f"{BAD} {type(exc).__name__}: {exc}")
        return False
    finally:
        await jev.aclose()


async def check_polymarket() -> bool:
    """Public endpoints need no auth, so this works in shadow mode."""
    from polymarket_us import AsyncPolymarketUS
    from adapters import derive_notionals, iter_event_markets, normalize_bbo
    print("\nPolymarket US (public endpoints)")
    try:
        async with AsyncPolymarketUS() as pm:
            page = await pm.events.list({"limit": 5, "closed": False})
            pairs = iter_event_markets(page)
            print(f"{OK} {len(page.get('events', []))} events, "
                  f"{len(pairs)} open markets")
            if not pairs:
                # Previously this printed [ok] and returned True on zero
                # markets, so a query that found nothing looked like a pass.
                print(f"{BAD} no open markets -- the events filter is wrong")
                return False
            slug = pairs[0][0]["slug"]
            bbo = normalize_bbo(await pm.markets.bbo(slug))
            print(f"{OK} {slug[:40]}: bid={bbo['bid']} ask={bbo['ask']}")
            if bbo["bid"] is None:
                print(f"{BAD} quote parsed to None -- normalize_bbo is not "
                      f"reading the response shape")
                return False
            n = derive_notionals(bbo)
            print(f"{OK} derived     volume=${n['volume_usd']:,.0f} "
                  f"liquidity=${n['liquidity_usd']:,.0f}")
        return True
    except Exception as exc:
        print(f"{BAD} {type(exc).__name__}: {exc}")
        return False


async def main() -> int:
    print("=" * 58)
    print("  swarm setup verification")
    print("=" * 58)
    results = [check_imports(), check_local(), check_config()]
    results.append(await check_jev())
    results.append(await check_polymarket())
    print("\n" + "=" * 58)
    if all(results):
        print("  all checks passed -- next: make shadow")
        return 0
    print("  some checks failed; see above")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
