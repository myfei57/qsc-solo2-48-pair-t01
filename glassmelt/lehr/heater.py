"""退火窑加热器。

加热器按退火曲线的给定值出力；温度告警闩锁期间压到最低功率保窑。告警
复位、闩锁释放之后，加热器必须回到按曲线出力——闩锁已经释放还按告警时
的结论输出最低功率，窑内温度上不去，出窑制品一碰就裂。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from ..config import LehrLimits
from ..eventbus import Event, EventBus


@dataclass(frozen=True)
class HeaterOutcome:
    """一次加热器出力结算结果。"""

    power_pct: float
    setpoint_c: float
    current_c: float
    latched: bool
    latch_epoch: int
    at: float

    def as_dict(self) -> dict:
        return {
            "power_pct": round(self.power_pct, 2),
            "setpoint_c": round(self.setpoint_c, 3),
            "current_c": round(self.current_c, 3),
            "latched": self.latched,
            "latch_epoch": self.latch_epoch,
            "at": round(self.at, 3),
        }


class HeaterBank:
    """退火窑加热器组。"""

    def __init__(self, clock, bus: EventBus, limits: LehrLimits) -> None:
        self._clock = clock
        self._bus = bus
        self._limits = limits
        self._power_pct = limits.heater_min_pct
        self._zone_powers: dict[str, float] = {
            zone: limits.heater_min_pct for zone in limits.zone_names()
        }
        self._last_latch_epoch = -1
        self._restores = 0
        self._holds = 0
        self._history: list[dict] = []
        self._lock = threading.RLock()

    @property
    def power_pct(self) -> float:
        with self._lock:
            return self._power_pct

    def step(self, setpoint_c: float, current_c: float, latch) -> HeaterOutcome:
        """按曲线给定值与闩锁当下状态结算加热功率。"""

        latched = bool(latch.latched)
        epoch = int(latch.epoch)
        if latched:
            power = self._limits.heater_min_pct
        else:
            power = 50.0 + (float(setpoint_c) - float(current_c)) * 4.0
            power = max(self._limits.heater_min_pct, min(self._limits.heater_max_pct, power))
        with self._lock:
            if latched:
                self._holds += 1
            elif self._power_pct <= self._limits.heater_min_pct:
                self._restores += 1
            self._power_pct = power
            for zone in self._zone_powers:
                self._zone_powers[zone] = power
            self._last_latch_epoch = epoch
            outcome = HeaterOutcome(
                power_pct=power,
                setpoint_c=float(setpoint_c),
                current_c=float(current_c),
                latched=latched,
                latch_epoch=epoch,
                at=self._clock.now(),
            )
            self._history.append(outcome.as_dict())
            if len(self._history) > 200:
                del self._history[: len(self._history) - 200]
        self._bus.publish(
            Event.of(
                "lehr",
                "lehr",
                "heater_power_settled",
                subject="heater_bank",
                severity="notice",
                power_pct=round(power, 2),
                setpoint_c=round(float(setpoint_c), 3),
                latched=latched,
                latch_epoch=epoch,
            )
        )
        return outcome

    def zone_powers(self) -> dict:
        with self._lock:
            return dict(self._zone_powers)

    def last_latch_epoch(self) -> int:
        """返回最近一次结算用到的闩锁代次（仅供展示与审计）。"""

        with self._lock:
            return self._last_latch_epoch

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
                "power_pct": round(self._power_pct, 2),
                "min_pct": self._limits.heater_min_pct,
                "max_pct": self._limits.heater_max_pct,
                "zone_powers": dict(self._zone_powers),
                "restores": counts["restores"],
                "holds": counts["holds"],
                "last_latch_epoch": counts["last_latch_epoch"],
                "recent": list(self._history[-3:]),
            }
