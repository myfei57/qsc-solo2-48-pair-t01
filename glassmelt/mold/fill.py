"""灌模。

模具灌得满不满，取决于落进去的料滴有没有成型到位。灌模侧同样按这一颗
料滴现场判定，不能拿上一颗的成型结论接着用；料滴偏短时模具灌不满，成型
后瓶口出现瘪口。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from ..config import FeederLimits
from ..errors import InterlockError
from ..eventbus import Event, EventBus


@dataclass(frozen=True)
class FillOutcome:
    """一次灌模结果。"""

    gob_index: int
    mass_g: float
    short_g: float
    filled: bool
    gob_epoch: int
    at: float

    def as_dict(self) -> dict:
        return {
            "gob_index": self.gob_index,
            "mass_g": round(self.mass_g, 3),
            "short_g": round(self.short_g, 3),
            "filled": self.filled,
            "gob_epoch": self.gob_epoch,
            "at": round(self.at, 3),
        }


class MoldFill:
    """灌模动作。"""

    def __init__(self, bus: EventBus, limits: FeederLimits) -> None:
        self._bus = bus
        self._limits = limits
        self._fills = 0
        self._short = 0
        self._history: list[dict] = []
        self._lock = threading.RLock()

    def fill(self, gob) -> FillOutcome:
        """按这一颗料滴的成型判定执行灌模。"""

        if not gob.is_formed():
            with self._lock:
                self._short += 1
            self._bus.publish(
                Event.of(
                    "mold",
                    "mold",
                    "fill_short",
                    subject="mold_fill",
                    severity="warning",
                    gob_index=gob.index,
                    gob_epoch=gob.epoch,
                    mass_g=round(gob.mass_g, 3),
                )
            )
            raise InterlockError(
                f"第 {gob.index} 颗料滴偏短（{gob.mass_g:.1f} g），禁止灌模",
                component="mold",
            )
        short_g = max(0.0, self._limits.gob_mass_target_g - gob.mass_g)
        with self._lock:
            self._fills += 1
            outcome = FillOutcome(
                gob_index=gob.index,
                mass_g=gob.mass_g,
                short_g=short_g,
                filled=True,
                gob_epoch=gob.epoch,
                at=gob.at,
            )
            self._history.append(outcome.as_dict())
            if len(self._history) > 200:
                del self._history[: len(self._history) - 200]
        self._bus.publish(
            Event.of(
                "mold",
                "mold",
                "filled",
                subject="mold_fill",
                severity="notice",
                gob_index=gob.index,
                gob_epoch=gob.epoch,
                mass_g=round(gob.mass_g, 3),
            )
        )
        return outcome

    def counts(self) -> dict:
        with self._lock:
            return {"fills": self._fills, "short": self._short}

    def history(self, limit: int = 5) -> list[dict]:
        with self._lock:
            return self._history[-limit:]

    def snapshot(self) -> dict:
        counts = self.counts()
        with self._lock:
            return {
                "fills": counts["fills"],
                "short": counts["short"],
                "target_g": self._limits.gob_mass_target_g,
                "tolerance_g": self._limits.gob_mass_tolerance_g,
                "recent": list(self._history[-3:]),
            }
