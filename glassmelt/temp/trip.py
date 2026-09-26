"""跳闸联锁。

跳闸要按当前的分区映射关掉对应的燃烧器。窑体大修后映射表会换版，跳闸
动作必须现场解析一次映射，并把用到的修订号写进结果；拿着大修前的映射
去关阀，会关错燃烧器，一侧窑温掉下来、另一侧还在烧。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from ..errors import SequenceError
from ..eventbus import Event, EventBus


@dataclass(frozen=True)
class TripOutcome:
    """一次跳闸联动结果。"""

    zone: str
    burner_id: str
    revision: int
    reason: str
    at: float

    def as_dict(self) -> dict:
        return {
            "zone": self.zone,
            "burner_id": self.burner_id,
            "revision": self.revision,
            "reason": self.reason,
            "at": round(self.at, 3),
        }


class TripInterlock:
    """跳闸联动：按当前分区映射关断燃烧器。"""

    def __init__(self, clock, bus: EventBus, zone_map) -> None:
        self._clock = clock
        self._bus = bus
        self._zone_map = zone_map
        self._tripped = False
        self._trips = 0
        self._history: list[TripOutcome] = []
        self._lock = threading.RLock()

    @property
    def zone_map_revision(self) -> int:
        return self._zone_map.revision

    def is_tripped(self) -> bool:
        with self._lock:
            return self._tripped

    def trip(self, zone: str, reason: str) -> TripOutcome:
        """按当前映射解析出要关断的燃烧器并执行跳闸。"""

        assignment = self._zone_map.resolve(zone)
        fresh = self._zone_map.is_current(assignment.revision)
        with self._lock:
            if self._tripped:
                raise SequenceError("联锁已处于跳闸状态", component="temp")
            self._tripped = True
            self._trips += 1
            outcome = TripOutcome(
                zone=assignment.zone,
                burner_id=assignment.burner_id,
                revision=assignment.revision,
                reason=reason,
                at=self._clock.now(),
            )
            self._history.append(outcome)
        self._bus.publish(
            Event.of(
                "temp",
                "temp",
                "trip_activated",
                subject="trip_interlock",
                severity="alarm",
                zone=outcome.zone,
                burner_id=outcome.burner_id,
                revision=outcome.revision,
                reason=reason,
                fresh=fresh,
            )
        )
        return outcome

    def reset(self, reason: str) -> bool:
        """跳闸复位，返回复位前是否处于跳闸状态。"""

        with self._lock:
            was = self._tripped
            self._tripped = False
        if was:
            self._bus.publish(
                Event.of(
                    "temp",
                    "temp",
                    "trip_reset",
                    subject="trip_interlock",
                    severity="notice",
                    reason=reason,
                )
            )
        return was

    def counts(self) -> dict:
        with self._lock:
            return {"trips": self._trips, "history": len(self._history)}

    def history(self, limit: int = 5) -> list[dict]:
        with self._lock:
            return [item.as_dict() for item in self._history[-limit:]]

    def snapshot(self) -> dict:
        counts = self.counts()
        return {
            "tripped": self.is_tripped(),
            "trips": counts["trips"],
            "zone_map_revision": self._zone_map.revision,
            "recent": self.history(),
        }
