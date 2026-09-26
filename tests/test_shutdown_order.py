"""停炉顺序：助燃风先关，排烟风机后停。"""

from __future__ import annotations

import pytest

from glassmelt.app import CycleInputs
from glassmelt.burner.draft import SHUTDOWN_ORDER, air_closed_before_draft_stop, close_combustion_air
from glassmelt.errors import InterlockError, SequenceError


def test_shutdown_order_is_compliant(platform):
    platform.start()
    report = platform.stop()
    assert report["steps"] == ["gate_close", "air_close", "draft_stop"]
    assert report["violations"] == []
    assert report["draft_running"] is False
    assert report["air_established"] is False


def test_draft_fan_refuses_to_stop_while_air_is_open(platform):
    platform.start()
    assert platform.air.is_established() is True
    with pytest.raises(InterlockError):
        platform.draft.stop(platform.air.state())
    assert platform.draft.is_running is True


def test_close_helper_returns_the_two_shutdown_steps(platform):
    platform.start()
    payload = close_combustion_air(platform.air, platform.draft, platform.bus)
    assert tuple(payload["steps"]) == SHUTDOWN_ORDER
    assert payload["air"]["established"] is False
    assert payload["draft"]["running"] is False
    assert air_closed_before_draft_stop(platform.bus) == []


def test_cycle_is_refused_after_shutdown(platform):
    platform.start()
    platform.stop()
    with pytest.raises(SequenceError):
        platform.cycle(
            CycleInputs(load=0.7, pull_rate=0.9, gob_mass_g=186.0, dt=1.0)
        )


def test_line_can_be_restarted_after_shutdown(platform):
    platform.start()
    platform.stop()
    second = platform.start()
    assert second["violations"] == []
    assert platform.igniter.is_burning() is True
    assert platform.gate.is_open() is True
    assert platform.draft.is_negative() is True


def test_gate_close_returns_previous_opening(platform):
    platform.start()
    previous = platform.gate.close("停炉")
    assert previous == pytest.approx(platform.config.forehearth.gate_open_pct)
    assert platform.gate.is_open() is False
    assert platform.gate.snapshot()["open"] is False
