"""Config tests -- mostly about refusing to run in unsafe states."""
from __future__ import annotations
import os
import pytest
from config import Config, ConfigError

pytestmark = pytest.mark.asyncio


def base(**kw):
    d = dict(typesafe_api_key="k", polymarket_key_id="i",
             polymarket_secret_key="s", mode="shadow")
    d.update(kw)
    return Config(**d)


async def test_shadow_mode_needs_no_trading_credentials():
    c = Config(typesafe_api_key="k", mode="shadow")
    assert c.validate() == []


async def test_paper_mode_requires_trading_credentials():
    c = Config(typesafe_api_key="k", mode="paper")
    problems = c.validate()
    assert any("POLYMARKET_KEY_ID" in p for p in problems)


async def test_live_requires_explicit_acknowledgement(monkeypatch):
    monkeypatch.delenv("I_UNDERSTAND_THIS_TRADES_REAL_MONEY", raising=False)
    problems = base(mode="live", jev_model="jev-1.13-20260917").validate()
    assert any("I_UNDERSTAND_THIS_TRADES_REAL_MONEY" in p for p in problems)


async def test_live_refuses_an_unpinned_model(monkeypatch):
    """
    'jev-latest' and the family slug both float. A model update would
    recalibrate every gate threshold silently, which is the kind of
    failure you only notice in the P&L.
    """
    monkeypatch.setenv("I_UNDERSTAND_THIS_TRADES_REAL_MONEY", "yes")
    problems = base(mode="live", jev_model="jev-latest").validate()
    assert any("unpinned" in p for p in problems)

    ok = base(mode="live", jev_model="jev-1.13-20260917").validate()
    assert ok == []


async def test_default_mode_is_shadow(monkeypatch):
    monkeypatch.delenv("SWARM_MODE", raising=False)
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    c = Config.from_env()
    assert c.mode == "shadow"
    assert c.is_live is False


async def test_bad_mode_rejected(monkeypatch):
    monkeypatch.setenv("SWARM_MODE", "yolo")
    with pytest.raises(ConfigError):
        Config.from_env()


async def test_require_raises_with_all_problems():
    with pytest.raises(ConfigError) as ex:
        Config(mode="paper").require()
    msg = str(ex.value)
    assert "TYPESAFE_API_KEY" in msg and "POLYMARKET_KEY_ID" in msg


async def test_describe_masks_secrets():
    out = base(polymarket_secret_key="supersecretvalue").describe()
    assert "supersecretvalue" not in out
    assert "set (16 chars)" in out
