"""玻璃液面监测与确认。

投料前必须先确认窑内液面：液面过低才能补料，液面一旦被压到允许带以上，
继续投料会把生料堆过熔化线。因此"确认"不能是一份长期有效的凭据——每次
投料动作都要按当前液面重新判一遍。出料或清炉后确认凭据必须作废，否则
恢复投料会拿着作废的确认继续加料。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from ..clock import Clock
from ..config import FurnaceLimits
from ..eventbus import Event, EventBus


@dataclass(frozen=True)
class LevelCheck:
    """一次液面确认的结果。"""

    level_mm: float
    target_mm: float
    band_mm: float
    ok: bool
    high: bool
    epoch: int
    checked_at: float

    def as_dict(self) -> dict:
        return {
            "level_mm": round(self.level_mm, 3),
            "target_mm": self.target_mm,
            "band_mm": self.band_mm,
            "ok": self.ok,
            "high": self.high,
            "epoch": self.epoch,
            "checked_at": round(self.checked_at, 3),
        }


class GlassLevel:
    """窑内液面测量与确认缓存。"""

    def __init__(self, clock: Clock, bus: EventBus, limits: FurnaceLimits) -> None:
        self._clock = clock
        self._bus = bus
        self._limits = limits
        self._level_mm = limits.level_target_mm
        self._epoch = 0
        self._confirmation: LevelCheck | None = None
        self._checks = 0
        self._clears = 0
        self._lock = threading.RLock()

    @property
    def level_mm(self) -> float:
        with self._lock:
            return self._level_mm

    @property
    def epoch(self) -> int:
        """确认凭据的代次；清炉或出料后加一。"""

        with self._lock:
            return self._epoch

    def observe(self, level_mm: float) -> float:
        """更新液面测量值。"""

        with self._lock:
            self._level_mm = float(level_mm)
            value = self._level_mm
        self._bus.publish(
            Event.of("furnace", "furnace", "level_observed", level_mm=round(value, 3))
        )
        return value

    def drift(self, delta_mm: float) -> float:
        """按投料／出料量调整液面，返回新的液面高度。"""

        return self.observe(self.level_mm + float(delta_mm))

    def check(self) -> LevelCheck:
        """现场复核液面：始终按当前测量值重新判定，并把本次结果记为最新确认。"""

        with self._lock:
            result = LevelCheck(
                level_mm=self._level_mm,
                target_mm=self._limits.level_target_mm,
                band_mm=self._limits.level_band_mm,
                ok=self._limits.level_ok(self._level_mm),
                high=self._limits.level_high(self._level_mm),
                epoch=self._epoch,
                checked_at=self._clock.now(),
            )
            self._confirmation = result
            self._checks += 1
        self._bus.publish(
            Event.of(
                "furnace",
                "furnace",
                "level_checked",
                subject="glass_level",
                severity="info" if result.ok else "warning",
                level_mm=round(result.level_mm, 3),
                epoch=result.epoch,
                ok=result.ok,
            )
        )
        return result

    def last_confirmation(self) -> LevelCheck | None:
        """返回最近一次确认结果（仅供展示与审计，不代替现场复核）。"""

        with self._lock:
            return self._confirmation

    def clear(self) -> int:
        """出料或清炉后作废确认凭据，返回作废后的代次。"""

        with self._lock:
            self._confirmation = None
            self._epoch += 1
            self._clears += 1
            epoch = self._epoch
        self._bus.publish(
            Event.of(
                "furnace",
                "furnace",
                "level_cleared",
                subject="glass_level",
                severity="notice",
                epoch=epoch,
            )
        )
        return epoch

    def counts(self) -> dict:
        with self._lock:
            return {"checks": self._checks, "clears": self._clears}

    def snapshot(self) -> dict:
        confirmation = self.last_confirmation()
        counts = self.counts()
        with self._lock:
            return {
                "level_mm": round(self._level_mm, 3),
                "level_ok": self._limits.level_ok(self._level_mm),
                "level_high": self._limits.level_high(self._level_mm),
                "target_mm": self._limits.level_target_mm,
                "band_mm": self._limits.level_band_mm,
                "epoch": self._epoch,
                "checks": counts["checks"],
                "clears": counts["clears"],
                "last_confirmation": confirmation.as_dict() if confirmation else None,
            }
