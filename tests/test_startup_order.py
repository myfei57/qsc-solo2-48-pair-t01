"""启动顺序：每一步的硬前置条件都必须真实生效。"""

from __future__ import annotations

import pytest

from glassmelt.app import CycleInputs
from glassmelt.errors import InterlockError, SequenceError


def test_startup_sequence_order_is_compliant(platform):
    report = platform.start()
    assert report["violations"] == []
    assert report["topology_missing"] == []
    steps = report["steps"]
    assert steps.index("draft_start") < steps.index("ignite")
    assert steps.index("air_establish") < steps.index("ignite")
    assert steps.index("scale_baseline") < steps.index("mix_plan")
    assert steps.index("stability_warm") < steps.index("gate_open")
    assert steps.index("gob_form") < steps.index("shear") < steps.index("mold_cycle")


def test_start_reports_topology_relations(platform):
    platform.start()
    assert platform.verify_topology() == []
    summary = platform.topology_summary()
    assert "batch" in summary["upstream"]["furnace"]
    assert "forehearth" in summary["downstream"]["furnace"]
    assert platform.topology.requires_before("temp", "lehr")


def test_cycle_refuses_to_run_before_startup(platform):
    with pytest.raises(SequenceError):
        platform.cycle(
            CycleInputs(load=0.9, pull_rate=1.0, gob_mass_g=186.0, dt=1.0)
        )


def test_melt_temperature_must_stay_inside_window(running):
    readiness = running.readiness_report()
    assert readiness["melt_temp_ok"] is True
    assert readiness["melt_temp_low"] is False
    running.furnace_temp.observe(1200.0)
    assert running.furnace_temp.is_ok() is False
    assert running.furnace_temp.is_low() is True
    assert running.furnace_temp.deviation_c() < 0


def test_draft_fan_must_be_negative_for_ignition_path(running):
    assert running.draft.is_negative() is True
    assert running.draft.pressure_pa <= running.config.furnace.draft_target_pa
    assert running.verify_order(running.startup_report()["steps"], ()) == []


def test_cycles_produce_state_and_trends(running):
    result = running.cycle(
        CycleInputs(load=0.8, pull_rate=1.0, gob_mass_g=186.0, dt=1.0)
    )
    assert result["cycle"] == 1
    assert result["forming"]["filled"] is True
    assert result["forming"]["closed"] is True
    assert result["gate"]["open"] is True
    names = running.trend.series_names()
    assert {"melt_temp", "level_mm", "gob_mass_g", "belt_speed"} <= set(names)
    assert running.trend.latest("melt_temp") is not None


def test_level_is_restored_by_charging_when_running_low(running):
    running.level.observe(running.config.furnace.level_target_mm - 30.0)
    before = running.level.level_mm
    outcome = running.start_charging(path="start")
    assert outcome["path"] == "start"
    assert outcome["weighed_kg"] == pytest.approx(
        outcome["batch_kg"] - running.scale.offset_kg
    )
    assert running.level.level_mm > before


def test_charging_is_rejected_when_level_is_high(running):
    running.level.observe(running.config.furnace.level_target_mm + 60.0)
    with pytest.raises(InterlockError):
        running.start_charging(path="start")
    assert running.charger.batch_count == 1
