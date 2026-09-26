"""合模。

合模到位是吹气的硬前置条件。模具重开之后，上一拍的"合模到位"结论必须
作废：合模力、代次都要按当下重新判。吹气侧拿到的是哪一拍的合模结论，
要跟着代次一起落到审计里，事后才能对上"这一炮吹气时模具到底是合着的
还是开着的"。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from ..config import MoldLimits
from ..eventbus import Event, EventBus


@dataclass(frozen=True)
class Closure:
    """一次合模状态判定。"""

    closed: bool
    force_kn: float
    epoch: int
    at: float

    def as_dict(self) -> dict:
        return {
            "closed": self.closed,
            "force_kn": round(self.force_kn, 3),
            "epoch": self.epoch,
            "at": round(self.at, 3),
        }


class MoldCloser:
    """合模机构。"""

    def __init__(self, clock, bus: EventBus, limits: MoldLimits) -> None:
        self._clock = clock
        self._bus = bus
        self._limits = limits
        self._closed = False
        self._force_kn = 0.0
        self._epoch = 0
        self._last_closure: Closure | None = None
        self._closes = 0
        self._opens = 0
        self._lock = threading.RLock()

    def close(self, force_kn: float | None = None) -> Closure:
        """合模，返回现场判定的合模状态。"""

        force = (
            self._limits.close_min_force_kn * 1.12
            if force_kn is None
            else float(force_kn)
        )
        with self._lock:
            self._force_kn = force
            self._closed = self._limits.close_ok(force)
            self._epoch += 1
            self._closes += 1
            closure = self.closure()
        self._bus.publish(
            Event.of(
                "mold",
                "mold",
                "mold_closed" if closure.closed else "mold_close_short",
                subject="mold_closer",
                severity="notice" if closure.closed else "warning",
                force_kn=round(force, 3),
                epoch=closure.epoch,
            )
        )
        return closure

    def open_mold(self, reason: str) -> Closure:
        """脱模或重试时重开模具，返回重开后的合模状态。"""

        with self._lock:
            self._closed = False
            self._force_kn = 0.0
            self._epoch += 1
            self._opens += 1
            closure = self.closure()
        self._bus.publish(
            Event.of(
                "mold",
                "mold",
                "mold_opened",
                subject="mold_closer",
                severity="notice",
                reason=reason,
                epoch=closure.epoch,
            )
        )
        return closure

    def closure(self) -> Closure:
        """现场评估合模状态：始终按当前合模力与代次重新判定。"""

        with self._lock:
            closure = Closure(
                closed=bool(self._closed and self._limits.close_ok(self._force_kn)),
                force_kn=self._force_kn,
                epoch=self._epoch,
                at=self._clock.now(),
            )
            self._last_closure = closure
        return closure

    def last_closure(self) -> Closure | None:
        """返回最近一次合模评估（仅供展示与审计）。"""

        with self._lock:
            return self._last_closure

    def is_closed(self) -> bool:
        """现场判定模具当前是否已合拢到位。"""

        return self.closure().closed

    def counts(self) -> dict:
        with self._lock:
            return {"closes": self._closes, "opens": self._opens, "epoch": self._epoch}

    def snapshot(self) -> dict:
        closure = self.last_closure()
        counts = self.counts()
        with self._lock:
            return {
                "closed": bool(self._closed),
                "force_kn": round(self._force_kn, 3),
                "min_force_kn": self._limits.close_min_force_kn,
                "epoch": counts["epoch"],
                "closes": counts["closes"],
                "opens": counts["opens"],
                "last_closure": closure.as_dict() if closure else None,
            }
