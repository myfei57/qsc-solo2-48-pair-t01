"""炉压与排烟风机。

停炉时助燃风必须先关、排烟风机才能停：反过来做会把炉膛推成正压，火舌从
观火孔往外冒。所以风机停机动作要现场核对助燃风状态，不能只看调用方给的
一份推断。
"""

from __future__ import annotations

import threading

from ..config import FurnaceLimits
from ..errors import InterlockError
from ..eventbus import Event, EventBus


class DraftFan:
    """排烟风机与炉膛压力。"""

    def __init__(self, bus: EventBus, limits: FurnaceLimits) -> None:
        self._bus = bus
        self._limits = limits
        self._running = False
        self._pressure_pa = 0.0
        self._stops = 0
        self._lock = threading.RLock()

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._running

    @property
    def pressure_pa(self) -> float:
        with self._lock:
            return self._pressure_pa

    def start(self) -> float:
        """启动风机建立炉膛负压，返回当前压力。"""

        with self._lock:
            self._running = True
            self._pressure_pa = self._limits.draft_target_pa
            pressure = self._pressure_pa
        self._bus.publish(
            Event.of(
                "furnace",
                "furnace",
                "draft_started",
                subject="draft_fan",
                pressure_pa=round(pressure, 3),
            )
        )
        return pressure

    def stop(self, air_state) -> float:
        """在助燃风已关闭的条件下停风机，返回停机后的炉膛压力。"""

        if getattr(air_state, "established", True):
            raise InterlockError("助燃风尚未关闭，禁止停排烟风机", component="furnace")
        with self._lock:
            self._running = False
            self._pressure_pa = 0.0
            self._stops += 1
            pressure = self._pressure_pa
        self._bus.publish(
            Event.of(
                "furnace",
                "furnace",
                "draft_stopped",
                subject="draft_fan",
                severity="notice",
                pressure_pa=round(pressure, 3),
            )
        )
        return pressure

    def is_negative(self) -> bool:
        """炉膛是否处于要求的负压。"""

        return self._limits.draft_ok(self.pressure_pa)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "running": self._running,
                "pressure_pa": round(self._pressure_pa, 3),
                "target_pa": self._limits.draft_target_pa,
                "negative": self._limits.draft_ok(self._pressure_pa),
                "stops": self._stops,
            }
