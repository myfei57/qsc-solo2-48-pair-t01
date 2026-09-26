"""退火窑网带。

温度告警置位时网带速度压到保护低速，避免制品在窑内被拉裂；告警复位后
网带必须跟着提速。提速动作要看闩锁当下的状态，不能拿告警还在时的结论
接着用——闩锁已经释放而网带还按旧结论跑，制品就会在窑里过冷、出窑碎裂。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from ..config import LehrLimits
from ..eventbus import Event, EventBus


@dataclass(frozen=True)
class BeltOutcome:
    """一次网带速度结算结果。"""

    speed_m_per_min: float
    latched: bool
    latch_epoch: int
    normal: bool
    at: float

    def as_dict(self) -> dict:
        return {
            "speed_m_per_min": round(self.speed_m_per_min, 3),
            "latched": self.latched,
            "latch_epoch": self.latch_epoch,
            "normal": self.normal,
            "at": round(self.at, 3),
        }


class BeltDrive:
    """网带驱动与速度跟踪。"""

    def __init__(self, clock, bus: EventBus, limits: LehrLimits) -> None:
        self._clock = clock
        self._bus = bus
        self._limits = limits
        self._speed_m_per_min = limits.belt_slow_m_per_min
        self._last_latch_epoch = -1
        self._restores = 0
        self._holds = 0
        self._history: list[dict] = []
        self._lock = threading.RLock()

    @property
    def speed_m_per_min(self) -> float:
        with self._lock:
            return self._speed_m_per_min

    def step(self, latch) -> BeltOutcome:
        """按闩锁当下的状态结算网带速度。"""

        latched = bool(latch.latched)
        epoch = int(latch.epoch)
        with self._lock:
            if latched:
                self._speed_m_per_min = self._limits.belt_slow_m_per_min
                self._holds += 1
            else:
                if self._speed_m_per_min <= self._limits.belt_slow_m_per_min:
                    self._restores += 1
                step = self._limits.belt_release_step()
                self._speed_m_per_min = min(
                    self._limits.belt_speed_m_per_min, self._speed_m_per_min + step
                )
            self._last_latch_epoch = epoch
            outcome = BeltOutcome(
                speed_m_per_min=self._speed_m_per_min,
                latched=latched,
                latch_epoch=epoch,
                normal=self._limits.belt_ok(self._speed_m_per_min),
                at=self._clock.now(),
            )
            self._history.append(outcome.as_dict())
            if len(self._history) > 200:
                del self._history[: len(self._history) - 200]
        self._bus.publish(
            Event.of(
                "lehr",
                "lehr",
                "belt_speed_settled",
                subject="belt_drive",
                severity="notice",
                speed_m_per_min=round(outcome.speed_m_per_min, 3),
                latched=latched,
                latch_epoch=epoch,
            )
        )
        return outcome

    def last_latch_epoch(self) -> int:
        """返回最近一次结算用到的闩锁代次（仅供展示与审计）。"""

        with self._lock:
            return self._last_latch_epoch

    def is_normal(self) -> bool:
        """网带速度是否已恢复到正常档。"""

        return self._limits.belt_ok(self.speed_m_per_min)

    def counts(self) -> dict:
        with self._lock:
            return {
                "restores": self._restores,
                "holds": self._holds,
                "last_latch_epoch": self._last_latch_epoch,
            }

    def history(self, limit: int = 5) -> list[dict]:
        with self._lock:
            return self._history[-limit:]

    def snapshot(self) -> dict:
        counts = self.counts()
        with self._lock:
            return {
                "speed_m_per_min": round(self._speed_m_per_min, 3),
                "normal_speed_m_per_min": self._limits.belt_speed_m_per_min,
                "slow_speed_m_per_min": self._limits.belt_slow_m_per_min,
                "normal": self._limits.belt_ok(self._speed_m_per_min),
                "restores": counts["restores"],
                "holds": counts["holds"],
                "last_latch_epoch": counts["last_latch_epoch"],
                "recent": list(self._history[-3:]),
            }
