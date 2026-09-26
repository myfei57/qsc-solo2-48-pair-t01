"""点火时序状态机。

点火顺序是「待助燃风 → 燃气投入 → 点火 → 燃烧」，任一步
失败都进闩锁。正常点火与失败后重新点火走的是同一套顺序：两条路径都必须
先建立助燃风，再开燃气。只把正常路径堵住，重新点火时炉膛里同样会先积
一团可燃气。
"""

from __future__ import annotations

import enum
import threading

from ..config import BurnerLimits
from ..errors import SequenceError
from ..eventbus import Event, EventBus
from ..furnace.gas import GasValve
from .air import CombustionAir


class IgnitionPhase(str, enum.Enum):
    """点火相位。"""

    IDLE = "idle"
    AIR = "air"
    GAS = "gas"
    IGNITE = "ignite"
    FLAME = "flame"
    PURGE = "purge"
    LATCHED = "latched"


class IgnitionUnit:
    """燃烧器点火单元。"""

    _ALLOWED = {
        IgnitionPhase.IDLE: (IgnitionPhase.AIR,),
        IgnitionPhase.AIR: (IgnitionPhase.GAS, IgnitionPhase.LATCHED),
        IgnitionPhase.GAS: (IgnitionPhase.IGNITE, IgnitionPhase.LATCHED),
        IgnitionPhase.IGNITE: (IgnitionPhase.FLAME, IgnitionPhase.LATCHED),
        IgnitionPhase.FLAME: (IgnitionPhase.PURGE, IgnitionPhase.IDLE),
        IgnitionPhase.PURGE: (IgnitionPhase.IDLE, IgnitionPhase.AIR),
        IgnitionPhase.LATCHED: (IgnitionPhase.PURGE,),
    }

    def __init__(
        self,
        bus: EventBus,
        air: CombustionAir,
        gas: GasValve,
        limits: BurnerLimits,
    ) -> None:
        self._bus = bus
        self._air = air
        self._gas = gas
        self._limits = limits
        self._phase = IgnitionPhase.IDLE
        self._history: list[IgnitionPhase] = [IgnitionPhase.IDLE]
        self._starts = 0
        self._latches = 0
        self._lock = threading.RLock()

    @property
    def phase(self) -> IgnitionPhase:
        with self._lock:
            return self._phase

    def is_burning(self) -> bool:
        return self.phase is IgnitionPhase.FLAME

    def _transition(self, target: IgnitionPhase) -> IgnitionPhase:
        with self._lock:
            if target == self._phase:
                return self._phase
            if target not in self._ALLOWED[self._phase]:
                raise SequenceError(
                    f"点火不允许从 {self._phase.value} 迁移到 {target.value}",
                    component="burner",
                )
            self._phase = target
            self._history.append(target)
            return self._phase

    def ignite(self, flame_temperature_c: float) -> dict:
        """正常点火：助燃风 → 燃气 → 点火 → 判燃。"""

        return self._run_sequence(flame_temperature_c, path="ignite")

    def reignite(self, flame_temperature_c: float, reason: str) -> dict:
        """失焰后重新点火：走与正常点火相同的助燃风前置顺序。"""

        self._bus.publish(
            Event.of(
                "burner",
                "burner",
                "reignite_requested",
                subject="igniter",
                severity="notice",
                reason=reason,
            )
        )
        with self._lock:
            phase = self._phase
        if phase in (IgnitionPhase.FLAME, IgnitionPhase.LATCHED):
            self.purge(self._limits.air_min_flow_m3_h * 0.5)
        return self._run_sequence(flame_temperature_c, path="reignite")

    def _run_sequence(self, flame_temperature_c: float, path: str) -> dict:
        with self._lock:
            self._starts += 1
        self._transition(IgnitionPhase.AIR)
        air_state = self._air.establish()
        self._transition(IgnitionPhase.GAS)
        gas_flow = self._gas.open(self._air.state())
        self._transition(IgnitionPhase.IGNITE)
        if not self._limits.flame_ok(flame_temperature_c):
            self._transition(IgnitionPhase.LATCHED)
            with self._lock:
                self._latches += 1
            self._bus.publish(
                Event.of(
                    "burner",
                    "burner",
                    "ignition_failed",
                    subject="igniter",
                    severity="alarm",
                    path=path,
                    flame_temperature_c=round(flame_temperature_c, 2),
                    air_epoch=air_state.epoch,
                )
            )
            return {
                "path": path,
                "phase": self.phase.value,
                "air_established": air_state.established,
                "air_epoch": air_state.epoch,
                "gas_flow_m3_h": round(gas_flow, 2),
                "flame_ok": False,
            }
        self._transition(IgnitionPhase.FLAME)
        self._bus.publish(
            Event.of(
                "burner",
                "burner",
                "ignited",
                subject="igniter",
                severity="notice",
                path=path,
                gas_flow_m3_h=round(gas_flow, 2),
                flame_temperature_c=round(flame_temperature_c, 2),
                air_epoch=air_state.epoch,
            )
        )
        return {
            "path": path,
            "phase": self.phase.value,
            "air_established": air_state.established,
            "air_epoch": air_state.epoch,
            "gas_flow_m3_h": round(gas_flow, 2),
            "flame_ok": True,
        }

    def purge(self, purge_flow_m3_h: float) -> dict:
        """吹扫：关燃气、按吹扫风量置换炉膛残余可燃气。"""

        self._gas.close()
        state = self._air.purge(purge_flow_m3_h)
        self._transition(IgnitionPhase.PURGE)
        self._transition(IgnitionPhase.IDLE)
        return {"phase": self.phase.value, "air": state.as_dict()}

    def release_latch(self, purge_flow_m3_h: float) -> dict:
        """闩锁解除：先吹扫再回到待机，允许下一次点火。"""

        with self._lock:
            if self._phase is not IgnitionPhase.LATCHED:
                raise SequenceError("点火单元不在闩锁状态", component="burner")
        result = self.purge(purge_flow_m3_h)
        result["released_from_latch"] = True
        return result

    def counts(self) -> dict:
        with self._lock:
            return {"starts": self._starts, "latches": self._latches}

    def history(self) -> list[str]:
        with self._lock:
            return [phase.value for phase in self._history]

    def snapshot(self) -> dict:
        counts = self.counts()
        with self._lock:
            return {
                "phase": self._phase.value,
                "burning": self._phase is IgnitionPhase.FLAME,
                "history": self.history(),
                "starts": counts["starts"],
                "latches": counts["latches"],
                "gas": self._gas.snapshot(),
                "air": self._air.snapshot(),
            }
