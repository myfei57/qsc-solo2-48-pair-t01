"""退火侧闩锁。

温度告警置位时把网带速度与加热功率压到保护档并闩锁保持；告警复位之后
闩锁必须跟着释放，执行机构才能回到正常档。释放动作要看当下告警是否真的
已经复位，不能用闩锁置位当时的判断结果去决定要不要释放。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from ..eventbus import Event, EventBus


@dataclass(frozen=True)
class LatchTransition:
    """一次闩锁迁移。"""

    latched: bool
    reason: str
    at: float
    epoch: int

    def as_dict(self) -> dict:
        return {
            "latched": self.latched,
            "reason": self.reason,
            "at": round(self.at, 3),
            "epoch": self.epoch,
        }


class HeaterLatch:
    """网带速度与加热功率共用的保护闩锁。"""

    def __init__(self, clock, bus: EventBus) -> None:
        self._clock = clock
        self._bus = bus
        self._latched = False
        self._epoch = 0
        self._sets = 0
        self._releases = 0
        self._transitions: list[LatchTransition] = []
        self._lock = threading.RLock()

    @property
    def latched(self) -> bool:
        with self._lock:
            return self._latched

    @property
    def epoch(self) -> int:
        """闩锁代次；每次置位或释放后加一。"""

        with self._lock:
            return self._epoch

    def _apply(self, latched: bool, reason: str) -> bool:
        with self._lock:
            if latched == self._latched:
                return False
            self._latched = latched
            self._epoch += 1
            if latched:
                self._sets += 1
            else:
                self._releases += 1
            self._transitions.append(
                LatchTransition(
                    latched=latched,
                    reason=reason,
                    at=self._clock.now(),
                    epoch=self._epoch,
                )
            )
            epoch = self._epoch
        self._bus.publish(
            Event.of(
                "temp",
                "temp",
                "anneal_latch_set" if latched else "anneal_latch_released",
                subject="anneal_latch",
                severity="warning" if latched else "notice",
                reason=reason,
                epoch=epoch,
            )
        )
        return True

    def latch(self, reason: str) -> bool:
        """置位闩锁，返回状态是否发生变化。"""

        return self._apply(True, reason)

    def release(self, reason: str) -> bool:
        """释放闩锁，返回状态是否发生变化。"""

        return self._apply(False, reason)

    def release_after_clear(self, alarm) -> bool:
        """告警复位后释放闩锁：现场核对告警是否真的已复位。"""

        if alarm.is_active():
            return False
        return self.release("退火告警复位")

    def follow_alarm(self, alarm) -> bool:
        """按告警状态同步闩锁：置位时闩锁，复位时释放。"""

        if alarm.is_active():
            return self.latch("退火温度告警")
        return self.release_after_clear(alarm)

    def counts(self) -> dict:
        with self._lock:
            return {"sets": self._sets, "releases": self._releases, "epoch": self._epoch}

    def transitions(self) -> list[dict]:
        with self._lock:
            return [item.as_dict() for item in self._transitions]

    def snapshot(self) -> dict:
        counts = self.counts()
        with self._lock:
            return {
                "latched": self._latched,
                "epoch": counts["epoch"],
                "sets": counts["sets"],
                "releases": counts["releases"],
                "recent": self.transitions()[-3:],
                "last_transition": self._transitions[-1].as_dict()
                if self._transitions
                else None,
            }
