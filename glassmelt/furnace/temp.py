"""窑温监测。

熔化温度决定玻璃液能不能流出供料道。窑温偏低时料液黏度上升，成型后瓶体
出现水波纹；窑温偏高会加速窑衬侵蚀。燃烧器的点火与功率调整都以这份测量
值为依据，因此窑温对象只负责测量与判定，不参与点火顺序。
"""

from __future__ import annotations

import threading

from ..config import FurnaceLimits
from ..eventbus import Event, EventBus


class FurnaceTemperature:
    """窑温测量与区间判定。"""

    def __init__(self, bus: EventBus, limits: FurnaceLimits) -> None:
        self._bus = bus
        self._limits = limits
        self._temperature_c = limits.melt_temp_target_c
        self._samples = 0
        self._lock = threading.RLock()

    @property
    def temperature_c(self) -> float:
        with self._lock:
            return self._temperature_c

    @property
    def target_c(self) -> float:
        return self._limits.melt_temp_target_c

    def observe(self, temperature_c: float) -> float:
        """更新窑温测量值。"""

        with self._lock:
            self._temperature_c = float(temperature_c)
            self._samples += 1
            value = self._temperature_c
        self._bus.publish(
            Event.of("furnace", "furnace", "melt_temp_observed", temperature_c=round(value, 2))
        )
        return value

    def is_ok(self) -> bool:
        """窑温是否落在熔化区间内。"""

        return self._limits.temp_ok(self.temperature_c)

    def is_low(self) -> bool:
        """窑温是否已低于熔化区间下限。"""

        return self.temperature_c < self._limits.melt_temp_min_c

    def deviation_c(self) -> float:
        """相对目标温度的偏差，用于燃烧器功率修正。"""

        return self.temperature_c - self._limits.melt_temp_target_c

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "temperature_c": round(self._temperature_c, 2),
                "target_c": self._limits.melt_temp_target_c,
                "min_c": self._limits.melt_temp_min_c,
                "max_c": self._limits.melt_temp_max_c,
                "in_range": self._limits.temp_ok(self._temperature_c),
                "samples": self._samples,
            }
