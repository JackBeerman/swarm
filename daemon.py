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
import os
import signal
import time
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from polymarket_us import AsyncPolymarketUS

from adapters import (
    PriceTracker,
    DEFAULT_TAG_MIX,
    QuoteFetcher,
    fetch_events_across_tags,
    interleave_by_event,
    iter_event_markets,
    normalize_bbo,
    normalize_market,
    price_band_reject,
)
from config import Config, ConfigError, setup_logging
from questions import GateThresholds, StructuralLimits
from risk_engine import CostLedger, KillSwitch, RiskEngine, RiskLimits
from search import default_search
import traces
from schemas import PipelineResult
from swarm import JevTriage, PortfolioLock, RiskConfig, Swarm

log = logging.getLogger("daemon")

EXIT_OK = 0
EXIT_KILLSWITCH = 2
EXIT_CONFIG = 3


def select_for_research(
    candidates: "list[tuple[PipelineResult, dict[str, Any], dict[str, Any]]]",
    max_research: int,
    per_event: int = 1,
) -> "list[tuple[PipelineResult, dict[str, Any], dict[str, Any]]]":
    """
    Which escalated markets get the paid tiers this cycle.

    Triage is nearly free and research is ~1,500x dearer, so the cycle
    triages everything first and then chooses, instead of researching
    whatever escalated first. Measured on a nine-category sweep
    (2026-09-21): 76% of non-sports markets that reached Jev escalated,
    and one event put up six or seven legs at once -- six net-worth
    thresholds, seven "top artist" legs. Legs of one event resolve
    together and share one set of facts, so researching each is paying
    several times for one bet.

    Highest gate score first; at most `per_event` per event; at most
    `max_research` in total. Ties go to the market that resolves sooner,
    since capital parked for months is a cost on a small treasury.
    """
    def key(c):
        result, market, _ = c
        hours = _hours_until_event(market)
        return (-result.triage.gate_score, hours if hours is not None else 1e9)

    taken: dict[str, int] = {}
    out = []
    for c in sorted((c for c in candidates if c[0].triage and c[0].triage.escalate),
                    key=key):
        # Checked before appending: a budget of zero must research nothing.
        if len(out) >= max_research:
            break
        event = str(c[1].get("event_slug") or c[1].get("slug"))
        if taken.get(event, 0) >= per_event:
            continue
        taken[event] = taken.get(event, 0) + 1
        out.append(c)
    return out


def _hours_until_event(market: "dict[str, Any]") -> "float | None":
    """Hours until the outcome is known; settlement if the event time has passed."""
    for field in ("event_at", "closes_at"):
        raw = market.get(field)
        if not raw:
            continue
        try:
            t = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            continue
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        hours = (t - datetime.now(timezone.utc)).total_seconds() / 3600.0
        if hours > 0:
            return hours
    return None


class Daemon:
    def __init__(self, cfg: Config, poll_seconds: float = 300.0,
                 once: bool = False,
                 cooldown_hours: float = 20.0,
                 markets_per_cycle: int = 150,
                 events_per_cycle: int = 50,
                 quote_concurrency: int = 2,
                 tags: "tuple[str, ...] | None" = None,
                 start_window_hours: float | None = None,
                 min_hours_to_event: float | None = None,
                 min_edge: float | None = None,
                 research_per_cycle: int = 5,
                 research_per_event: int = 1):
        self.cfg = cfg
        # The research budget for one pass, in markets. See
        # select_for_research(): everything is triaged, the best few are
        # researched.
        self.research_per_cycle = research_per_cycle
        self.research_per_event = research_per_event
        # Mirrors shadow.py. Without these a paper cycle reads the default
        # listing page -- season futures and awards, none of the weekend's
        # games -- and the research floor (6h) rejects a same-morning
        # kickoff. The defaults in questions.py are untouched; these are
        # per-run.
        self.tags = tags
        self.start_window_hours = start_window_hours
        self.min_hours_to_event = min_hours_to_event
        # Per-run override of RiskConfig.min_edge. The 0.10 default is
        # arithmetic, not caution -- and on 2026-09-20 it was right by
        # $3.16: every escalation was a ~0.93 contract, none could clear
        # it, and buying them all returned -28%. This exists so a PAPER
        # cycle can show what the sizer would do at a lower bar, not as a
        # recommendation to lower it.
        self.min_edge = min_edge
        self.poll_seconds = poll_seconds
        self.once = once
        # A market is re-triaged at most once per cooldown window. Its
        # description does not change; only its price does, and Tier 1
        # is deliberately not shown the price.
        self.cooldown_seconds = cooldown_hours * 3600.0
        # Hard cap per pass, independent of how many markets exist. Without
        # it the workload is set by the exchange's listing count rather
        # than by anything chosen.
        self.markets_per_cycle = markets_per_cycle
        self.events_per_cycle = events_per_cycle
        self.quote_concurrency = quote_concurrency
        self.tracker = PriceTracker()
        self._stop = asyncio.Event()
        self._last_seen: dict[str, float] = {}
        self._traces: Any = None        # opened on first escalation
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
                    # Shadow never reaches Tier 2, so it never pays for a
                    # search. Wiring it anyway would only invite the
                    # always-on burn the kill switch exists to catch.
                    search=default_search(enabled=self.cfg.mode != "shadow"),
                    budget=budget,
                    gate=GateThresholds(),
                    risk=(RiskConfig(min_edge=self.min_edge)
                          if self.min_edge is not None else RiskConfig()),
                    limits=(StructuralLimits(min_hours_to_close=self.min_hours_to_event)
                            if self.min_hours_to_event is not None
                            else StructuralLimits()),
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

        # `active: True` selects RESOLVED markets here -- active and closed
        # are orthogonal on this gateway. `volumeMin` is ignored. The volume
        # floor lives in StructuralLimits and is applied after the quote.
        if self.tags or self.start_window_hours is not None:
            # Same selection as shadow.py: tag pages plus a start-time
            # window, which is what actually returns the weekend's games.
            extra: dict[str, Any] = {}
            if self.start_window_hours is not None:
                now_utc = datetime.now(timezone.utc)
                extra = {
                    "startTimeMin": (now_utc - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "startTimeMax": (now_utc + timedelta(hours=self.start_window_hours)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
            tag_mix = self.tags or DEFAULT_TAG_MIX
            evs = await fetch_events_across_tags(
                pm, tags=tag_mix,
                per_tag=max(1, self.events_per_cycle // len(tag_mix)),
                extra=extra,
            )
            page = {"events": evs}
        else:
            page = await pm.events.list({"limit": self.events_per_cycle,
                                         "closed": False})
        pairs = iter_event_markets(page)
        total = len(pairs)

        # Cooldown. Without it this loop re-triaged every market on every
        # pass: ~1,488 markets every 300s is 428,544 triage calls a day,
        # $28.71 at $0.000067 each -- 29% of a $100 treasury, daily, to
        # re-read descriptions that had not changed. Tier 1 is cheap per
        # call and ruinous in aggregate; the per-call price is not a
        # licence to re-ask the same question 288 times.
        now = time.time()
        cutoff = now - self.cooldown_seconds
        self._last_seen = {s: t for s, t in self._last_seen.items() if t > cutoff}
        # Interleave before truncating, or the cap takes one event's legs
        # and the cycle sees a single sport.
        # Prescreen extremes before spending a paced quote, as the
        # collector does. A single game lists ~800 markets extreme-first
        # ("cover 17.5" at 0.985); with one event, interleaving changes
        # nothing, and the cap would otherwise be spent entirely on lines
        # the price band rejects.
        lim = swarm.limits
        fresh = interleave_by_event(
            [p for p in pairs
             if p[0].get("slug") not in self._last_seen
             and not price_band_reject(p[0], lim.min_price, lim.max_price)]
        )[:self.markets_per_cycle]

        log.info(
            "cycle %d: %d open, %d off cooldown, %d this pass",
            self.stats["loops"], total, total - len(self._last_seen), len(fresh),
        )

        quotes = QuoteFetcher(pm, concurrency=self.quote_concurrency)
        candidates: list[tuple[PipelineResult, dict[str, Any], dict[str, Any]]] = []

        for market, event in fresh:
            if self._stop.is_set():
                return
            slug = market.get("slug")
            if not slug:
                continue
            bbo = await quotes.bbo(slug)
            if bbo is None:
                continue
            self._last_seen[slug] = time.time()
            try:
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

            result = await swarm.triage(norm, bbo, self.tracker)
            self.stats["triaged"] += 1
            if result.triage and result.triage.escalate:
                self.stats["escalated"] += 1
                candidates.append((result, norm, bbo))

        chosen = select_for_research(
            candidates, self.research_per_cycle, self.research_per_event)
        log.info("research: %d escalated, %d chosen (max %d, %d per event)",
                 len(candidates), len(chosen),
                 self.research_per_cycle, self.research_per_event)
        for result, norm, bbo in chosen:
            if self._stop.is_set():
                return
            slug = result.market_slug
            result = await swarm.research(result, norm, bbo, self.tracker)
            # Keep what the paid tiers believed. Before this, a cycle
            # remembered only its cost -- facts, Tier 3's probability
            # and the sized order were all discarded, so the research
            # could never be scored against the outcome.
            if self._traces is None:
                self._traces = traces.connect()
            traces.record(self._traces, self.cfg.mode, result, norm, bbo)
            sig = result.signal
            log.info("EVAL %-40s gate=%.2f  tier3=%s  order=%s  cost=$%.3f%s",
                     slug[:40], result.triage.gate_score,
                     f"{sig.side.value}@{sig.probability:.2f}" if sig else "none",
                     f"{result.order.quantity}@{result.order.limit_price:.3f}"
                     if result.order else "none",
                     result.total_cost_usd,
                     f"  [{result.halted_at}]" if result.halted_at else "")
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


#: A test order must be tiny by construction, not by discipline.
TEST_ORDER_MAX_QTY = 5
TEST_ORDER_MAX_NOTIONAL = 5.00


async def place_test_order(cfg: Config, slug: str, side: str, qty: int) -> int:
    """
    One deliberately tiny live order, so the operator can watch the real
    order path post to the real account before the pipeline ever sizes
    one. Same params shape, same preview -> create sequence, same tif as
    _place(); nothing here is a second order path.

    Requires mode=live (Config.validate() enforces the I_UNDERSTAND gate
    and a pinned Jev model) and refuses while the halt sentinel exists.
    Prices at the CURRENT ask/bid from a fresh quote, IOC, so the result
    is a filled position rather than a resting order -- the truest test.
    Caps: TEST_ORDER_MAX_QTY shares, TEST_ORDER_MAX_NOTIONAL dollars.
    """
    if not cfg.is_live:
        log.error("test order requires SWARM_MODE=live on the command line")
        return EXIT_CONFIG
    if Path(os.getenv("HALT_SENTINEL", ".halted")).exists():
        log.critical("halt sentinel present; refusing to place anything")
        return EXIT_KILLSWITCH
    side = side.upper()
    if side not in ("YES", "NO"):
        log.error("side must be YES or NO")
        return EXIT_CONFIG
    if not (1 <= qty <= TEST_ORDER_MAX_QTY):
        log.error("qty must be 1..%d for a test order", TEST_ORDER_MAX_QTY)
        return EXIT_CONFIG

    async with AsyncPolymarketUS(
        key_id=cfg.polymarket_key_id, secret_key=cfg.polymarket_secret_key
    ) as pm:
        bbo = normalize_bbo(await pm.markets.bbo(slug))
        bid, ask = bbo.get("bid"), bbo.get("ask")
        if bid is None or ask is None:
            log.error("no two-sided quote on %s; not placing", slug)
            return EXIT_CONFIG
        # Marketable limit: YES lifts the ask, NO hits the bid (a NO share
        # costs 1 - bid). Same intent mapping as _place().
        if side == "YES":
            intent, limit_price, cost_per_share = "ORDER_INTENT_BUY_LONG", ask, ask
        else:
            intent, limit_price, cost_per_share = "ORDER_INTENT_BUY_SHORT", bid, 1.0 - bid
        notional = cost_per_share * qty
        if not (0.05 < limit_price < 0.95):
            log.error("price %.3f outside the sane band; not placing", limit_price)
            return EXIT_CONFIG
        if notional > TEST_ORDER_MAX_NOTIONAL:
            log.error("notional $%.2f exceeds the $%.2f test cap; not placing",
                      notional, TEST_ORDER_MAX_NOTIONAL)
            return EXIT_CONFIG

        params = {
            "marketSlug": slug,
            "intent": intent,
            "type": "ORDER_TYPE_LIMIT",
            "price": {"value": f"{limit_price:.3f}", "currency": "USD"},
            "quantity": int(qty),
            "tif": "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL",
            "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_MANUAL",
        }
        log.info("TEST ORDER: %s %s x%d @ %.3f  (~$%.2f)  book bid=%.3f ask=%.3f",
                 slug, side, qty, limit_price, notional, bid, ask)
        try:
            await pm.orders.preview({"request": params})
        except Exception as exc:
            log.error("preview rejected: %s", exc)
            return EXIT_CONFIG
        try:
            res = await pm.orders.create(params)
        except Exception as exc:
            log.error("order failed: %s", exc)
            return EXIT_CONFIG
        oid = res.get("id")
        # The create response does not carry fills: the first live test
        # returned executions=0 on an order that was already FILLED. Read
        # the order back by id; `state` and `cumQuantity` are the truth.
        # Positions populate asynchronously after the fill, so wait.
        await asyncio.sleep(1.5)
        try:
            back = await pm.orders.retrieve(oid)
            od = back.get("order", back)
            log.info("LIVE order id=%s state=%s filled=%s/%s @ %s",
                     oid, od.get("state"), od.get("cumQuantity"),
                     od.get("quantity"), (od.get("price") or {}).get("value"))
        except Exception as exc:
            log.warning("order placed (id=%s) but read-back failed: %s", oid, exc)

        pos = await pm.portfolio.positions()
        p = (pos.get("positions") or {}).get(slug)
        if p:
            log.info("position now: net=%s cost=%s cashValue=%s",
                     p.get("netPosition"), (p.get("cost") or {}).get("value"),
                     (p.get("cashValue") or {}).get("value"))
        else:
            log.warning("no position visible for %s -- if state above is "
                        "not FILLED, the IOC was cancelled unfilled", slug)
    return EXIT_OK


class _ShadowBudget:
    """Never spends, never sizes. Shadow mode touches no account."""

    async def can_spend(self, usd: float) -> bool:
        return False

    async def record_spend(self, cost: Any) -> None:
        return None

    async def available_bankroll(self) -> float:
        return 0.0


# _unconfigured_search moved to search.unconfigured_search, alongside the
# Anthropic implementation. default_search() picks between them.


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--poll", type=float, default=300.0, help="seconds between cycles")
    ap.add_argument("--once", action="store_true", help="run one cycle and exit")
    ap.add_argument("--cooldown-hours", type=float, default=20.0,
                    help="do not re-triage a market more often than this")
    ap.add_argument("--markets-per-cycle", type=int, default=150,
                    help="hard cap per pass, regardless of how many exist")
    ap.add_argument("--events-per-cycle", type=int, default=50)
    ap.add_argument("--quote-concurrency", type=int, default=2,
                    help="concurrent quote requests; >2 gets rate-limited")
    ap.add_argument("--tags", default=None,
                    help="comma-separated tag slugs to sample (e.g. nfl); "
                         "default is the general mix")
    ap.add_argument("--start-window", type=float, default=None,
                    help="only events starting within this many hours; "
                         "the nfl tag page alone is season futures")
    ap.add_argument("--research-per-cycle", type=int, default=5,
                    help="max markets sent to the paid tiers per pass, best "
                         "gate score first (~$0.12 each)")
    ap.add_argument("--research-per-event", type=int, default=1,
                    help="max researched markets from one event; its legs "
                         "resolve together and share the same facts")
    ap.add_argument("--min-hours", type=float, default=None,
                    help="research window on the EVENT clock for this run; "
                         "default keeps questions.py (6h)")
    ap.add_argument("--min-edge", type=float, default=None,
                    help="override RiskConfig.min_edge for this run (default "
                         "0.10). For PAPER runs: shows what the sizer would "
                         "do at a lower bar. Buying every 2026-09-20 "
                         "escalation below that bar returned -28%%")
    ap.add_argument("--test-order", metavar="SLUG", default=None,
                    help="place ONE tiny live order on this market and exit. "
                         "Requires SWARM_MODE=live and the I_UNDERSTAND gate "
                         "on the command line; capped at "
                         f"{TEST_ORDER_MAX_QTY} shares / "
                         f"${TEST_ORDER_MAX_NOTIONAL:.0f}")
    ap.add_argument("--test-side", default="YES", help="YES or NO")
    ap.add_argument("--test-qty", type=int, default=1)
    args = ap.parse_args()

    try:
        cfg = Config.from_env().require()
    except ConfigError as exc:
        setup_logging("INFO")
        log.error("%s", exc)
        return EXIT_CONFIG

    setup_logging(cfg.log_level)
    log.info("starting\n%s", cfg.describe())

    if args.test_order:
        # One tiny order through the real path, then exit. Never enters
        # the cycle loop; never runs the pipeline.
        return asyncio.run(
            place_test_order(cfg, args.test_order, args.test_side, args.test_qty)
        )

    daemon = Daemon(
        cfg,
        poll_seconds=args.poll,
        once=args.once,
        cooldown_hours=args.cooldown_hours,
        markets_per_cycle=args.markets_per_cycle,
        events_per_cycle=args.events_per_cycle,
        quote_concurrency=args.quote_concurrency,
        tags=tuple(t.strip() for t in args.tags.split(",")) if args.tags else None,
        start_window_hours=args.start_window,
        min_hours_to_event=args.min_hours,
        min_edge=args.min_edge,
        research_per_cycle=args.research_per_cycle,
        research_per_event=args.research_per_event,
    )

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
