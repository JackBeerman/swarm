"""
risk_engine.py -- net liquid value, cost accounting, and the kill switch.

This is the module that stands between the pipeline and an empty account.
Everything here is deliberately boring and deterministic.

Four things the original spec got wrong, all corrected below:

1. NLV IS NOT THE CASH BALANCE. The spec read `GET /v1/portfolio/balances`
   and compared it to a floor. But an account holding $15 cash and $60 of
   open positions is not near a $10 floor -- and the naive version would
   liquidate a healthy book. `UserBalance` already exposes `assetNotional`
   (marked value of holdings) and `buyingPower`; `UserPosition.cashValue`
   gives per-position marks. Use them.

2. API SPEND AND TRADING EQUITY ARE DIFFERENT LEDGERS. Inference is billed
   to a card; it never leaves the Polymarket balance. Subtracting one from
   the other is a modelling choice, not an accounting fact. Both are
   tracked separately here and combined only at the final comparison, so a
   halt always names which ledger actually breached.

3. ACCRUED SPEND MUST SURVIVE RESTART. An in-memory counter resets on
   every restart, so a crash-looping daemon burns the treasury with a
   kill switch that believes it has spent nothing. The ledger is sqlite.

4. `sys.exit(0)` IS THE WRONG HALT. Exit code 0 tells a supervisor the
   process succeeded; `Restart=always` brings it straight back up with a
   fresh in-memory state and it trades again. Halting writes a sentinel
   file that blocks startup until a human clears it, cancels open orders
   first, and exits non-zero.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from adapters import amount
from schemas import StageCost

log = logging.getLogger("risk")

LEDGER_PATH = os.getenv("RISK_LEDGER_DB", "ledger.db")
HALT_SENTINEL = Path(os.getenv("HALT_SENTINEL", ".halted"))


class KillSwitch(Exception):
    """Raised when a floor is breached. Never catch this to keep trading."""

    def __init__(self, reason: str, snapshot: "RiskSnapshot | None" = None):
        super().__init__(reason)
        self.reason = reason
        self.snapshot = snapshot


# ==========================================================================
# configuration
# ==========================================================================

@dataclass(frozen=True)
class RiskLimits:
    """
    Every number a human needs to review before this daemon touches money.
    """

    starting_treasury_usd: float = 100.00

    # Absolute floor on net liquid value. Breach = permanent halt.
    hard_floor_usd: float = 10.00

    # Session drawdown limit, as a fraction of the session's opening NLV.
    # Distinct from the hard floor: catches a fast bleed that has not yet
    # reached the floor, which is when stopping is still cheap.
    max_session_drawdown: float = 0.25

    # Cap on cumulative inference spend, independent of trading P&L. A
    # daemon that loses no money but burns $80 on tokens has still failed.
    max_cumulative_api_usd: float = 25.00

    # Refuse to open an evaluation unless this much headroom remains above
    # the floor. Without it the last evaluation can push NLV under.
    min_headroom_usd: float = 5.00

    # Balance snapshots older than this are refetched before any decision.
    balance_staleness_seconds: float = 30.0


@dataclass
class RiskSnapshot:
    """A point-in-time view. All figures USD."""

    cash: float = 0.0
    buying_power: float = 0.0
    position_value: float = 0.0
    open_order_value: float = 0.0
    unsettled: float = 0.0

    api_spend_session: float = 0.0
    api_spend_cumulative: float = 0.0

    taken_at: float = field(default_factory=time.time)

    @property
    def trading_equity(self) -> float:
        """What the exchange says the account is worth, marked to market."""
        return self.cash + self.position_value + self.unsettled

    @property
    def net_liquid_value(self) -> float:
        """Trading equity less cumulative inference burn."""
        return self.trading_equity - self.api_spend_cumulative

    def explain(self) -> str:
        return (
            f"cash=${self.cash:.2f} positions=${self.position_value:.2f} "
            f"unsettled=${self.unsettled:.2f} "
            f"| equity=${self.trading_equity:.2f} "
            f"api=${self.api_spend_cumulative:.4f} "
            f"| NLV=${self.net_liquid_value:.2f}"
        )


# ==========================================================================
# persistent cost ledger
# ==========================================================================

LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS spend (
    id           INTEGER PRIMARY KEY,
    at           TEXT NOT NULL,
    session_id   TEXT NOT NULL,
    stage        TEXT NOT NULL,
    model        TEXT NOT NULL,
    usd          REAL NOT NULL,
    in_tokens    INTEGER DEFAULT 0,
    out_tokens   INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_spend_session ON spend(session_id);

CREATE TABLE IF NOT EXISTS halts (
    id         INTEGER PRIMARY KEY,
    at         TEXT NOT NULL,
    session_id TEXT NOT NULL,
    reason     TEXT NOT NULL,
    snapshot   TEXT
);
"""


class CostLedger:
    """
    Append-only spend log. sqlite rather than memory so a crash-looping
    daemon cannot forget what it has already burned.
    """

    def __init__(self, path: str = LEDGER_PATH, session_id: str | None = None):
        self.path = path
        self.session_id = session_id or datetime.now(timezone.utc).strftime(
            "%Y%m%dT%H%M%S"
        )
        with closing(self._conn()) as c:
            c.executescript(LEDGER_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def record(self, cost: StageCost) -> None:
        with closing(self._conn()) as c:
            c.execute(
                "INSERT INTO spend (at, session_id, stage, model, usd,"
                " in_tokens, out_tokens) VALUES (?,?,?,?,?,?,?)",
                (
                    datetime.now(timezone.utc).isoformat(),
                    self.session_id,
                    cost.stage,
                    cost.model,
                    cost.usd,
                    cost.input_tokens,
                    cost.output_tokens,
                ),
            )
            c.commit()

    def total(self, session_only: bool = False) -> float:
        q = "SELECT COALESCE(SUM(usd), 0.0) AS t FROM spend"
        args: tuple = ()
        if session_only:
            q += " WHERE session_id = ?"
            args = (self.session_id,)
        with closing(self._conn()) as c:
            return float(c.execute(q, args).fetchone()["t"])

    def by_stage(self) -> dict[str, float]:
        with closing(self._conn()) as c:
            return {
                r["stage"]: float(r["s"])
                for r in c.execute(
                    "SELECT stage, SUM(usd) AS s FROM spend GROUP BY stage"
                )
            }

    def record_halt(self, reason: str, snap: RiskSnapshot | None) -> None:
        with closing(self._conn()) as c:
            c.execute(
                "INSERT INTO halts (at, session_id, reason, snapshot)"
                " VALUES (?,?,?,?)",
                (
                    datetime.now(timezone.utc).isoformat(),
                    self.session_id,
                    reason,
                    json.dumps(snap.__dict__, default=str) if snap else None,
                ),
            )
            c.commit()


# ==========================================================================
# the engine
# ==========================================================================

class RiskEngine:
    """
    Implements the BudgetGuard protocol that swarm.Swarm consumes.

    Usage:
        engine = RiskEngine(pm_client)
        await engine.start()            # refuses if a halt sentinel exists
        swarm = Swarm(..., budget=engine)
        ...
        await engine.check_floors()     # call on every loop iteration
    """

    def __init__(
        self,
        client: Any,
        limits: RiskLimits | None = None,
        ledger: CostLedger | None = None,
        dry_run: bool = True,
    ):
        self.client = client
        self.limits = limits or RiskLimits()
        self.ledger = ledger or CostLedger()
        self.dry_run = dry_run

        # taken_at=0.0, not now(): a snapshot that has never been fetched
        # must read as infinitely stale. With the default factory the
        # staleness check short-circuits on the very first call and every
        # consumer silently sees an all-zeros account.
        self._snapshot = RiskSnapshot(taken_at=0.0)
        self._session_open_nlv: float | None = None
        self._lock = asyncio.Lock()
        self._halted = False

    # ---- lifecycle ---------------------------------------------------

    async def start(self) -> RiskSnapshot:
        """
        Refuse to run if a previous session halted. The sentinel must be
        removed by a human -- that is the entire point of it. An automatic
        restart after a floor breach is how a bounded loss becomes an
        unbounded one.
        """
        if HALT_SENTINEL.exists():
            raise KillSwitch(
                f"halt sentinel present at {HALT_SENTINEL} -- "
                f"a previous session hit a floor. Review the ledger's halts "
                f"table, then delete the file to resume."
            )
        snap = await self.refresh(force=True)
        self._session_open_nlv = snap.net_liquid_value
        log.info("session opened: %s", snap.explain())
        if snap.net_liquid_value <= self.limits.hard_floor_usd:
            await self.halt("already below hard floor at startup", snap)
        return snap

    # ---- state -------------------------------------------------------

    async def refresh(self, force: bool = False) -> RiskSnapshot:
        """Pull balances and positions, marked to market by the exchange."""
        age = time.time() - self._snapshot.taken_at
        if not force and age < self.limits.balance_staleness_seconds:
            return self._snapshot

        async with self._lock:
            cash = buying_power = position_value = 0.0
            open_orders = unsettled = 0.0

            try:
                balances = await _maybe_await(self.client.account.balances())
                for b in balances.get("balances", []):
                    if (b.get("currency") or "USD").upper() != "USD":
                        continue
                    cash += float(b.get("currentBalance") or 0.0)
                    buying_power += float(b.get("buyingPower") or 0.0)
                    # assetNotional is the exchange's own mark on holdings.
                    position_value += float(b.get("assetNotional") or 0.0)
                    open_orders += float(b.get("openOrders") or 0.0)
                    unsettled += float(b.get("unsettledFunds") or 0.0)
            except Exception as exc:
                log.error("balance fetch failed: %s", exc)
                # Do not fabricate a healthy snapshot on an API failure --
                # return the last known one and let staleness surface it.
                return self._snapshot

            # Cross-check against per-position cashValue when available.
            # GetUserPositionsResponse.positions is a DICT keyed by slug,
            # not a list; iterating it as a list yields bare strings.
            try:
                pos = await _maybe_await(self.client.portfolio.positions())
                marks = sum(
                    amount(p.get("cashValue")) or 0.0
                    for p in (pos.get("positions") or {}).values()
                )
                if marks and abs(marks - position_value) > 0.01:
                    log.debug(
                        "position mark disagreement: balances=%.2f positions=%.2f"
                        " -- using the more conservative figure",
                        position_value, marks,
                    )
                    position_value = min(position_value, marks)
            except Exception as exc:
                log.debug("position fetch failed, using balance marks: %s", exc)

            self._snapshot = RiskSnapshot(
                cash=cash,
                buying_power=buying_power,
                position_value=position_value,
                open_order_value=open_orders,
                unsettled=unsettled,
                api_spend_session=self.ledger.total(session_only=True),
                api_spend_cumulative=self.ledger.total(),
            )
            return self._snapshot

    @property
    def snapshot(self) -> RiskSnapshot:
        return self._snapshot

    # ---- BudgetGuard protocol ----------------------------------------

    async def can_spend(self, usd: float) -> bool:
        """Consulted before every paid stage."""
        if self._halted:
            return False
        snap = await self.refresh()

        if snap.api_spend_cumulative + usd > self.limits.max_cumulative_api_usd:
            log.warning(
                "refusing spend: cumulative API $%.4f + $%.4f > cap $%.2f",
                snap.api_spend_cumulative, usd, self.limits.max_cumulative_api_usd,
            )
            return False

        projected = snap.net_liquid_value - usd
        if projected < self.limits.hard_floor_usd + self.limits.min_headroom_usd:
            log.warning(
                "refusing spend: projected NLV $%.2f under floor+headroom $%.2f",
                projected,
                self.limits.hard_floor_usd + self.limits.min_headroom_usd,
            )
            return False
        return True

    async def record_spend(self, cost: StageCost) -> None:
        self.ledger.record(cost)
        self._snapshot.api_spend_session += cost.usd
        self._snapshot.api_spend_cumulative += cost.usd

    async def available_bankroll(self) -> float:
        """
        What sizing may draw on.

        buyingPower, not cash: it already nets out margin requirements,
        balance reservations and working orders, which cash does not. The
        hard floor is held back so a position cannot itself trip the
        switch.
        """
        snap = await self.refresh()
        usable = min(snap.buying_power, snap.net_liquid_value)
        return max(0.0, usable - self.limits.hard_floor_usd)

    # ---- the switch ---------------------------------------------------

    async def check_floors(self) -> RiskSnapshot:
        """Call every loop iteration. Raises KillSwitch on any breach."""
        snap = await self.refresh(force=True)

        if snap.net_liquid_value <= self.limits.hard_floor_usd:
            await self.halt(
                f"NLV ${snap.net_liquid_value:.2f} <= hard floor "
                f"${self.limits.hard_floor_usd:.2f}",
                snap,
            )

        if snap.api_spend_cumulative >= self.limits.max_cumulative_api_usd:
            await self.halt(
                f"cumulative API spend ${snap.api_spend_cumulative:.2f} "
                f">= cap ${self.limits.max_cumulative_api_usd:.2f}",
                snap,
            )

        if self._session_open_nlv:
            drawdown = 1.0 - (snap.net_liquid_value / self._session_open_nlv)
            if drawdown >= self.limits.max_session_drawdown:
                await self.halt(
                    f"session drawdown {drawdown:.1%} >= "
                    f"{self.limits.max_session_drawdown:.0%} "
                    f"(${self._session_open_nlv:.2f} -> "
                    f"${snap.net_liquid_value:.2f})",
                    snap,
                )
        return snap

    async def halt(self, reason: str, snap: RiskSnapshot | None = None) -> None:
        """
        Cancel open orders, write the sentinel, record, then raise.

        Order matters: cancel first. Writing a sentinel and exiting while
        limit orders rest on the book leaves unmanaged exposure behind,
        which is worse than the condition that triggered the halt.
        """
        if self._halted:
            raise KillSwitch(reason, snap)
        self._halted = True

        log.critical("KILL SWITCH: %s", reason)
        if snap:
            log.critical("  %s", snap.explain())

        if self.dry_run:
            log.critical("  dry_run=True -- not cancelling live orders")
        else:
            try:
                res = await _maybe_await(self.client.orders.cancel_all())
                ids = (res or {}).get("canceledOrderIds", [])
                log.critical("  cancelled %d open order(s)", len(ids))
            except Exception as exc:
                # Log loudly and keep halting. A failed cancel is a reason
                # to stop faster, not to stay up and keep trading.
                log.critical("  CANCEL-ALL FAILED: %s -- orders may rest", exc)

        self.ledger.record_halt(reason, snap)
        try:
            HALT_SENTINEL.write_text(
                f"{datetime.now(timezone.utc).isoformat()}\n{reason}\n"
            )
        except OSError as exc:
            log.critical("  could not write halt sentinel: %s", exc)

        raise KillSwitch(reason, snap)

    # ---- reporting -----------------------------------------------------

    def report(self) -> str:
        s = self._snapshot
        by_stage = self.ledger.by_stage()
        lines = [
            "",
            "=" * 58,
            f"  session {self.ledger.session_id}",
            "=" * 58,
            f"  cash                 ${s.cash:>10.2f}",
            f"  positions (marked)   ${s.position_value:>10.2f}",
            f"  unsettled            ${s.unsettled:>10.2f}",
            f"  buying power         ${s.buying_power:>10.2f}",
            f"  {'-' * 54}",
            f"  trading equity       ${s.trading_equity:>10.2f}",
        ]
        for stage, usd in sorted(by_stage.items()):
            lines.append(f"    api/{stage:<16} ${usd:>10.4f}")
        lines += [
            f"  api spend (total)    ${s.api_spend_cumulative:>10.4f}",
            f"  {'-' * 54}",
            f"  NET LIQUID VALUE     ${s.net_liquid_value:>10.2f}",
            f"  hard floor           ${self.limits.hard_floor_usd:>10.2f}",
        ]
        if self._session_open_nlv:
            dd = 1.0 - (s.net_liquid_value / self._session_open_nlv)
            lines.append(f"  session drawdown     {dd:>10.1%}")
        lines.append("")
        return "\n".join(lines)


async def _maybe_await(v: Any) -> Any:
    """The SDK has sync and async clients; accept either."""
    if asyncio.iscoroutine(v) or isinstance(v, asyncio.Future):
        return await v
    return v
