"""The daily runner's guards and step selection. Runs nothing."""

import pytest

import run_daily as rd


def test_backfills_run_before_anything_reports_on_them():
    names = [s[0] for s in rd.STEPS]
    assert names.index("weather-backfill") < names.index("weather")
    assert names.index("dashboard-export") == len(names) - 1


def test_select_by_name_or_prefix():
    assert [s[0] for s in rd.select_steps("weather")] == ["weather-backfill", "weather"]
    assert [s[0] for s in rd.select_steps("arb,dashboard-export")] == ["arb", "dashboard-export"]
    with pytest.raises(SystemExit):
        rd.select_steps("launch-rockets")


def test_no_step_can_reach_the_order_path():
    for _, argv, _ in rd.STEPS:
        assert argv[0] != "daemon.py"
        assert "--test-order" not in argv


def test_lock_blocks_a_second_run_and_stale_locks_are_taken_over(tmp_path, monkeypatch):
    monkeypatch.setattr(rd, "LOCK", tmp_path / ".daily.lock")
    assert rd.acquire_lock() is True
    assert rd.acquire_lock() is False
    assert rd.acquire_lock(max_age_h=0) is True
