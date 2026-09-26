"""供料道稳温判定。

闸板能否打开，取决于料道温度是否稳定，而不是"温度到过某个值"。稳温的
定义是温度连续落在设定带内达到保持时间；一旦温度漂出带外，稳温计时必须
从头开始，判定结果随之作废——否则一次漂移前后的两个温度会被拼成一段
虚假的稳温记录。下游只认现场重新判出来的结果。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from ..config import ForehearthLimits
from ..eventbus import Event, EventBus


@dataclass(frozen=True)
class StabilityState:
    """一次稳温判定结果。"""

    temperature_c: float
    setpoint_c: float
    stable: bool
    in_band_s: float
    epoch: int
    evaluated_at: float

    def as_dict(self) -> dict:
        return {
            "temperature_c": round(self.temperature_c, 3),
            "setpoint_c": round(self.setpoint_c, 3),
            "stable": self.stable,
            "in_band_s": round(self.in_band_s, 3),
            "epoch": self.epoch,
            "evaluated_at": round(self.evaluated_at, 3),
        }


class StabilityMonitor:
    """按设定带与保持时间判定料道稳温。"""

    def __init__(self, clock, bus: EventBus, limits: ForehearthLimits) -> None:
        self._clock = clock
        self._bus = bus
        self._limits = limits
        self._setpoint_c = limits.gate_min_temp_c + limits.stable_band_c
        self._temperature_c = self._setpoint_c
        self._in_band_since: float | None = None
        self._epoch = 0
        self._last_state: StabilityState | None = None
        self._drifts = 0
        self._lock = threading.RLock()

    @property
    def setpoint_c(self) -> float:
        return self._setpoint_c

    @property
    def temperature_c(self) -> float:
        with self._lock:
            return self._temperature_c

    @property
    def epoch(self) -> int:
        """稳温代次；每次漂移出带外后加一。"""

        with self._lock:
            return self._epoch

    def observe(self, temperature_c: float) -> StabilityState:
        """更新料道温度并现场重新判定稳温。"""

        moment = self._clock.now()
        value = float(temperature_c)
        with self._lock:
            self._temperature_c = value
            in_band = self._limits.within_band(value, self._setpoint_c)
            if in_band:
                if self._in_band_since is None:
                    self._in_band_since = moment
            else:
                if self._in_band_since is not None:
                    self._epoch += 1
                    self._drifts += 1
                self._in_band_since = None
            held = 0.0 if self._in_band_since is None else moment - self._in_band_since
            state = StabilityState(
                temperature_c=value,
                setpoint_c=self._setpoint_c,
                stable=in_band and held >= self._limits.stable_hold_s,
                in_band_s=held,
                epoch=self._epoch,
                evaluated_at=moment,
            )
            self._last_state = state
        self._bus.publish(
            Event.of(
                "temp",
                "temp",
                "stability_evaluated",
                subject="forehearth_temp",
                severity="info" if state.stable else "notice",
                temperature_c=round(value, 3),
                in_band_s=round(state.in_band_s, 3),
                stable=state.stable,
                epoch=state.epoch,
            )
        )
        return state

    def state(self) -> StabilityState:
        """按当前温度重新判定，返回最新结果。"""

        return self.observe(self.temperature_c)

    def last_state(self) -> StabilityState | None:
        """返回最近一次判定结果（仅供展示与审计）。"""

        with self._lock:
            return self._last_state

    def is_stable(self) -> bool:
        """现场重新判定料道是否已稳温。"""

        return self.state().stable

    def reset(self, reason: str) -> int:
        """换料或检修后作废稳温计时，返回新的代次。"""

        with self._lock:
            self._in_band_since = None
            self._epoch += 1
            epoch = self._epoch
        self._bus.publish(
            Event.of(
                "temp",
                "temp",
                "stability_reset",
                subject="forehearth_temp",
                severity="notice",
                reason=reason,
                epoch=epoch,
            )
        )
        return epoch

    def counts(self) -> dict:
        with self._lock:
            return {"drifts": self._drifts, "epoch": self._epoch}

    def snapshot(self) -> dict:
        state = self.last_state()
        counts = self.counts()
        with self._lock:
            return {
                "temperature_c": round(self._temperature_c, 3),
                "setpoint_c": round(self._setpoint_c, 3),
                "band_c": self._limits.stable_band_c,
                "hold_s": self._limits.stable_hold_s,
                "stable": bool(state.stable) if state else False,
                "in_band_s": round(state.in_band_s, 3) if state else 0.0,
                "epoch": counts["epoch"],
                "drifts": counts["drifts"],
            }
