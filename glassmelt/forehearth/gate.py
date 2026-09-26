"""供料道闸板。

闸板开启的硬前置条件是料道稳温：不管是初次升温还是停料后的再升温，两条
路径都必须现场取一次稳温结论。开闸板前不再复核稳定性，料滴就会在温度还
没稳的时候流出来，整批成型温度偏低。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from ..config import ForehearthLimits
from ..errors import InterlockError
from ..eventbus import Event, EventBus


@dataclass(frozen=True)
class GateOutcome:
    """一次开闸板动作的结果。"""

    path: str
    opening_pct: float
    temperature_c: float
    stability_epoch: int
    stable: bool
    at: float

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "opening_pct": round(self.opening_pct, 2),
            "temperature_c": round(self.temperature_c, 3),
            "stability_epoch": self.stability_epoch,
            "stable": self.stable,
            "at": round(self.at, 3),
        }


class ForehearthGate:
    """供料道闸板本体。"""

    def __init__(self, bus: EventBus, limits: ForehearthLimits) -> None:
        self._bus = bus
        self._limits = limits
        self._opening_pct = 0.0
        self._opens = 0
        self._rejections = 0
        self._history: list[dict] = []
        self._lock = threading.RLock()

    @property
    def opening_pct(self) -> float:
        with self._lock:
            return self._opening_pct

    def is_open(self) -> bool:
        return self.opening_pct > 0.0

    def warm_up(self, monitor) -> GateOutcome:
        """初次升温后开闸板。"""

        return self._open(monitor, path="warm_up")

    def reheat(self, monitor) -> GateOutcome:
        """停料再升温后开闸板：与初次升温走同一道稳温复核。"""

        return self._open(monitor, path="reheat")

    def _open(self, monitor, path: str) -> GateOutcome:
        if not monitor.is_stable():
            stability = monitor.last_state() or monitor.state()
            return self._reject(path, stability, reason="温度尚未稳定")
        stability = monitor.last_state() or monitor.state()
        if not self._limits.temperature_ok(stability.temperature_c):
            return self._reject(path, stability, reason="料道温度低于开闸下限")
        with self._lock:
            self._opening_pct = self._limits.gate_open_pct
            self._opens += 1
            outcome = GateOutcome(
                path=path,
                opening_pct=self._opening_pct,
                temperature_c=stability.temperature_c,
                stability_epoch=stability.epoch,
                stable=stability.stable,
                at=stability.evaluated_at,
            )
            self._history.append(outcome.as_dict())
        self._bus.publish(
            Event.of(
                "forehearth",
                "forehearth",
                "gate_opened",
                subject="forehearth_gate",
                severity="notice",
                path=path,
                opening_pct=round(self._opening_pct, 2),
                temperature_c=round(stability.temperature_c, 3),
                stability_epoch=stability.epoch,
            )
        )
        return outcome

    def _reject(self, path: str, stability, reason: str) -> GateOutcome:
        with self._lock:
            self._opening_pct = 0.0
            self._rejections += 1
        self._bus.publish(
            Event.of(
                "forehearth",
                "forehearth",
                "gate_rejected",
                subject="forehearth_gate",
                severity="warning",
                path=path,
                reason=reason,
                temperature_c=round(stability.temperature_c, 3),
                stable=stability.stable,
                stability_epoch=stability.epoch,
            )
        )
        raise InterlockError(
            f"{reason}（{stability.temperature_c:.1f} °C，稳温代次 {stability.epoch}），禁止开闸板",
            component="forehearth",
        )

    def close(self, reason: str) -> float:
        """关闸板，返回关闸板前的开度。"""

        with self._lock:
            previous = self._opening_pct
            self._opening_pct = 0.0
        self._bus.publish(
            Event.of(
                "forehearth",
                "forehearth",
                "gate_closed",
                subject="forehearth_gate",
                severity="notice",
                reason=reason,
            )
        )
        return previous

    def counts(self) -> dict:
        with self._lock:
            return {"opens": self._opens, "rejections": self._rejections}

    def history(self, limit: int = 5) -> list[dict]:
        with self._lock:
            return self._history[-limit:]

    def snapshot(self) -> dict:
        counts = self.counts()
        with self._lock:
            return {
                "opening_pct": round(self._opening_pct, 2),
                "open": self._opening_pct > 0.0,
                "min_temp_c": self._limits.gate_min_temp_c,
                "opens": counts["opens"],
                "rejections": counts["rejections"],
                "recent": list(self._history[-3:]),
            }
