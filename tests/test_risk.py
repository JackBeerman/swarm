"""
Risk engine tests. The kill switch is the one component where a bug is
unrecoverable, so these lean on the failure modes rather than happy paths.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("TYPESAFE_API_KEY", "test-key")

import risk_engine as re_  # noqa: E402
from risk_engine import (  # noqa: E402
    CostLedger,
    KillSwitch,
    RiskEngine,
    RiskLimits,
)
from schemas import StageCost  # noqa: E402

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------

class FakeAccount:
    def __init__(self, **kw):
        self.data = {
            "currentBalance": 40.0,
            "currency": "USD",
            "buyingPower": 38.0,
            "assetNotional": 55.0,
            "openOrders": 0.0,
            "unsettledFunds": 0.0,
        }
        self.data.update(kw)
        self.fail = False

    def balances(self):
        if self.fail:
            raise RuntimeError("upstream 503")
        return {"balances": [self.data]}


class FakeOrders:
    def __init__(self):
        self.cancel_all_calls = 0
        self.fail_cancel = False

    def cancel_all(self, params=None):
        self.cancel_all_calls += 1
        if self.fail_cancel:
            raise RuntimeError("cancel failed")
        return {"canceledOrderIds": ["o1", "o2"]}


class FakePortfolio:
    def __init__(self, marks=None):
        # positions is a DICT keyed by slug, per the real SDK
        self.marks = marks if marks is not None else {
            "m1": {"cashValue": {"value": "55.00", "currency": "USD"}}
        }

    def positions(self, params=None):
        return {"positions": self.marks}


class FakeClient:
    def __init__(self, **kw):
        self.account = FakeAccount(**kw)
        self.orders = FakeOrders()
        self.portfolio = FakePortfolio()


@pytest.fixture
def tmp_env(monkeypatch, tmp_path):
    monkeypatch.setattr(re_, "HALT_SENTINEL", tmp_path / ".halted")
    return tmp_path


def make_engine(tmp_path, client=None, **limit_kw):
    ledger = CostLedger(path=str(tmp_path / "ledger.db"))
    return RiskEngine(
        client or FakeClient(),
        limits=RiskLimits(**limit_kw),
        ledger=ledger,
        dry_run=False,
    )


# --------------------------------------------------------------------------
# NLV -- the bug the spec would have shipped
# --------------------------------------------------------------------------

async def test_open_positions_count_toward_nlv(tmp_env):
    """
    $40 cash + $55 of open positions is a healthy account, not one at a
    $10 floor. The cash-only version would have liquidated it.
    """
    e = make_engine(tmp_env)
    snap = await e.refresh(force=True)
    assert snap.cash == 40.0
    assert snap.position_value == 55.0
    assert snap.trading_equity == pytest.approx(95.0)
    assert snap.net_liquid_value == pytest.approx(95.0)
    await e.check_floors()   # must not raise


async def test_cash_only_view_would_have_falsely_halted(tmp_env):
    """Same account, judged on cash alone, sits under a $50 floor."""
    e = make_engine(tmp_env, hard_floor_usd=50.0)
    snap = await e.refresh(force=True)
    assert snap.cash < 50.0                      # naive check would halt
    assert snap.net_liquid_value > 50.0          # correct check does not
    await e.check_floors()


async def test_conservative_mark_wins_on_disagreement(tmp_env):
    c = FakeClient()
    c.portfolio = FakePortfolio(
        {"m1": {"cashValue": {"value": "40.00", "currency": "USD"}}}
    )
    e = make_engine(tmp_env, client=c)
    snap = await e.refresh(force=True)
    assert snap.position_value == 40.0, "take the lower of the two marks"


async def test_positions_dict_not_list(tmp_env):
    """
    GetUserPositionsResponse.positions is a dict keyed by slug. Iterating
    it as a list yields bare strings and silently marks everything at 0.
    """
    c = FakeClient()
    e = make_engine(tmp_env, client=c)
    raw = c.portfolio.positions()
    assert isinstance(raw["positions"], dict)
    snap = await e.refresh(force=True)
    assert snap.position_value > 0


# --------------------------------------------------------------------------
# persistence
# --------------------------------------------------------------------------

async def test_spend_survives_restart(tmp_env):
    """A crash-looping daemon must not forget what it already burned."""
    path = str(tmp_env / "ledger.db")
    l1 = CostLedger(path=path, session_id="s1")
    for _ in range(10):
        l1.record(StageCost(stage="gather", model="m", usd=0.50))
    assert l1.total() == pytest.approx(5.0)

    l2 = CostLedger(path=path, session_id="s2")   # "restart"
    assert l2.total() == pytest.approx(5.0), "cumulative spend must persist"
    assert l2.total(session_only=True) == 0.0, "new session starts at zero"


async def test_cumulative_api_cap_blocks_spending(tmp_env):
    e = make_engine(tmp_env, max_cumulative_api_usd=1.00)
    for _ in range(5):
        await e.record_spend(StageCost(stage="gather", model="m", usd=0.20))
    await e.refresh(force=True)
    assert await e.can_spend(0.20) is False


async def test_by_stage_breakdown(tmp_env):
    e = make_engine(tmp_env)
    await e.record_spend(StageCost(stage="triage", model="jev", usd=0.0001))
    await e.record_spend(StageCost(stage="gather", model="flash", usd=0.09))
    await e.record_spend(StageCost(stage="synthesize", model="astra", usd=0.08))
    by = e.ledger.by_stage()
    assert by["synthesize"] > by["triage"] * 100


# --------------------------------------------------------------------------
# the switch
# --------------------------------------------------------------------------

async def test_hard_floor_cancels_orders_before_halting(tmp_env):
    c = FakeClient(currentBalance=5.0, assetNotional=0.0, buyingPower=5.0)
    e = make_engine(tmp_env, client=c, hard_floor_usd=10.0)
    with pytest.raises(KillSwitch) as ex:
        await e.check_floors()
    assert "hard floor" in str(ex.value)
    assert c.orders.cancel_all_calls == 1, "must cancel before exiting"


async def test_failed_cancel_still_halts(tmp_env):
    """A failed cancel is a reason to stop faster, not to keep trading."""
    c = FakeClient(currentBalance=5.0, assetNotional=0.0)
    c.orders.fail_cancel = True
    e = make_engine(tmp_env, client=c, hard_floor_usd=10.0)
    with pytest.raises(KillSwitch):
        await e.check_floors()
    assert re_.HALT_SENTINEL.exists()


async def test_halt_writes_sentinel_and_blocks_restart(tmp_env):
    c = FakeClient(currentBalance=5.0, assetNotional=0.0)
    e = make_engine(tmp_env, client=c, hard_floor_usd=10.0)
    with pytest.raises(KillSwitch):
        await e.check_floors()
    assert re_.HALT_SENTINEL.exists()

    # A supervisor restart must not resume trading.
    e2 = make_engine(tmp_env, client=FakeClient(), hard_floor_usd=10.0)
    with pytest.raises(KillSwitch) as ex:
        await e2.start()
    assert "sentinel" in str(ex.value)


async def test_session_drawdown_halts_before_the_floor(tmp_env):
    """Catch a fast bleed while stopping is still cheap."""
    c = FakeClient(currentBalance=100.0, assetNotional=0.0, buyingPower=100.0)
    e = make_engine(tmp_env, client=c, hard_floor_usd=10.0,
                    max_session_drawdown=0.25)
    await e.start()
    assert e._session_open_nlv == pytest.approx(100.0)

    c.account.data["currentBalance"] = 70.0     # -30%, still above floor
    with pytest.raises(KillSwitch) as ex:
        await e.check_floors()
    assert "drawdown" in str(ex.value)


async def test_halt_is_idempotent(tmp_env):
    c = FakeClient(currentBalance=1.0, assetNotional=0.0)
    e = make_engine(tmp_env, client=c, hard_floor_usd=10.0)
    with pytest.raises(KillSwitch):
        await e.check_floors()
    with pytest.raises(KillSwitch):
        await e.halt("again")
    assert c.orders.cancel_all_calls == 1, "must not re-cancel on repeat halt"


async def test_halted_engine_refuses_all_spending(tmp_env):
    c = FakeClient(currentBalance=1.0, assetNotional=0.0)
    e = make_engine(tmp_env, client=c, hard_floor_usd=10.0)
    with pytest.raises(KillSwitch):
        await e.check_floors()
    assert await e.can_spend(0.01) is False


# --------------------------------------------------------------------------
# headroom and bankroll
# --------------------------------------------------------------------------

async def test_headroom_prevents_the_last_evaluation(tmp_env):
    """Without headroom the final evaluation can itself breach the floor."""
    c = FakeClient(currentBalance=13.0, assetNotional=0.0, buyingPower=13.0)
    e = make_engine(tmp_env, client=c, hard_floor_usd=10.0, min_headroom_usd=5.0)
    await e.refresh(force=True)
    assert await e.can_spend(0.25) is False


async def test_bankroll_withholds_the_floor(tmp_env):
    c = FakeClient(currentBalance=60.0, assetNotional=0.0, buyingPower=60.0)
    e = make_engine(tmp_env, client=c, hard_floor_usd=10.0)
    assert await e.available_bankroll() == pytest.approx(50.0)


async def test_bankroll_uses_buying_power_not_cash(tmp_env):
    """buyingPower nets out reservations and working orders; cash does not."""
    c = FakeClient(currentBalance=60.0, buyingPower=25.0, assetNotional=0.0)
    e = make_engine(tmp_env, client=c, hard_floor_usd=10.0)
    assert await e.available_bankroll() == pytest.approx(15.0)


async def test_bankroll_never_negative(tmp_env):
    c = FakeClient(currentBalance=3.0, buyingPower=3.0, assetNotional=0.0)
    e = make_engine(tmp_env, client=c, hard_floor_usd=10.0)
    assert await e.available_bankroll() == 0.0


# --------------------------------------------------------------------------
# failure modes
# --------------------------------------------------------------------------

async def test_balance_api_failure_does_not_fabricate_health(tmp_env):
    """
    An API outage must not produce a rosy snapshot. Returning the last
    known state is right; inventing zeros would trip the switch, and
    inventing a healthy balance would disable it.
    """
    c = FakeClient()
    e = make_engine(tmp_env, client=c)
    good = await e.refresh(force=True)
    assert good.trading_equity == pytest.approx(95.0)

    c.account.fail = True
    stale = await e.refresh(force=True)
    assert stale.trading_equity == pytest.approx(95.0), "last known, not zero"


async def test_non_usd_balances_ignored(tmp_env):
    c = FakeClient()
    c.account.data = {"currentBalance": 999.0, "currency": "EUR",
                      "buyingPower": 999.0, "assetNotional": 0.0}
    e = make_engine(tmp_env, client=c)
    snap = await e.refresh(force=True)
    assert snap.cash == 0.0


async def test_dry_run_does_not_cancel_live_orders(tmp_env):
    c = FakeClient(currentBalance=1.0, assetNotional=0.0)
    ledger = CostLedger(path=str(tmp_env / "l.db"))
    e = RiskEngine(c, RiskLimits(hard_floor_usd=10.0), ledger, dry_run=True)
    with pytest.raises(KillSwitch):
        await e.check_floors()
    assert c.orders.cancel_all_calls == 0


async def test_report_renders(tmp_env):
    e = make_engine(tmp_env)
    await e.record_spend(StageCost(stage="triage", model="jev", usd=0.00007))
    await e.refresh(force=True)
    out = e.report()
    assert "NET LIQUID VALUE" in out
    assert "api/triage" in out


async def test_first_use_always_fetches(tmp_env):
    """
    A never-fetched snapshot must read as infinitely stale. With a
    default timestamp of now(), the staleness check short-circuits on the
    first call and every consumer sees an all-zeros account -- which
    silently disables sizing and refuses every spend.
    """
    e = make_engine(tmp_env)
    assert e.snapshot.taken_at == 0.0
    bankroll = await e.available_bankroll()     # no explicit refresh
    assert bankroll > 0, "first call must fetch, not return the empty snapshot"
    assert await e.can_spend(0.25) is True
