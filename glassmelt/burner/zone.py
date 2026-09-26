"""燃烧器分区映射。

窑体大修后燃烧器编号与窑体分区会重新对应，跳闸联动必须按新映射关阀。
映射表带修订号：跳闸时用的是哪一版映射要落到审计里，控制台也能按修订号
核对"这次联动是不是按大修后的分区执行的"。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from ..config import BurnerLimits
from ..errors import InterlockError
from ..eventbus import Event, EventBus


@dataclass(frozen=True)
class ZoneAssignment:
    """一条分区映射。"""

    zone: str
    burner_id: str
    revision: int

    def as_dict(self) -> dict:
        return {"zone": self.zone, "burner_id": self.burner_id, "revision": self.revision}


class ZoneMap:
    """分区到燃烧器的映射表。"""

    def __init__(self, bus: EventBus, limits: BurnerLimits) -> None:
        self._bus = bus
        self._limits = limits
        self._revision = 1
        self._assignments: dict[str, str] = {
            zone: f"burner-{index + 1}" for index, zone in enumerate(limits.zone_names())
        }
        self._rebuilds = 0
        self._lock = threading.RLock()

    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision

    def resolve(self, zone: str) -> ZoneAssignment:
        """按当前映射解析分区对应的燃烧器。"""

        with self._lock:
            burner_id = self._assignments.get(zone)
            revision = self._revision
        if burner_id is None:
            raise InterlockError(f"分区未登记: {zone}", component="burner")
        return ZoneAssignment(zone=zone, burner_id=burner_id, revision=revision)

    def rebuild(self, assignments: dict, reason: str) -> int:
        """窑体大修后重建映射表，返回新的修订号。"""

        zones = self._limits.zone_names()
        if not isinstance(assignments, dict) or set(assignments) != set(zones):
            raise InterlockError("分区映射必须覆盖全部窑体分区", component="burner")
        with self._lock:
            self._assignments = {zone: str(assignments[zone]) for zone in zones}
            self._revision += 1
            self._rebuilds += 1
            revision = self._revision
        self._bus.publish(
            Event.of(
                "burner",
                "burner",
                "zone_map_rebuilt",
                subject="zone_map",
                severity="notice",
                reason=reason,
                revision=revision,
            )
        )
        return revision

    def is_current(self, revision: int) -> bool:
        """给定修订号是否就是当前映射。"""

        return int(revision) == self.revision

    def zones(self) -> list[str]:
        with self._lock:
            return sorted(self._assignments)

    def describe(self) -> list[dict]:
        with self._lock:
            revision = self._revision
            items = [
                ZoneAssignment(zone=zone, burner_id=burner, revision=revision)
                for zone, burner in sorted(self._assignments.items())
            ]
        return [item.as_dict() for item in items]

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "revision": self._revision,
                "rebuilds": self._rebuilds,
                "assignments": dict(sorted(self._assignments.items())),
            }
