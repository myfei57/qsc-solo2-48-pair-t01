"""助燃风。

点火顺序的第一环：助燃风必须先建立起火所需流量，燃气才允许开。助燃风
状态会随着关风、吹扫而变化，所以下游只能拿一份现场评估出来的状态，不能
拿关风或吹扫之前留下的旧状态。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from ..config import BurnerLimits
from ..eventbus import Event, EventBus


@dataclass(frozen=True)
class AirState:
    """一次助燃风状态评估的结果。"""

    flow_m3_h: float
    established: bool
    epoch: int
    evaluated_at: float

    def as_dict(self) -> dict:
        return {
            "flow_m3_h": round(self.flow_m3_h, 2),
            "established": self.established,
            "epoch": self.epoch,
            "evaluated_at": round(self.evaluated_at, 3),
        }


class CombustionAir:
    """助燃风机与风量监测。"""

    def __init__(self, clock, bus: EventBus, limits: BurnerLimits) -> None:
        self._clock = clock
        self._bus = bus
        self._limits = limits
        self._flow_m3_h = 0.0
        self._epoch = 0
        self._last_state: AirState | None = None
        self._starts = 0
        self._closes = 0
        self._lock = threading.RLock()

    @property
    def flow_m3_h(self) -> float:
        with self._lock:
            return self._flow_m3_h

    @property
    def epoch(self) -> int:
        """风路代次；关风或吹扫后加一。"""

        with self._lock:
            return self._epoch

    def establish(self, flow_m3_h: float | None = None) -> AirState:
        """把助燃风建立到点火所需流量，返回现场状态。"""

        target = (
            self._limits.air_min_flow_m3_h * 1.08
            if flow_m3_h is None
            else float(flow_m3_h)
        )
        if not self._limits.air_ok(target):
            raise ValueError(f"助燃风流量 {target:.0f} m3/h 低于点火下限")
        with self._lock:
            self._flow_m3_h = target
            self._starts += 1
        self._bus.publish(
            Event.of(
                "burner",
                "burner",
                "air_established",
                subject="combustion_air",
                flow_m3_h=round(target, 2),
                epoch=self.epoch,
            )
        )
        return self.state()

    def close(self) -> AirState:
        """关助燃风（点火失败的吹扫前也要先关风）。"""

        with self._lock:
            self._flow_m3_h = 0.0
            self._epoch += 1
            self._closes += 1
        self._bus.publish(
            Event.of(
                "burner",
                "burner",
                "air_closed",
                subject="combustion_air",
                severity="notice",
                epoch=self.epoch,
            )
        )
        return self.state()

    def purge(self, purge_flow_m3_h: float = 0.0) -> AirState:
        """吹扫炉膛：按给定吹扫风量置换残余可燃气体。"""

        with self._lock:
            self._flow_m3_h = max(0.0, float(purge_flow_m3_h))
            self._epoch += 1
            flow = self._flow_m3_h
        self._bus.publish(
            Event.of(
                "burner",
                "burner",
                "air_purged",
                subject="combustion_air",
                severity="notice",
                flow_m3_h=round(flow, 2),
                epoch=self.epoch,
            )
        )
        return self.state()

    def state(self) -> AirState:
        """现场评估助燃风状态：始终按当前风量重新判定。"""

        with self._lock:
            state = AirState(
                flow_m3_h=self._flow_m3_h,
                established=self._limits.air_ok(self._flow_m3_h),
                epoch=self._epoch,
                evaluated_at=self._clock.now(),
            )
            self._last_state = state
        return state

    def last_state(self) -> AirState | None:
        """返回最近一次评估结果（仅供展示与审计）。"""

        with self._lock:
            return self._last_state

    def is_established(self) -> bool:
        return self.state().established

    def snapshot(self) -> dict:
        state = self.last_state()
        with self._lock:
            return {
                "flow_m3_h": round(self._flow_m3_h, 2),
                "min_flow_m3_h": self._limits.air_min_flow_m3_h,
                "established": self._limits.air_ok(self._flow_m3_h),
                "epoch": self._epoch,
                "starts": self._starts,
                "closes": self._closes,
                "last_state": state.as_dict() if state else None,
            }
