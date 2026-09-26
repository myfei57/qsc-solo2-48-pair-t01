"""剪切。

剪切刀有正常投料与恢复两条路径：正常料滴成型后剪切，恢复时补切一颗。
两条路径都要现场确认"这一颗料滴已成型"，剪切窗口只在成型之后打开。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from ..config import FeederLimits
from ..errors import InterlockError
from ..eventbus import Event, EventBus


@dataclass(frozen=True)
class CutOutcome:
    """一次剪切结果。"""

    path: str
    gob_index: int
    gob_mass_g: float
    cut_window_ms: float
    gob_epoch: int
    at: float

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "gob_index": self.gob_index,
            "gob_mass_g": round(self.gob_mass_g, 3),
            "cut_window_ms": round(self.cut_window_ms, 2),
            "gob_epoch": self.gob_epoch,
            "at": round(self.at, 3),
        }


class Shear:
    """剪切刀本体。"""

    def __init__(self, bus: EventBus, limits: FeederLimits) -> None:
        self._bus = bus
        self._limits = limits
        self._cuts = 0
        self._early = 0
        self._history: list[dict] = []
        self._lock = threading.RLock()

    def feed(self, gob) -> CutOutcome:
        """正常投料路径：料滴成型后剪切。"""

        return self._cut(gob, path="feed")

    def recover(self, gob) -> CutOutcome:
        """恢复路径：补切一颗，同样要求料滴已成型。"""

        return self._cut(gob, path="recover")

    def _cut(self, gob, path: str) -> CutOutcome:
        if not gob.is_formed():
            with self._lock:
                self._early += 1
            self._bus.publish(
                Event.of(
                    "feeder",
                    "feeder",
                    "cut_early",
                    subject="shear",
                    severity="warning",
                    path=path,
                    gob_index=gob.index,
                    gob_mass_g=round(gob.mass_g, 3),
                    gob_epoch=gob.epoch,
                )
            )
            raise InterlockError(
                f"第 {gob.index} 颗料滴（{gob.mass_g:.1f} g）尚未成型到位，禁止剪切",
                component="feeder",
            )
        with self._lock:
            self._cuts += 1
            outcome = CutOutcome(
                path=path,
                gob_index=gob.index,
                gob_mass_g=gob.mass_g,
                cut_window_ms=self._limits.cut_window_ms,
                gob_epoch=gob.epoch,
                at=gob.at,
            )
            self._history.append(outcome.as_dict())
            if len(self._history) > 200:
                del self._history[: len(self._history) - 200]
        self._bus.publish(
            Event.of(
                "feeder",
                "feeder",
                "cut",
                subject="shear",
                severity="notice",
                path=path,
                gob_index=gob.index,
                gob_mass_g=round(gob.mass_g, 3),
                gob_epoch=gob.epoch,
            )
        )
        return outcome

    def counts(self) -> dict:
        with self._lock:
            return {"cuts": self._cuts, "early": self._early}

    def history(self, limit: int = 5) -> list[dict]:
        with self._lock:
            return self._history[-limit:]

    def snapshot(self) -> dict:
        counts = self.counts()
        with self._lock:
            return {
                "cuts": counts["cuts"],
                "early": counts["early"],
                "window_ms": self._limits.cut_window_ms,
                "recent": list(self._history[-3:]),
            }
