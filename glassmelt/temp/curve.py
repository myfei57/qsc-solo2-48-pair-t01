"""退火曲线。

退火分三段：升温到退火温度、恒温消除内应力、按曲线降温。加热器按曲线
给定值出力，网带按曲线推进；曲线只描述给定值，不参与闩锁判断。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from ..config import LehrLimits, TempLimits
from ..eventbus import Event, EventBus

AMBIENT_C = 60.0


@dataclass(frozen=True)
class CurveState:
    """退火曲线在某一时刻的状态。"""

    phase: str
    setpoint_c: float
    elapsed_s: float
    progress: float

    def as_dict(self) -> dict:
        return {
            "phase": self.phase,
            "setpoint_c": round(self.setpoint_c, 3),
            "elapsed_s": round(self.elapsed_s, 3),
            "progress": round(self.progress, 4),
        }


class AnnealCurve:
    """升温／恒温／降温三段式退火曲线。"""

    def __init__(self, bus: EventBus, limits: TempLimits, lehr: LehrLimits) -> None:
        self._bus = bus
        self._limits = limits
        self._lehr = lehr
        self._heat_s = max(60.0, limits.soak_seconds / 3.0)
        self._soak_s = max(60.0, limits.soak_seconds)
        self._cool_s = max(120.0, limits.soak_seconds * 2.0)
        self._evaluations = 0
        self._lock = threading.RLock()

    @property
    def total_s(self) -> float:
        """整条曲线的总时长。"""

        return self._heat_s + self._soak_s + self._cool_s

    def setpoint(self, elapsed_s: float) -> CurveState:
        """按已运行时间给出曲线给定值。"""

        elapsed = max(0.0, float(elapsed_s))
        if elapsed < self._heat_s:
            phase = "heat"
            progress = elapsed / self._heat_s
            setpoint = AMBIENT_C + (self._limits.anneal_target_c - AMBIENT_C) * progress
        elif elapsed < self._heat_s + self._soak_s:
            phase = "soak"
            progress = (elapsed - self._heat_s) / self._soak_s
            setpoint = self._limits.anneal_target_c
        elif elapsed < self.total_s:
            phase = "cool"
            progress = (elapsed - self._heat_s - self._soak_s) / self._cool_s
            setpoint = self._limits.anneal_target_c - (
                self._limits.anneal_target_c - AMBIENT_C
            ) * progress
        else:
            phase = "done"
            progress = 1.0
            setpoint = AMBIENT_C
        state = CurveState(
            phase=phase, setpoint_c=setpoint, elapsed_s=elapsed, progress=progress
        )
        with self._lock:
            self._evaluations += 1
        return state

    def zone_setpoints(self, elapsed_s: float) -> dict:
        """把曲线给定值分配到各退火段，返回段名到给定值的映射。"""

        base = self.setpoint(elapsed_s).setpoint_c
        zones = self._lehr.zone_names()
        span = self._limits.anneal_target_c - AMBIENT_C
        result: dict[str, float] = {}
        for index, zone in enumerate(zones):
            share = (index / max(1, len(zones) - 1)) - 0.5
            result[zone] = base + share * span * 0.08
        return result

    def report(self, elapsed_s: float) -> dict:
        """把曲线当前状态写成一条审计事件并返回载荷。"""

        state = self.setpoint(elapsed_s)
        self._bus.publish(
            Event.of(
                "temp",
                "temp",
                "anneal_curve_step",
                subject="anneal_curve",
                phase=state.phase,
                setpoint_c=round(state.setpoint_c, 3),
                elapsed_s=round(elapsed_s, 3),
            )
        )
        return state.as_dict()

    def snapshot(self, elapsed_s: float) -> dict:
        with self._lock:
            evaluations = self._evaluations
        return {
            "segments": {"heat": self._heat_s, "soak": self._soak_s, "cool": self._cool_s},
            "total_s": round(self.total_s, 3),
            "zone_setpoints": self.zone_setpoints(elapsed_s),
            "evaluations": evaluations,
            "state": self.setpoint(elapsed_s).as_dict(),
        }
