"""烟气含氧量测点。

修正回路能不能闭环，不取决于分析仪表自己报不报故障，而取决于读数本身
可不可信。锆头老化、探头堵灰、标定中会给出三类假信号：超量程的硬坏值、
一闪而过的尖峰、以及长时间一动不动的"冻值"。这里对每一拍原始读数做一阶
滤波，并按量程、单拍跳变、冻值、空燃物料平衡四条独立判据现场重判可信度，
只把这一拍的结论交给下游——下游不允许沿用上一拍"当时还好"的旧结论。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from ..config import OxygenTrimLimits
from ..eventbus import Event, EventBus


@dataclass(frozen=True)
class O2Reading:
    """一次含氧量评估结果。"""

    raw_pct: float
    value_pct: float
    credible: bool
    reasons: tuple[str, ...]
    epoch: int
    evaluated_at: float

    def as_dict(self) -> dict:
        return {
            "raw_pct": round(self.raw_pct, 4),
            "value_pct": round(self.value_pct, 4),
            "credible": self.credible,
            "reasons": list(self.reasons),
            "epoch": self.epoch,
            "evaluated_at": round(self.evaluated_at, 3),
        }


class OxygenProbe:
    """烟气含氧量分析与测点可信度评估。"""

    def __init__(self, clock, bus: EventBus, limits: OxygenTrimLimits) -> None:
        self._clock = clock
        self._bus = bus
        self._limits = limits
        self._raw_pct = limits.o2_target_pct
        self._prev_raw_pct: float | None = None
        self._filtered_pct = limits.o2_target_pct
        self._change_anchor = limits.o2_target_pct
        self._last_change_at = clock.now()
        self._epoch = 0
        self._rejections = 0
        self._last_reading: O2Reading | None = None
        self._lock = threading.RLock()

    @property
    def value_pct(self) -> float:
        """最近一次滤波后的读数（未经可信度判定，仅供展示）。"""

        with self._lock:
            return self._filtered_pct

    @property
    def epoch(self) -> int:
        """测点代次；读数从可信变为不可信时加一。"""

        with self._lock:
            return self._epoch

    def invalidate(self, reason: str) -> O2Reading:
        """标定／反吹期间由现场信号直接作废本拍读数。"""

        return self.evaluate(
            self._raw_pct,
            1.0,
            air_flow_m3_h=0.0,
            gas_flow_m3_h=0.0,
            forced_reason=reason,
        )

    def evaluate(
        self,
        raw_pct: float,
        dt_s: float,
        air_flow_m3_h: float = 0.0,
        gas_flow_m3_h: float = 0.0,
        forced_reason: str = "",
    ) -> O2Reading:
        """读入一拍原始含氧量，现场滤波并重判可信度。"""

        moment = self._clock.now()
        raw = float(raw_pct)
        dt = max(0.001, float(dt_s))
        with self._lock:
            reasons: list[str] = []

            in_range = (
                self._limits.o2_valid_min_pct <= raw <= self._limits.o2_valid_max_pct
            )
            if not in_range:
                reasons.append("out_of_range")

            # 尖峰判据看原始信号自身的单拍跳变；首拍没有上一拍，不判。
            spiked = False
            if self._prev_raw_pct is not None:
                slew = abs(raw - self._prev_raw_pct) / dt
                spiked = slew > self._limits.o2_slew_pct_per_s
            if in_range and spiked:
                reasons.append("spike")

            # 一阶低通滤波；硬坏值与尖峰不进入滤波器，避免污染历史。
            accepted = in_range and not spiked and not forced_reason
            if accepted:
                alpha = dt / (self._limits.filter_tau_s + dt)
                self._filtered_pct += alpha * (raw - self._filtered_pct)
            filtered = self._filtered_pct

            # 冻值判据：原始读数长时间没有任何超出噪声带的变化。
            if abs(raw - self._change_anchor) > self._limits.o2_freeze_noise_pct:
                self._change_anchor = raw
                self._last_change_at = moment
            if moment - self._last_change_at >= self._limits.o2_freeze_hold_s:
                reasons.append("frozen")

            # 物料平衡：残氧与当前空燃流量推算的理论值偏差过大，说明
            # 不是燃烧真变了，而是表或流量信号有一个在说谎。
            expected = self._expected_o2(air_flow_m3_h, gas_flow_m3_h)
            if expected is not None and abs(filtered - expected) > (
                self._limits.plausibility_tolerance_pct
            ):
                reasons.append("mass_balance")

            if forced_reason:
                reasons.append(str(forced_reason))

            credible = not reasons
            if not credible and (
                self._last_reading is None or self._last_reading.credible
            ):
                self._epoch += 1
                self._rejections += 1
            self._raw_pct = raw
            self._prev_raw_pct = raw
            reading = O2Reading(
                raw_pct=raw,
                value_pct=filtered,
                credible=credible,
                reasons=tuple(reasons),
                epoch=self._epoch,
                evaluated_at=moment,
            )
            self._last_reading = reading
            payload = reading.as_dict()
        self._bus.publish(
            Event.of(
                "burner",
                "burner",
                "o2_evaluated",
                subject="flue_o2_probe",
                severity="info" if reading.credible else "alarm",
                **payload,
            )
        )
        return reading

    def _expected_o2(
        self, air_flow_m3_h: float, gas_flow_m3_h: float
    ) -> float | None:
        """按当前空燃体积流量估算理论残氧；燃气断流时无法估算。"""

        if gas_flow_m3_h <= 1.0:
            return None
        return self._limits.o2_at_ratio(air_flow_m3_h / gas_flow_m3_h)

    def state(self) -> O2Reading | None:
        """返回最近一次评估结果（仅供展示与审计）。"""

        with self._lock:
            return self._last_reading

    def snapshot(self) -> dict:
        reading = self.state()
        with self._lock:
            return {
                "raw_pct": round(self._raw_pct, 4),
                "value_pct": round(self.value_pct, 4),
                "target_pct": self._limits.o2_target_pct,
                "valid_range_pct": [
                    self._limits.o2_valid_min_pct,
                    self._limits.o2_valid_max_pct,
                ],
                "credible": bool(reading.credible) if reading else False,
                "reasons": list(reading.reasons) if reading else [],
                "epoch": self.epoch,
                "rejections": self._rejections,
            }
