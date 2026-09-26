"""退火段。

退火窑按段控温：每段有各自的给定值，段温决定制品通过该段时的降温速率。
曲线算出各段给定值之后由这里落到段温上，控制台按段展示，冷却速率用于
判断出窑制品会不会因为降温过快而开裂。
"""

from __future__ import annotations

import threading

from ..config import LehrLimits
from ..eventbus import Event, EventBus


class AnnealZones:
    """退火段温度。"""

    def __init__(self, bus: EventBus, limits: LehrLimits, start_c: float = 60.0) -> None:
        self._bus = bus
        self._limits = limits
        self._temperatures: dict[str, float] = {
            zone: float(start_c) for zone in limits.zone_names()
        }
        self._applies = 0
        self._lock = threading.RLock()

    def apply(self, setpoints: dict, elapsed_s: float) -> dict:
        """把各段给定值落到段温上，返回落定后的段温映射。"""

        zones = self._limits.zone_names()
        with self._lock:
            for zone in zones:
                target = float(setpoints.get(zone, 0.0))
                current = self._temperatures.get(zone, target)
                self._temperatures[zone] = current + (target - current) * 0.5
            self._applies += 1
            result = dict(self._temperatures)
        self._bus.publish(
            Event.of(
                "lehr",
                "lehr",
                "anneal_zones_applied",
                subject="anneal_zones",
                elapsed_s=round(float(elapsed_s), 3),
                mean_c=round(sum(result.values()) / max(1, len(result)), 3),
            )
        )
        return result

    def zone_temperatures(self) -> dict:
        with self._lock:
            return dict(self._temperatures)

    def mean_c(self) -> float:
        with self._lock:
            values = list(self._temperatures.values())
        if not values:
            return 0.0
        return sum(values) / len(values)

    def cooling_rate_c_per_min(self) -> float:
        """首末段温差对应的平均降温速率。"""

        zones = self._limits.zone_names()
        with self._lock:
            head = self._temperatures.get(zones[0], 0.0)
            tail = self._temperatures.get(zones[-1], 0.0)
        return (head - tail) / max(1.0, len(zones) - 1)

    def snapshot(self) -> dict:
        temperatures = self.zone_temperatures()
        with self._lock:
            applies = self._applies
        return {
            "temperatures": {zone: round(value, 3) for zone, value in temperatures.items()},
            "mean_c": round(self.mean_c(), 3),
            "cooling_rate_c_per_min": round(self.cooling_rate_c_per_min(), 3),
            "applies": applies,
        }
