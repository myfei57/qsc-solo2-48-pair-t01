"""液面门控：正常投料路径与恢复路径都要现场复核液面。"""

from __future__ import annotations

import pytest

from glassmelt.errors import InterlockError, SequenceError


def test_level_check_is_fresh_each_time(platform):
    level = platform.level
    level.observe(platform.config.furnace.level_target_mm)
    first = level.check()
    assert first.ok is True
    level.observe(platform.config.furnace.level_target_mm + 90.0)
    second = level.check()
    assert second.ok is False
    assert second.high is True
    assert second.level_mm > first.level_mm
    assert level.last_confirmation().as_dict() == second.as_dict()


def test_clear_invalidates_the_confirmation(platform):
    platform.level.observe(platform.config.furnace.level_target_mm)
    platform.level.check()
    assert platform.level.last_confirmation() is not None
    assert platform.level.epoch == 0
    epoch = platform.level.clear()
    assert epoch == 1
    assert platform.level.last_confirmation() is None
    assert platform.level.snapshot()["last_confirmation"] is None


def test_resume_path_also_gates_on_level(platform):
    platform.interrupt_charging("测试用投料联锁")
    platform.level.observe(platform.config.furnace.level_target_mm + 80.0)
    with pytest.raises(InterlockError):
        platform.start_charging(path="resume")
    assert platform.charger.interrupted is True
    platform.level.observe(platform.config.furnace.level_target_mm)
    outcome = platform.start_charging(path="resume")
    assert outcome["path"] == "resume"
    assert platform.charger.interrupted is False


def test_resume_without_interlock_is_rejected(platform):
    with pytest.raises(InterlockError):
        platform.start_charging(path="resume")


def test_unknown_charge_path_is_rejected(platform):
    with pytest.raises(SequenceError):
        platform.start_charging(path="rewind")


def test_cleared_furnace_requires_a_new_confirmation(platform):
    platform.level.observe(platform.config.furnace.level_target_mm)
    platform.level.check()
    cleared = platform.clear_furnace("出料")
    assert cleared["level_epoch"] == 1
    assert cleared["charger"]["interrupted"] is True
    platform.level.observe(platform.config.furnace.level_target_mm + 70.0)
    with pytest.raises(InterlockError):
        platform.start_charging(path="resume")


def test_charge_history_and_counts_are_tracked(platform):
    platform.level.observe(platform.config.furnace.level_target_mm)
    platform.start_charging(path="start")
    platform.level.observe(platform.config.furnace.level_target_mm)
    platform.start_charging(path="start")
    snapshot = platform.charger.snapshot()
    assert snapshot["batch_count"] == 2
    assert snapshot["charged_kg"] == pytest.approx(
        2 * platform.config.batch.charge_batch_kg
    )
    assert len(snapshot["recent"]) == 2
    assert platform.charger.history(limit=1)[0]["path"] == "start"
