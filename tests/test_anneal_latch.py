"""退火侧：告警复位后闩锁必须释放，网带与加热器跟着恢复。"""

from __future__ import annotations

import pytest

from glassmelt.app import CycleInputs


def test_alarm_sets_and_clears_with_hysteresis(platform):
    limits = platform.config.temp
    assert platform.alarm.observe(limits.anneal_alarm_c + 5.0) is True
    assert platform.alarm.observe(limits.anneal_clear_c + 1.0) is True
    assert platform.alarm.observe(limits.anneal_clear_c - 1.0) is False
    counts = platform.alarm.counts()
    assert counts["sets"] == 1
    assert counts["clears"] == 1
    assert platform.alarm.transitions()[-1]["active"] is False


def test_latch_follows_the_alarm(platform):
    platform.alarm.observe(platform.config.temp.anneal_alarm_c + 4.0)
    assert platform.latch.follow_alarm(platform.alarm) is True
    assert platform.latch.latched is True
    assert platform.latch.snapshot()["sets"] == 1
    platform.alarm.observe(platform.config.temp.anneal_clear_c - 2.0)
    assert platform.latch.follow_alarm(platform.alarm) is True
    assert platform.latch.latched is False
    assert platform.latch.snapshot()["releases"] == 1


def test_release_is_refused_while_the_alarm_is_active(platform):
    platform.alarm.observe(platform.config.temp.anneal_alarm_c + 4.0)
    platform.latch.latch("退火温度告警")
    assert platform.latch.release_after_clear(platform.alarm) is False
    assert platform.latch.latched is True


def test_belt_slows_when_latched_and_recovers_after_release(running):
    excursion = running.evaluate_anneal(running.config.temp.anneal_alarm_c + 8.0)
    assert excursion["alarm_active"] is True
    assert excursion["latch"]["latched"] is True
    assert excursion["belt"]["latched"] is True
    assert excursion["belt"]["speed_m_per_min"] == pytest.approx(
        running.config.lehr.belt_slow_m_per_min
    )
    assert excursion["heater"]["power_pct"] == pytest.approx(
        running.config.lehr.heater_min_pct
    )

    reset = running.reset_lehr("退火复位")
    assert reset["alarm_cleared"] is True
    assert reset["latch_released"] is True
    assert reset["belt"]["speed_m_per_min"] == pytest.approx(
        running.config.lehr.belt_speed_m_per_min
    )
    assert reset["heater"]["power_pct"] > running.config.lehr.heater_min_pct


def test_heater_returns_to_control_after_release(running):
    running.alarm.observe(running.config.temp.anneal_alarm_c + 6.0)
    running.latch.follow_alarm(running.alarm)
    held = running.heater.step(560.0, 500.0, running.latch)
    assert held.power_pct == pytest.approx(running.config.lehr.heater_min_pct)
    running.alarm.clear("人工复位")
    running.latch.release_after_clear(running.alarm)
    restored = running.heater.step(560.0, 500.0, running.latch)
    assert restored.power_pct > running.config.lehr.heater_min_pct
    assert restored.latch_epoch > held.latch_epoch or restored.latched is False


def test_cycle_excursion_latches_and_resets(running):
    running.cycle(CycleInputs(load=0.8, pull_rate=1.0, gob_mass_g=186.0, dt=1.0))
    excursion = running.evaluate_anneal(600.0)
    assert excursion["latch"]["latched"] is True
    reset = running.reset_lehr()
    assert reset["latch"]["latched"] is False
    assert running.belt.is_normal() is True


def test_curve_is_split_into_heat_soak_cool(platform):
    curve = platform.curve
    heat = curve.setpoint(curve.total_s * 0.03)
    soak = curve.setpoint(curve.total_s * 0.25)
    cool = curve.setpoint(curve.total_s * 0.7)
    done = curve.setpoint(curve.total_s * 2.0)
    assert heat.phase == "heat"
    assert soak.phase == "soak"
    assert cool.phase == "cool"
    assert done.phase == "done"
    assert heat.setpoint_c < soak.setpoint_c
    assert cool.setpoint_c < soak.setpoint_c
    assert set(curve.zone_setpoints(soak.elapsed_s)) == set(
        platform.config.lehr.zone_names()
    )


def test_anneal_zones_track_the_curve(running):
    soak_elapsed = running.curve.total_s * 0.25
    setpoints = running.curve.zone_setpoints(soak_elapsed)
    for _ in range(20):
        running.anneal.apply(setpoints, soak_elapsed)
    assert running.anneal.mean_c() == pytest.approx(
        running.config.temp.anneal_target_c, rel=0.02
    )
    assert running.anneal.cooling_rate_c_per_min() == pytest.approx(0.0, abs=30.0)
    zones = running.anneal.zone_temperatures()
    assert set(zones) == set(running.config.lehr.zone_names())
