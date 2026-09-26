"""供料道温度控制。

料道温度决定料滴温度：温度没到、或者还在波动，出来的料滴就会偏冷，成型
后瓶体出现水波纹。料道温度控制器只负责把温度推到设定值并给出热负荷，稳
不稳定的判定交给稳温监视器，闸板只认稳温结论。
"""

from __future__ import annotations

import threading

from ..config import ForehearthLimits
from ..eventbus import Event, EventBus


class ForehearthSteady:
    """料道温度与热负荷。"""

    def __init__(self, bus: EventBus, limits: ForehearthLimits) -> None:
        self._bus = bus
        self._limits = limits
        self._setpoint_c = limits.gate_min_temp_c + limits.stable_band_c
        self._temperature_c = self._setpoint_c
        self._samples = 0
        self._lock = threading.RLock()

    @property
    def setpoint_c(self) -> float:
        return self._setpoint_c

    @property
    def temperature_c(self) -> float:
        with self._lock:
            return self._temperature_c

    def observe(self, temperature_c: float) -> float:
        """更新料道温度测量值。"""

        with self._lock:
            self._temperature_c = float(temperature_c)
            self._samples += 1
            value = self._temperature_c
        self._bus.publish(
            Event.of(
                "forehearth",
                "forehearth",
                "steady_temp_observed",
                subject="forehearth_temp",
                temperature_c=round(value, 3),
            )
        )
        return value

    def drift(self, delta_c: float) -> float:
        """按热负荷调整料道温度，返回新的温度。"""

        return self.observe(self.temperature_c + float(delta_c))

    def deviation_c(self) -> float:
        """相对设定值的偏差。"""

        return self.temperature_c - self._setpoint_c

    def heat_demand_pct(self) -> float:
        """按偏差折算的加热需求，用于控制台展示与趋势采样。"""

        demand = 50.0 + self.deviation_c() * 6.0
        return max(0.0, min(100.0, demand))

    def is_above_gate_min(self) -> bool:
        """料道温度是否已达到开闸板下限。"""

        return self._limits.temperature_ok(self.temperature_c)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "temperature_c": round(self._temperature_c, 3),
                "setpoint_c": round(self._setpoint_c, 3),
                "deviation_c": round(self._temperature_c - self._setpoint_c, 3),
                "heat_demand_pct": round(self.heat_demand_pct(), 2),
                "above_gate_min": self._limits.temperature_ok(self._temperature_c),
                "samples": self._samples,
            }
