"""
daemon.py -- the entry point. Defaults to shadow mode; live trading must be
turned on deliberately.

    python daemon.py                      # shadow: triage only, no orders
    SWARM_MODE=paper python daemon.py     # full pipeline, logs orders
    SWARM_MODE=live python daemon.py      # places real orders

Shutdown is graceful on SIGINT/SIGTERM and non-zero on a kill switch, so a
supervisor can tell "the operator stopped it" from "it hit a floor". The
original spec's `sys.exit(0)` conflated those: exit 0 tells systemd the
process succeeded, and Restart=always brings it straight back with a fresh
in-memory state.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from typing import Any

from polymarket_us import AsyncPolymarketUS

from adapters import PriceTracker, iter_event_markets, normalize_bbo, normalize_market
from config import Config, ConfigError, setup_logging
from questions import GateThresholds, StructuralLimits
from risk_engine import CostLedger, KillSwitch, RiskEngine, RiskLimits
from schemas import PipelineResult
from swarm import JevTriage, PortfolioLock, RiskConfig, Swarm

log = logging.getLogger("daemon")

EXIT_OK = 0
EXIT_KILLSWITCH = 2
EXIT_CONFIG = 3


class Daemon:
    def __init__(self, cfg: Config, poll_seconds: float = 300.0,
                 once: bool = False):
        self.cfg = cfg
        self.poll_seconds = poll_seconds
        self.once = once
        self.tracker = PriceTracker()
        self._stop = asyncio.Event()
        self.stats = {"triaged": 0, "escalated": 0, "orders": 0, "loops": 0}

    def request_stop(self, *_: Any) -> None:
        log.info("shutdown requested; finishing current cycle")
        self._stop.set()

    # ------------------------------------------------------------------

    async def run(self) -> int:
        pm_kwargs = {}
        if self.cfg.needs_trading_credentials:
            pm_kwargs = {
                "key_id": self.cfg.polymarket_key_id,
                "secret_key": self.cfg.polymarket_secret_key,
            }

        async with AsyncPolymarketUS(**pm_kwargs) as pm:
            jev = JevTriage(
                api_key=self.cfg.typesafe_api_key, model=self.cfg.jev_model
            )
            engine = RiskEngine(
                pm,
                limits=RiskLimits(),
                ledger=CostLedger(path=self.cfg.ledger_db),
                dry_run=not self.cfg.is_live,
            )

            try:
                if self.cfg.mode == "shadow":
                    # No account, no balances, no risk surface. Shadow mode
                    # must run without trading credentials at all -- that is
                    # the whole reason it exists.
                    log.info("shadow mode: no account access, no orders")
                    budget: Any = _ShadowBudget()
                else:
                    await engine.start()
                    budget = engine

                swarm = Swarm(
                    jev=jev,
                    search=_unconfigured_search,
                    budget=budget,
                    gate=GateThresholds(),
                    risk=RiskConfig(),
                    limits=StructuralLimits(),
                )

                while not self._stop.is_set():
                    self.stats["loops"] += 1
                    try:
                        await self._cycle(pm, swarm, engine)
                    except KillSwitch:
                        raise
                    except Exception as exc:
                        log.exception("cycle failed, continuing: %s", exc)

                    try:
                        if self.once:
                            self._stop.set()
                            break
                        await asyncio.wait_for(
                            self._stop.wait(), timeout=self.poll_seconds
                        )
                    except asyncio.TimeoutError:
                        pass

                log.info("stopped cleanly: %s", self.stats)
                if self.cfg.mode != "shadow":
                    print(engine.report())
                return EXIT_OK

            except KillSwitch as ks:
                log.critical("halted: %s", ks.reason)
                if self.cfg.mode != "shadow":
                    print(engine.report())
                return EXIT_KILLSWITCH
            finally:
                await jev.aclose()

    # ------------------------------------------------------------------

    async def _cycle(self, pm: Any, swarm: Swarm, engine: RiskEngine) -> None:
        if self.cfg.mode != "shadow":
            await engine.check_floors()

        page = await pm.events.list(
            {"limit": 100, "active": True, "closed": False, "volumeMin": 50_000}
        )
        pairs = iter_event_markets(page, min_volume=50_000)
        log.info("cycle %d: %d candidate markets", self.stats["loops"], len(pairs))

        for market, event in pairs:
            if self._stop.is_set():
                return
            slug = market.get("slug")
            if not slug:
                continue
            try:
                bbo = normalize_bbo(await pm.markets.bbo(slug))
                if bbo["bid"] is not None and bbo["ask"] is not None:
                    self.tracker.observe(slug, (bbo["bid"] + bbo["ask"]) / 2)
                norm = normalize_market(market, event)
            except Exception as exc:
                log.debug("skipping %s: %s", slug, exc)
                continue

            if self.cfg.mode == "shadow":
                verdict = await swarm.jev.evaluate(
                    norm, bbo, swarm.gate, self.tracker, swarm.limits
                )
                self.stats["triaged"] += 1
                if verdict.escalate:
                    self.stats["escalated"] += 1
                    log.info(
                        "ESCALATE %-40s score=%.3f", slug[:40], verdict.gate_score
                    )
                continue

            result = await swarm.evaluate(norm, bbo, self.tracker)
            self.stats["triaged"] += 1
            if result.triage and result.triage.escalate:
                self.stats["escalated"] += 1
            if result.order:
                await self._place(pm, result, swarm.portfolio)

    async def _place(
        self, pm: Any, result: PipelineResult, portfolio: PortfolioLock
    ) -> None:
        o = result.order
        assert o is not None
        intent = (
            "ORDER_INTENT_BUY_LONG" if o.side.value == "YES"
            else "ORDER_INTENT_BUY_SHORT"
        )
        params = {
            "marketSlug": o.market_slug,
            "intent": intent,
            "type": "ORDER_TYPE_LIMIT",
            "price": {"value": f"{o.limit_price:.3f}", "currency": "USD"},
            "quantity": o.quantity,          # int -- whole shares only
            "tif": "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL",
            "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
        }

        if not self.cfg.is_live:
            log.info(
                "PAPER order: %s %s %d @ %.3f ($%.2f, edge %.3f)",
                o.market_slug, o.side.value, o.quantity,
                o.limit_price, o.notional_usd, o.edge,
            )
            self.stats["orders"] += 1
            portfolio.release(o.market_slug)
            return

        try:
            try:
                # preview() validates against exchange rules before
                # committing. Cheap insurance against a malformed order.
                await pm.orders.preview({"request": params})
            except Exception as exc:
                log.warning("preview rejected %s: %s", o.market_slug, exc)
                return

            res = await pm.orders.create(params)
            self.stats["orders"] += 1
            log.info("LIVE order %s placed: %s", res.get("id"), params)
        except Exception as exc:
            log.error("order failed for %s: %s", o.market_slug, exc)
        finally:
            # Release the sizing reservation on every path. A rejected
            # order holds no capital, and a filled one is already counted
            # in the exchange's own balances -- keeping the reservation
            # would double-count it and shrink every later position.
            portfolio.release(o.market_slug)


class _ShadowBudget:
    """Never spends, never sizes. Shadow mode touches no account."""

    async def can_spend(self, usd: float) -> bool:
        return False

    async def record_spend(self, cost: Any) -> None:
        return None

    async def available_bankroll(self) -> float:
        return 0.0


async def _unconfigured_search(query: str) -> list[dict[str, str]]:
    log.warning("web search is not wired up; gatherers will return nothing")
    return []


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--poll", type=float, default=300.0, help="seconds between cycles")
    ap.add_argument("--once", action="store_true", help="run one cycle and exit")
    args = ap.parse_args()

    try:
        cfg = Config.from_env().require()
    except ConfigError as exc:
        setup_logging("INFO")
        log.error("%s", exc)
        return EXIT_CONFIG

    setup_logging(cfg.log_level)
    log.info("starting\n%s", cfg.describe())

    daemon = Daemon(cfg, poll_seconds=args.poll, once=args.once)

    async def runner() -> int:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, daemon.request_stop)
            except NotImplementedError:
                pass
        return await daemon.run()

    return asyncio.run(runner())


if __name__ == "__main__":
    sys.exit(main())
