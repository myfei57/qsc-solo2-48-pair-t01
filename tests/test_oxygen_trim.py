"""含氧量空燃比修正：慢收敛、测点退回、交叉限幅与配置校验。"""

from __future__ import annotations

import pytest

from glassmelt.app import CycleInputs, build_platform
from glassmelt.burner.crosslimit import CrossLimiter
from glassmelt.burner.o2probe import OxygenProbe
from glassmelt.burner.trim import OxygenTrim, TrimMode
from glassmelt.config import OxygenTrimLimits, default_config
from glassmelt.errors import ConfigError
from glassmelt.eventbus import EventBus


def _components(clock):
    """绕开平台直接装配测点与修正器，便于逐拍驱动。"""

    bus = EventBus()
    limits = default_config().oxygen
    probe = OxygenProbe(clock, bus, limits)
    trim = OxygenTrim(clock, bus, limits)
    trim.enable()
    return bus, limits, probe, trim


def _wobble(index: int, base: float = 3.0) -> float:
    """给测点喂带小幅波动的读数，避免被冻值判据误伤。"""

    return base + 0.1 * (1 if index % 2 == 0 else -1)


def _feed(clock, probe, trim, raw_pct, load=0.8, dt=1.0):
    clock.advance(dt)
    reading = probe.evaluate(raw_pct, dt)
    return trim.step(reading, load, dt), reading


def test_trim_converges_oxygen_back_to_target(running):
    """表计偏差把残氧顶高后，修正系数应缓慢把配比收回到目标死区内。"""

    limits = running.config.oxygen
    for _ in range(1200):
        running.cycle(CycleInputs(load=0.8, pull_rate=0.8, gob_mass_g=186.0, dt=1.0))
    status = running.status()["oxygen"]
    assert status["trim"]["mode"] == TrimMode.AUTO.value
    trim = status["trim"]["trim"]
    assert limits.trim_min <= trim <= limits.trim_max
    # 偏差被收掉：滤波后的残氧回到目标死区附近，系数离开了 1.0。
    assert abs(status["probe"]["value_pct"] - limits.o2_target_pct) <= (
        limits.o2_deadband_pct + 0.2
    )
    assert trim < 1.0


def test_trim_moves_slowly_and_stays_bounded(running):
    """每一拍系数变化不得越率，且全程不越界——慢慢收，不猛调。"""

    limits = running.config.oxygen
    previous = running.oxygen_trim.trim
    for _ in range(300):
        running.cycle(CycleInputs(load=0.8, pull_rate=0.8, gob_mass_g=186.0, dt=1.0))
        current = running.oxygen_trim.trim
        max_step = limits.slew_up_pct_per_min / 100.0 / 60.0 + 1e-9
        assert abs(current - previous) <= max_step
        assert limits.trim_min <= current <= limits.trim_max
        previous = current


def test_out_of_range_reading_falls_back_to_fixed_ratio(clock):
    """测点硬坏值：先冻结，超时退回固定配比并报警提示。"""

    bus, limits, probe, trim = _components(clock)
    for index in range(5):
        output, _ = _feed(clock, probe, trim, _wobble(index))
    assert trim.mode is TrimMode.AUTO

    output = None
    for _ in range(int(limits.fixed_after_s) + 5):
        output, reading = _feed(clock, probe, trim, 15.0)
    assert reading.credible is False
    assert "out_of_range" in reading.reasons
    assert trim.mode is TrimMode.FIXED
    assert output.o2_credible is False
    # 退回后系数收向安全系数，有效配比就是固定配比。
    for _ in range(400):
        output, _ = _feed(clock, probe, trim, 15.0)
    assert abs(trim.trim - limits.trim_safe) < 1e-9
    assert output.ratio == pytest.approx(limits.safe_ratio(), abs=1e-6)

    fallback_events = [
        event
        for event in bus.history("burner")
        if event.action == "trim_mode_changed"
        and event.payload.get("to_mode") == TrimMode.FIXED.value
    ]
    assert fallback_events, "退回固定配比必须有事件提示"
    assert fallback_events[0].payload["severity"] == "alarm"
    assert trim.snapshot()["fallbacks"] == 1


def test_frozen_reading_is_rejected(clock):
    """读数一动不动超过确认时间，按冻值判不可信。"""

    _, limits, probe, trim = _components(clock)
    reading = None
    for _ in range(int(limits.o2_freeze_hold_s) + 5):
        clock.advance(1.0)
        reading = probe.evaluate(3.0, 1.0)
    assert "frozen" in reading.reasons
    assert reading.credible is False


def test_spike_is_rejected_without_fallback(clock):
    """单拍尖峰被剔除且不污染滤波值，也不应触发退回。"""

    _, limits, probe, trim = _components(clock)
    for index in range(10):
        _feed(clock, probe, trim, _wobble(index))
    assert trim.mode is TrimMode.AUTO
    filtered_before = probe.value_pct

    _, spike = _feed(clock, probe, trim, 9.5)
    assert spike.credible is False
    assert "spike" in spike.reasons
    assert abs(probe.value_pct - filtered_before) < 0.2

    # 尖峰后第一拍仍按跳变剔除，第二拍恢复可信，全程保持 AUTO。
    _feed(clock, probe, trim, 3.0)
    _, recovered = _feed(clock, probe, trim, 3.05)
    assert recovered.credible is True
    assert trim.mode is TrimMode.AUTO


def test_recovery_requires_sustained_credible_readings(clock):
    """从固定配比恢复必须连续可信满确认时间，且系数无扰衔接。"""

    _, limits, probe, trim = _components(clock)
    for _ in range(int(limits.fixed_after_s) + 5):
        _feed(clock, probe, trim, 15.0)
    assert trim.mode is TrimMode.FIXED

    index = 0
    for _ in range(int(limits.recover_require_s) - 10):
        _feed(clock, probe, trim, _wobble(index))
        index += 1
    assert trim.mode is TrimMode.FIXED, "确认时间未满不得提前闭环"

    trim_before = trim.trim
    for _ in range(20):
        _feed(clock, probe, trim, _wobble(index))
        index += 1
    assert trim.mode is TrimMode.AUTO
    # 无扰衔接：恢复瞬间系数不出现跳变。
    assert abs(trim.trim - trim_before) <= 0.01
    assert trim.snapshot()["recoveries"] == 1


def test_low_load_freezes_trim(clock):
    """低负荷残氧代表性差，冻结系数不闭环。"""

    _, limits, probe, trim = _components(clock)
    for index in range(5):
        _feed(clock, probe, trim, _wobble(index))
    assert trim.mode is TrimMode.AUTO

    output, _ = _feed(clock, probe, trim, _wobble(6), load=limits.o2_min_load - 0.1)
    assert trim.mode is TrimMode.HOLD
    frozen = trim.trim
    for index in range(7, 15):
        output, _ = _feed(clock, probe, trim, _wobble(index), load=0.2)
    assert trim.trim == frozen
    assert output.integrating is False


def test_manual_fallback_matches_timeout_path(running):
    """人工退回与测点超时退回走同一条路径。"""

    result = running.fallback_oxygen_trim("测试人工退回")
    assert result["trim"]["mode"] == TrimMode.FIXED.value
    assert running.oxygen_trim.mode is TrimMode.FIXED


def test_calibration_invalidate_marks_reading_untrusted(running):
    """标定期间本拍读数直接作废。"""

    reading = running.calibrate_o2_probe("测试标定")
    assert reading["credible"] is False
    assert "测试标定" in reading["reasons"]


def test_cross_limiter_air_leads_on_load_up():
    """加负荷：燃气需求被当前风量卡住，风先加气后加。"""

    limits = default_config().oxygen
    limiter = CrossLimiter(EventBus(), limits, gas_max_m3_h=620.0)
    demand = limiter.demands(
        gas_demand_m3_h=400.0, air_flow_m3_h=2000.0, gas_flow_m3_h=100.0, trim=1.0
    )
    assert demand.gas_limited is True
    assert demand.gas_m3_h < 400.0
    assert demand.air_m3_h == pytest.approx(400.0 * limits.ratio_at(1.0))


def test_cross_limiter_gas_leads_on_load_down():
    """减负荷：风量需求被当前燃气量托住，气先减风后减。"""

    limits = default_config().oxygen
    limiter = CrossLimiter(EventBus(), limits, gas_max_m3_h=620.0)
    demand = limiter.demands(
        gas_demand_m3_h=100.0, air_flow_m3_h=5750.0, gas_flow_m3_h=500.0, trim=1.0
    )
    assert demand.air_limited is True
    assert demand.air_m3_h > 100.0 * limits.ratio_at(1.0)
    assert demand.gas_m3_h == pytest.approx(100.0)


def test_cross_limiter_releases_at_steady_state():
    """稳态时两侧都不应被自己的保护边界卡住。"""

    limits = default_config().oxygen
    limiter = CrossLimiter(EventBus(), limits, gas_max_m3_h=620.0)
    ratio = limits.ratio_at(1.0)
    demand = limiter.demands(
        gas_demand_m3_h=300.0,
        air_flow_m3_h=300.0 * ratio,
        gas_flow_m3_h=300.0,
        trim=1.0,
    )
    assert demand.gas_limited is False
    assert demand.air_limited is False
    assert demand.gas_m3_h == pytest.approx(300.0)
    assert demand.air_m3_h == pytest.approx(300.0 * ratio)


def test_cycle_reports_combustion_status(running):
    """控制周期必须带回燃烧结算，状态汇总必须含含氧量回路。"""

    result = None
    for _ in range(5):
        result = running.cycle(
            CycleInputs(load=0.7, pull_rate=0.8, gob_mass_g=186.0, dt=1.0)
        )
    combustion = result["combustion"]
    assert combustion["demand"]["air_m3_h"] > 0
    assert combustion["demand"]["gas_m3_h"] > 0
    oxygen = running.status()["oxygen"]
    assert oxygen["probe"]["credible"] is True
    assert oxygen["trim"]["mode"] in {mode.value for mode in TrimMode}


def test_oxygen_config_rejects_unreachable_target():
    """含氧量目标超出修正行程可达范围时，装配阶段必须拒绝。"""

    section = {
        "ratio_air_per_gas": 10.0,
        "excess_air_base": 1.15,
        "trim_min": 0.92,
        "trim_max": 1.08,
        "trim_safe": 1.0,
        "o2_target_pct": 9.0,
        "o2_deadband_pct": 0.3,
        "o2_low_load_target_bias_pct": 0.8,
        "o2_min_load": 0.4,
        "o2_valid_min_pct": 0.5,
        "o2_valid_max_pct": 12.0,
        "o2_slew_pct_per_s": 1.0,
        "o2_freeze_noise_pct": 0.05,
        "o2_freeze_hold_s": 30.0,
        "filter_tau_s": 8.0,
        "integral_per_min": 0.5,
        "slew_up_pct_per_min": 2.0,
        "slew_down_pct_per_min": 1.0,
        "hold_enter_s": 20.0,
        "fixed_after_s": 90.0,
        "recover_require_s": 60.0,
        "plausibility_tolerance_pct": 1.5,
        "cross_limiter_bias_pct": 1.0,
        "air_min_operating_m3_h": 2000.0,
    }
    with pytest.raises(ConfigError):
        OxygenTrimLimits.from_mapping(section)
    section["o2_target_pct"] = 3.0
    section["excess_air_base"] = 1.0
    with pytest.raises(ConfigError):
        OxygenTrimLimits.from_mapping(section)
