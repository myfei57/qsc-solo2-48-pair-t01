"""空燃交叉限幅。

负荷变动时燃气与助燃风不能各追各的目标：加负荷必须风先加、气后加，
减负荷必须气先减、风后减，否则动态过程里会瞬时缺氧冒黑烟。这里按双交叉
限幅结构，用实测流量互相约束对方的需求：燃气需求不许超过当前风量（加
偏置）能支撑的上限，风量需求不许低于当前燃气（扣偏置）所需的下限。偏置
量保证稳态时两侧都能回到各自给定，不被自己的保护边界卡住。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from ..config import OxygenTrimLimits
from ..eventbus import Event, EventBus


@dataclass(frozen=True)
class RatioDemand:
    """一次交叉限幅结算出的空燃需求。"""

    gas_m3_h: float
    air_m3_h: float
    ratio: float
    trim: float
    gas_limited: bool
    air_limited: bool

    def as_dict(self) -> dict:
        return {
            "gas_m3_h": round(self.gas_m3_h, 2),
            "air_m3_h": round(self.air_m3_h, 2),
            "ratio": round(self.ratio, 4),
            "trim": round(self.trim, 5),
            "gas_limited": self.gas_limited,
            "air_limited": self.air_limited,
        }


class CrossLimiter:
    """按实测流量互相约束的空燃需求限幅器。"""

    def __init__(
        self, bus: EventBus, limits: OxygenTrimLimits, gas_max_m3_h: float
    ) -> None:
        self._bus = bus
        self._limits = limits
        self._gas_max = float(gas_max_m3_h)
        self._limited_cycles = 0
        self._was_limited = False
        self._last: RatioDemand | None = None
        self._lock = threading.RLock()

    def demands(
        self,
        gas_demand_m3_h: float,
        air_flow_m3_h: float,
        gas_flow_m3_h: float,
        trim: float,
    ) -> RatioDemand:
        """按当前实测流量与修正系数结算本拍空燃需求。"""

        ratio = self._limits.ratio_at(trim)
        bias_gas = self._gas_max * self._limits.cross_limiter_bias_pct / 100.0
        gas_want = max(0.0, float(gas_demand_m3_h))
        air_want = gas_want * ratio

        # 风量下限：当前燃气量（扣偏置）烧掉所需的风，且不低于运行最小风量。
        air_floor = max(
            self._limits.air_min_operating_m3_h,
            max(0.0, float(gas_flow_m3_h) - bias_gas) * ratio,
        )
        air = max(air_want, air_floor)

        # 燃气上限：当前风量（加偏置）能支撑的燃气量。
        gas_cap = max(0.0, (float(air_flow_m3_h) + bias_gas * ratio) / ratio)
        gas = min(gas_want, gas_cap)

        gas_limited = gas < gas_want - 1e-9
        air_limited = air > air_want + 1e-9
        demand = RatioDemand(
            gas_m3_h=gas,
            air_m3_h=air,
            ratio=ratio,
            trim=float(trim),
            gas_limited=gas_limited,
            air_limited=air_limited,
        )
        with self._lock:
            limited = gas_limited or air_limited
            entered = limited and not self._was_limited
            self._was_limited = limited
            if limited:
                self._limited_cycles += 1
            self._last = demand
        if entered:
            self._bus.publish(
                Event.of(
                    "burner",
                    "burner",
                    "cross_limit_engaged",
                    subject="cross_limiter",
                    severity="notice",
                    **demand.as_dict(),
                )
            )
        return demand

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "bias_pct": self._limits.cross_limiter_bias_pct,
                "air_min_operating_m3_h": self._limits.air_min_operating_m3_h,
                "limited": self._was_limited,
                "limited_cycles": self._limited_cycles,
                "last": self._last.as_dict() if self._last else None,
            }
