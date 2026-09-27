"""氧量校正：配比自收敛、修正有界、测点不可信时退回固定配比并告警。"""

from __future__ import annotations

import copy
import json
import urllib.error
import urllib.request

import pytest

from glassmelt.app import CycleInputs
from glassmelt.burner.o2trim import O2TrimController, TrimMode
from glassmelt.clock import ManualClock
from glassmelt.config import O2TrimLimits, PlatformConfig, config_to_mapping, default_config
from glassmelt.console.server import ConsoleServer
from glassmelt.errors import ConfigError
from glassmelt.eventbus import EventBus


def _cycle(platform, cycles: int, **kwargs) -> dict:
    inputs = dict(load=0.85, pull_rate=1.0, gob_mass_g=186.0, dt=2.0)
    inputs.update(kwargs)
    result = {}
    for _ in range(cycles):
        result = platform.cycle(CycleInputs(**inputs))
    return result


def _get(url: str) -> tuple[int, dict]:
    with urllib.request.urlopen(url, timeout=5.0) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def _post(url: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload or {}).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=5.0) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def test_trim_converges_to_setpoint_despite_disturbance(running):
    result = _cycle(running, 150, o2_bias_pct=0.8)
    trim = result["o2_trim"]
    assert trim["mode"] == "trimming"
    assert trim["sensor_ok"] is True
    assert trim["o2_pct"] == pytest.approx(
        running.config.o2trim.o2_setpoint_pct, abs=0.2
    )
    # 正偏差（漏风）要求把风量往下修，修正量收敛到有界区间内的负值
    assert -6.5 < trim["trim_pct"] < -3.0
    assert abs(trim["trim_pct"]) <= running.config.o2trim.trim_max_pct
    assert "flue_o2_pct" in running.trend.series_names()
    assert "air_fuel_ratio" in running.trend.series_names()


def test_trim_authority_is_bounded(running):
    result = _cycle(running, 150, o2_bias_pct=4.0)
    trim = result["o2_trim"]
    assert trim["mode"] == "trimming"
    assert trim["trim_pct"] == pytest.approx(-running.config.o2trim.trim_max_pct)
    assert trim["ratio"] == pytest.approx(
        running.config.o2trim.ratio_base * 0.92, rel=1e-3
    )


def test_out_of_range_sensor_falls_back_to_base_ratio(running):
    result = _cycle(running, 25, dt=1.0, o2_sensor_pct=25.0)
    trim = result["o2_trim"]
    assert trim["mode"] == "fallback"
    assert trim["sensor_ok"] is False
    assert trim["invalid_reason"] == "range"
    assert trim["trim_pct"] == 0.0
    assert trim["ratio"] == pytest.approx(running.config.o2trim.ratio_base)
    gas_flow = running.gas.flow_m3_h
    assert running.air.flow_m3_h == pytest.approx(
        gas_flow * running.config.o2trim.ratio_base
    )
    alarms = running.audit.alarms()
    assert any(item["action"] == "o2_trim_fallback" for item in alarms)
    assert any(item["action"] == "o2_sensor_suspect" for item in alarms)


def test_frozen_sensor_is_detected_and_falls_back(running):
    result = _cycle(running, 30, dt=1.0, o2_sensor_pct=3.0)
    trim = result["o2_trim"]
    assert trim["mode"] == "fallback"
    assert trim["invalid_reason"] == "frozen"
    fallback = [
        item for item in running.audit.alarms() if item["action"] == "o2_trim_fallback"
    ]
    assert fallback
    assert fallback[-1]["detail"]["reason"] == "frozen"


def test_stale_sensor_falls_back_after_grace_period():
    limits = default_config().o2trim
    bus = EventBus()
    controller = O2TrimController(ManualClock(), bus, limits, air_min_flow_m3_h=5200.0)
    controller.enable(at=0.0)
    assert controller.observe(3.5, at=1.0) is True
    state = controller.update(578.0, 1.0, at=1.0)
    assert state.mode is TrimMode.TRIMMING
    assert state.sensor_ok is True

    state = controller.update(578.0, 1.0, at=17.0)
    assert state.sensor_ok is False
    assert state.invalid_reason == "stale"
    assert state.mode is TrimMode.TRIMMING  # 宽限期内只冻结积分，不回退

    state = controller.update(578.0, 1.0, at=37.5)
    assert state.mode is TrimMode.FALLBACK
    assert state.trim_pct == 0.0
    assert state.ratio == pytest.approx(limits.ratio_base)
    assert state.air_setpoint_m3_h == pytest.approx(578.0 * limits.ratio_base)
    actions = [event.action for event in bus.history("burner")]
    assert "o2_sensor_suspect" in actions
    assert "o2_trim_fallback" in actions


def test_trim_recovers_and_reconverges_after_sensor_returns():
    limits = default_config().o2trim
    bus = EventBus()
    controller = O2TrimController(ManualClock(), bus, limits, air_min_flow_m3_h=5200.0)
    controller.enable(at=0.0)
    controller.observe(3.5, at=1.0)
    controller.update(578.0, 1.0, at=1.0)
    controller.update(578.0, 1.0, at=17.0)
    state = controller.update(578.0, 1.0, at=37.5)
    assert state.mode is TrimMode.FALLBACK

    controller.observe(3.2, at=38.0)
    state = controller.update(578.0, 1.0, at=38.0)
    assert state.mode is TrimMode.TRIMMING
    assert state.sensor_ok is True
    assert controller.snapshot()["recoveries"] == 1
    actions = [event.action for event in bus.history("burner")]
    assert "o2_trim_recovered" in actions


def test_air_never_drops_below_safe_ratio():
    limits = O2TrimLimits(
        o2_setpoint_pct=3.0,
        o2_min_plausible_pct=0.2,
        o2_max_plausible_pct=16.0,
        ratio_base=10.2,
        ratio_min_safe=9.9,
        trim_max_pct=8.0,
        trim_rate_per_s=0.001,
        sensor_stale_after_s=15.0,
        sensor_freeze_samples=6,
        sensor_freeze_eps_pct=0.02,
        fallback_grace_s=20.0,
    )
    controller = O2TrimController(ManualClock(), EventBus(), limits, air_min_flow_m3_h=5200.0)
    controller.enable(at=0.0)
    state = None
    for index in range(40):
        # 含氧量一直偏高，校正拼命减风；风量不许跌破安全配比下限
        controller.observe(8.0 + 0.1 * (index % 3), at=float(index + 1))
        state = controller.update(578.0, 1.0, at=float(index + 1))
        assert state.air_setpoint_m3_h >= 578.0 * limits.ratio_min_safe - 1e-6
    assert state is not None
    assert state.ratio == pytest.approx(limits.ratio_min_safe, abs=0.1)


def test_trim_disabled_until_ignition_and_after_stop(platform):
    assert platform.o2trim.snapshot()["mode"] == "disabled"
    platform.start()
    assert platform.o2trim.snapshot()["mode"] == "trimming"
    platform.stop()
    assert platform.o2trim.snapshot()["mode"] == "disabled"


def test_o2trim_config_section_is_required():
    mapping = config_to_mapping(default_config())
    broken = copy.deepcopy(mapping)
    del broken["limits"]["o2trim"]
    with pytest.raises(ConfigError):
        PlatformConfig.from_mapping(broken)


def test_o2trim_config_rejects_unsafe_values():
    mapping = config_to_mapping(default_config())
    out_of_band = copy.deepcopy(mapping)
    out_of_band["limits"]["o2trim"]["o2_setpoint_pct"] = 20.0
    with pytest.raises(ConfigError):
        PlatformConfig.from_mapping(out_of_band)
    unsafe_floor = copy.deepcopy(mapping)
    unsafe_floor["limits"]["o2trim"]["ratio_min_safe"] = 12.0
    with pytest.raises(ConfigError):
        PlatformConfig.from_mapping(unsafe_floor)
    no_authority = copy.deepcopy(mapping)
    no_authority["limits"]["o2trim"]["trim_max_pct"] = 0.0
    with pytest.raises(ConfigError):
        PlatformConfig.from_mapping(no_authority)


def test_o2trim_console_endpoint(platform):
    server = ConsoleServer(platform)
    base = server.start()
    try:
        status, _started = _post(f"{base}/api/start")
        assert status == 200
        status, payload = _get(f"{base}/api/burner/o2trim")
        assert status == 200
        assert payload["mode"] == "trimming"
        assert payload["ratio_base"] == pytest.approx(11.3)
        status, state = _get(f"{base}/api/state")
        assert status == 200
        assert state["o2trim"]["trim_max_pct"] == pytest.approx(8.0)
    finally:
        server.stop()
