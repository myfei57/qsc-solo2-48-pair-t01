"""吹气。

吹气必须在合模到位之后：正常循环与补料重试两条路径都要现场核对合模状态。
吹气侧只接受一份当拍的合模结论，重开模之前的结论一律不能用。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from ..config import MoldLimits
from ..errors import InterlockError
from ..eventbus import Event, EventBus


@dataclass(frozen=True)
class BlowOutcome:
    """一次吹气结果。"""

    path: str
    blown_ms: float
    pressure_kpa: float
    closure_epoch: int
    at: float

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "blown_ms": round(self.blown_ms, 2),
            "pressure_kpa": round(self.pressure_kpa, 2),
            "closure_epoch": self.closure_epoch,
            "at": round(self.at, 3),
        }


class BlowStation:
    """吹气站本体。"""

    def __init__(self, bus: EventBus, limits: MoldLimits) -> None:
        self._bus = bus
        self._limits = limits
        self._blows = 0
        self._early = 0
        self._history: list[dict] = []
        self._lock = threading.RLock()

    def normal(self, closure) -> BlowOutcome:
        """正常循环吹气。"""

        return self._blow(closure, path="cycle")

    def retry(self, closure, reason: str) -> BlowOutcome:
        """补料重试吹气：与正常循环走同一道合模核对。"""

        return self._blow(closure, path=f"retry:{reason}")

    def _blow(self, closure, path: str) -> BlowOutcome:
        if not closure.closed:
            with self._lock:
                self._early += 1
            self._bus.publish(
                Event.of(
                    "mold",
                    "mold",
                    "blow_early",
                    subject="blow_station",
                    severity="alarm",
                    path=path,
                    closure_epoch=closure.epoch,
                    force_kn=round(closure.force_kn, 3),
                )
            )
            raise InterlockError(
                f"合模未到位（代次 {closure.epoch}，合模力 {closure.force_kn:.1f} kN），禁止吹气",
                component="mold",
            )
        with self._lock:
            self._blows += 1
            outcome = BlowOutcome(
                path=path,
                blown_ms=self._limits.blow_min_ms * 1.1,
                pressure_kpa=self._limits.blow_pressure_kpa,
                closure_epoch=closure.epoch,
                at=closure.at,
            )
            self._history.append(outcome.as_dict())
            if len(self._history) > 200:
                del self._history[: len(self._history) - 200]
        self._bus.publish(
            Event.of(
                "mold",
                "mold",
                "blown",
                subject="blow_station",
                severity="notice",
                path=path,
                pressure_kpa=round(outcome.pressure_kpa, 2),
                closure_epoch=closure.epoch,
            )
        )
        return outcome

    def counts(self) -> dict:
        with self._lock:
            return {"blows": self._blows, "early": self._early}

    def history(self, limit: int = 5) -> list[dict]:
        with self._lock:
            return self._history[-limit:]

    def snapshot(self) -> dict:
        counts = self.counts()
        with self._lock:
            return {
                "blows": counts["blows"],
                "early": counts["early"],
                "pressure_kpa": self._limits.blow_pressure_kpa,
                "min_ms": self._limits.blow_min_ms,
                "recent": list(self._history[-3:]),
            }
