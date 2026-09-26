"""审计留存、事件分发与交付树死代码门禁。"""

from __future__ import annotations

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_alarm_entries_are_captured(running):
    running.evaluate_anneal(running.config.temp.anneal_alarm_c + 6.0)
    alarms = running.audit.alarms()
    assert alarms
    assert any(item["severity"] == "alarm" for item in alarms)
    assert any(item["subject"] == "anneal_temp" for item in alarms)


def test_events_are_published_to_subscribers(platform):
    seen: list[str] = []
    unsubscribe = platform.bus.subscribe("batch", lambda event: seen.append(event.action))
    platform.level.observe(platform.config.furnace.level_target_mm)
    platform.start_charging(path="start")
    assert "charged" in seen
    unsubscribe()
    assert platform.bus.history("batch")
    assert platform.bus.history()


def test_ignition_history_captures_both_paths(platform):
    platform.ignite_burner(platform.furnace_temp.target_c + 8.0)
    platform.reignite_burner(platform.furnace_temp.target_c + 9.0, "失焰")
    history = platform.igniter.history()
    assert history[0] == "idle"
    assert "flame" in history
    assert platform.igniter.snapshot()["starts"] == 2


def test_deadcode_gate_reports_no_unused_symbols():
    result = subprocess.run(
        [sys.executable, os.path.join(ROOT, "tools", "deadcode.py"), os.path.join(ROOT, "glassmelt")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["unused_symbols"] == []
    assert report["unreachable_modules"] == []
    assert report["files"] >= 50
    assert report["symbols"] >= 200
