"""退火温度告警。

退火窑温度高于告警值时置位告警并压低加热功率，属于保护动作；温度回落到
复位值以下时告警必须自动复位，否则加热器会一直停在低功率，出窑制品降温
过快、一碰就裂。告警带迟滞：置位点与复位点不是同一个值。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from ..config import TempLimits
from ..eventbus import Event, EventBus


@dataclass(frozen=True)
class AlarmTransition:
    """一次告警状态迁移。"""

    active: bool
    temperature_c: float
    threshold_c: float
    at: float

    def as_dict(self) -> dict:
        return {
            "active": self.active,
            "temperature_c": round(self.temperature_c, 3),
            "threshold_c": round(self.threshold_c, 3),
            "at": round(self.at, 3),
        }


class AnnealAlarm:
    """退火温度告警（带迟滞）。"""

    def __init__(self, clock, bus: EventBus, limits: TempLimits) -> None:
        self._clock = clock
        self._bus = bus
        self._limits = limits
        self._active = False
        self._temperature_c = limits.anneal_target_c
        self._transitions: list[AlarmTransition] = []
        self._sets = 0
        self._clears = 0
        self._lock = threading.RLock()

    @property
    def active(self) -> bool:
        with self._lock:
            return self._active

    @property
    def temperature_c(self) -> float:
        with self._lock:
            return self._temperature_c

    def observe(self, temperature_c: float) -> bool:
        """更新退火温度并现场判定告警状态，返回告警是否置位。"""

        moment = self._clock.now()
        value = float(temperature_c)
        transition: AlarmTransition | None = None
        with self._lock:
            self._temperature_c = value
            if not self._active and self._limits.alarm_should_set(value):
                self._active = True
                self._sets += 1
                transition = AlarmTransition(
                    active=True,
                    temperature_c=value,
                    threshold_c=self._limits.anneal_alarm_c,
                    at=moment,
                )
            elif self._active and self._limits.alarm_should_clear(value):
                self._active = False
                self._clears += 1
                transition = AlarmTransition(
                    active=False,
                    temperature_c=value,
                    threshold_c=self._limits.anneal_clear_c,
                    at=moment,
                )
            if transition is not None:
                self._transitions.append(transition)
            active = self._active
        if transition is not None:
            self._bus.publish(
                Event.of(
                    "temp",
                    "temp",
                    "anneal_alarm_set" if transition.active else "anneal_alarm_clear",
                    subject="anneal_temp",
                    severity="alarm" if transition.active else "notice",
                    temperature_c=round(value, 3),
                    threshold_c=round(transition.threshold_c, 3),
                )
            )
        return active

    def is_active(self) -> bool:
        return self.active

    def clear(self, reason: str) -> bool:
        """人工复位告警，返回复位前是否处于告警状态。"""

        was = False
        with self._lock:
            was = self._active
            if was:
                self._active = False
                self._clears += 1
                self._transitions.append(
                    AlarmTransition(
                        active=False,
                        temperature_c=self._temperature_c,
                        threshold_c=self._limits.anneal_clear_c,
                        at=self._clock.now(),
                    )
                )
        if was:
            self._bus.publish(
                Event.of(
                    "temp",
                    "temp",
                    "anneal_alarm_cleared_manually",
                    subject="anneal_temp",
                    severity="notice",
                    reason=reason,
                )
            )
        return was

    def counts(self) -> dict:
        with self._lock:
            return {"sets": self._sets, "clears": self._clears}

    def transitions(self) -> list[dict]:
        with self._lock:
            return [item.as_dict() for item in self._transitions]

    def snapshot(self) -> dict:
        counts = self.counts()
        with self._lock:
            return {
                "active": self._active,
                "temperature_c": round(self._temperature_c, 3),
                "alarm_c": self._limits.anneal_alarm_c,
                "clear_c": self._limits.anneal_clear_c,
                "sets": counts["sets"],
                "clears": counts["clears"],
                "recent": self.transitions()[-3:],
                "last_transition": self._transitions[-1].as_dict()
                if self._transitions
                else None,
            }
