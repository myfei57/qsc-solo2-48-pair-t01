"""烟气含氧量校正空燃配比。

固定配比挡不住燃气热值波动、风温变化和风道泄漏：配比偏富时火焰拉长发飘，
直舔窑顶（窑顶烤红大多这么来）；配比偏贫时火焰短、窑温上不去。看火工只能
间断纠偏，所以把校正交给控制回路，但必须满足三条现场规矩：

1. 慢：窑体热惯性以小时计，校正只能以小步积分收敛，快调必然振荡；
2. 有限：修正幅度以固定配比为中心有硬上下界，测点再疯也把配比拉不出界；
3. 可回退：含氧量测点不可信（超量程、卡死、断更）时先冻结积分，宽限期内
   不恢复就退回固定配比并告警，恢复后从零修正重新收敛。

风量输出还有一道与修正量无关的硬下限：永不低于燃气安全配比所需风量，也永
不低于助燃风点火下限——宁可偏贫，绝不富燃。
"""

from __future__ import annotations

import enum
import threading
from collections import deque
from dataclasses import dataclass

from ..config import O2TrimLimits
from ..eventbus import Event, EventBus


class TrimMode(str, enum.Enum):
    """校正回路模式。"""

    DISABLED = "disabled"
    TRIMMING = "trimming"
    FALLBACK = "fallback"


@dataclass(frozen=True)
class TrimState:
    """一拍校正的结果：应执行的风量设定与判定依据。"""

    mode: TrimMode
    o2_pct: float | None
    o2_setpoint_pct: float
    trim_pct: float
    ratio: float
    air_setpoint_m3_h: float
    sensor_ok: bool
    invalid_reason: str | None
    invalid_for_s: float
    epoch: int
    evaluated_at: float

    def as_dict(self) -> dict:
        return {
            "mode": self.mode.value,
            "o2_pct": None if self.o2_pct is None else round(self.o2_pct, 3),
            "o2_setpoint_pct": self.o2_setpoint_pct,
            "trim_pct": round(self.trim_pct, 3),
            "ratio": round(self.ratio, 4),
            "air_setpoint_m3_h": round(self.air_setpoint_m3_h, 2),
            "sensor_ok": self.sensor_ok,
            "invalid_reason": self.invalid_reason,
            "invalid_for_s": round(self.invalid_for_s, 3),
            "epoch": self.epoch,
            "evaluated_at": round(self.evaluated_at, 3),
        }


class O2TrimController:
    """按烟气含氧量慢速收敛空燃配比的校正回路。"""

    def __init__(
        self,
        clock,
        bus: EventBus,
        limits: O2TrimLimits,
        air_min_flow_m3_h: float,
    ) -> None:
        self._clock = clock
        self._bus = bus
        self._limits = limits
        self._air_min_flow_m3_h = float(air_min_flow_m3_h)
        self._mode = TrimMode.DISABLED
        self._trim = 0.0
        self._o2_pct: float | None = None
        self._last_sample_at: float | None = None
        self._sample_ok = False
        self._sample_invalid_reason: str | None = "no_sample"
        self._recent: deque[float] = deque(maxlen=limits.sensor_freeze_samples)
        self._invalid_since: float | None = None
        self._invalid_reason: str | None = None
        self._suspect_notified = False
        self._epoch = 0
        self._fallbacks = 0
        self._recoveries = 0
        self._samples = 0
        self._rejected = 0
        self._last_state: TrimState | None = None
        self._lock = threading.RLock()

    @property
    def mode(self) -> TrimMode:
        with self._lock:
            return self._mode

    @property
    def epoch(self) -> int:
        """回退代次；每次退回固定配比后加一。"""

        with self._lock:
            return self._epoch

    def is_enabled(self) -> bool:
        return self.mode is not TrimMode.DISABLED

    def enable(self, at: float | None = None) -> TrimMode:
        """点火成功后投入校正：从零修正起步，给首个样本留出宽限期。"""

        moment = self._clock.now() if at is None else float(at)
        with self._lock:
            if self._mode is TrimMode.DISABLED:
                self._mode = TrimMode.TRIMMING
                self._trim = 0.0
                self._invalid_since = moment
                self._invalid_reason = self._sample_invalid_reason
                self._suspect_notified = False
                enabled = True
            else:
                enabled = False
        if enabled:
            self._bus.publish(
                Event.of(
                    "burner",
                    "burner",
                    "o2_trim_enabled",
                    subject="o2_trim",
                    severity="notice",
                    o2_setpoint_pct=self._limits.o2_setpoint_pct,
                )
            )
        return self.mode

    def disable(self, reason: str) -> TrimMode:
        """停炉或燃烧中断时退出校正：修正清零，不再驱动风量。"""

        with self._lock:
            if self._mode is TrimMode.DISABLED:
                return self._mode
            self._mode = TrimMode.DISABLED
            self._trim = 0.0
            self._invalid_since = None
            self._suspect_notified = False
        self._bus.publish(
            Event.of(
                "burner",
                "burner",
                "o2_trim_disabled",
                subject="o2_trim",
                severity="notice",
                reason=reason,
            )
        )
        return self.mode

    def observe(self, o2_pct: float, at: float | None = None) -> bool:
        """记录一个含氧量样本并判定可信度，返回本次样本是否可信。"""

        moment = self._clock.now() if at is None else float(at)
        value = float(o2_pct)
        with self._lock:
            self._samples += 1
            ok, reason = self._validate(value)
            self._o2_pct = value
            self._last_sample_at = moment
            self._sample_ok = ok
            self._sample_invalid_reason = reason
            if not ok:
                self._rejected += 1
        return ok

    def _validate(self, value: float) -> tuple[bool, str | None]:
        if not self._limits.o2_plausible(value):
            return False, "range"
        self._recent.append(value)
        if len(self._recent) == self._limits.sensor_freeze_samples:
            if max(self._recent) - min(self._recent) <= self._limits.sensor_freeze_eps_pct:
                return False, "frozen"
        return True, None

    def update(self, gas_flow_m3_h: float, dt_s: float, at: float | None = None) -> TrimState:
        """推进一拍校正，返回本拍应执行的风量设定。"""

        moment = self._clock.now() if at is None else float(at)
        gas_flow_m3_h = max(0.0, float(gas_flow_m3_h))
        dt_s = max(0.0, float(dt_s))
        transitions: list[Event] = []
        with self._lock:
            sensor_ok, reason = self._sensor_verdict(moment)
            invalid_for_s = (
                0.0
                if self._invalid_since is None
                else max(0.0, moment - self._invalid_since)
            )

            if self._mode is TrimMode.DISABLED:
                state = self._conclude(
                    gas_flow_m3_h, 0.0, sensor_ok, reason, invalid_for_s, moment
                )
                self._last_state = state
                return state

            if sensor_ok:
                if self._mode is TrimMode.FALLBACK:
                    self._mode = TrimMode.TRIMMING
                    self._trim = 0.0
                    self._recoveries += 1
                    transitions.append(
                        Event.of(
                            "burner",
                            "burner",
                            "o2_trim_recovered",
                            subject="o2_trim",
                            severity="notice",
                            epoch=self._epoch,
                        )
                    )
                self._invalid_since = None
                self._invalid_reason = None
                self._suspect_notified = False
                self._integrate(gas_flow_m3_h, dt_s)
            else:
                if self._invalid_since is None:
                    self._invalid_since = moment
                    self._invalid_reason = reason
                    invalid_for_s = 0.0
                if not self._suspect_notified:
                    self._suspect_notified = True
                    transitions.append(
                        Event.of(
                            "burner",
                            "burner",
                            "o2_sensor_suspect",
                            subject="o2_trim",
                            severity="warning",
                            reason=reason,
                            o2_pct=None if self._o2_pct is None else round(self._o2_pct, 3),
                        )
                    )
                if (
                    moment - self._invalid_since >= self._limits.fallback_grace_s
                    and self._mode is not TrimMode.FALLBACK
                ):
                    self._mode = TrimMode.FALLBACK
                    self._trim = 0.0
                    self._epoch += 1
                    self._fallbacks += 1
                    transitions.append(
                        Event.of(
                            "burner",
                            "burner",
                            "o2_trim_fallback",
                            subject="o2_trim",
                            severity="alarm",
                            reason=self._invalid_reason,
                            ratio_base=self._limits.ratio_base,
                            epoch=self._epoch,
                        )
                    )

            trim_used = self._trim if self._mode is TrimMode.TRIMMING else 0.0
            state = self._conclude(
                gas_flow_m3_h, trim_used, sensor_ok, reason, invalid_for_s, moment
            )
            self._last_state = state
        for event in transitions:
            self._bus.publish(event)
        return state

    def _sensor_verdict(self, moment: float) -> tuple[bool, str | None]:
        """综合样本判定与断更判定，给出当前测点可信度。"""

        if self._last_sample_at is None:
            return False, "no_sample"
        if moment - self._last_sample_at > self._limits.sensor_stale_after_s:
            return False, "stale"
        return self._sample_ok, self._sample_invalid_reason

    def _integrate(self, gas_flow_m3_h: float, dt_s: float) -> None:
        """小步积分收敛；输出顶到风量下限且误差仍往下推时冻结积分。"""

        assert self._o2_pct is not None
        error = self._limits.o2_setpoint_pct - self._o2_pct
        cap = self._limits.trim_max_pct / 100.0
        candidate = self._trim + self._limits.trim_rate_per_s * error * dt_s
        candidate = max(-cap, min(cap, candidate))
        air_candidate = gas_flow_m3_h * self._limits.ratio_base * (1.0 + candidate)
        if air_candidate < self._air_floor(gas_flow_m3_h) and error < 0:
            return
        self._trim = candidate

    def _air_floor(self, gas_flow_m3_h: float) -> float:
        """风量硬下限：点火下限与安全配比下限取大者，永不富燃。"""

        return max(
            self._air_min_flow_m3_h,
            gas_flow_m3_h * self._limits.ratio_min_safe,
        )

    def _conclude(
        self,
        gas_flow_m3_h: float,
        trim: float,
        sensor_ok: bool,
        reason: str | None,
        invalid_for_s: float,
        moment: float,
    ) -> TrimState:
        air = max(
            self._air_floor(gas_flow_m3_h),
            gas_flow_m3_h * self._limits.ratio_base * (1.0 + trim),
        )
        ratio = air / gas_flow_m3_h if gas_flow_m3_h > 0 else self._limits.ratio_base
        return TrimState(
            mode=self._mode,
            o2_pct=self._o2_pct,
            o2_setpoint_pct=self._limits.o2_setpoint_pct,
            trim_pct=trim * 100.0,
            ratio=ratio,
            air_setpoint_m3_h=air,
            sensor_ok=sensor_ok,
            invalid_reason=None if sensor_ok else reason,
            invalid_for_s=invalid_for_s,
            epoch=self._epoch,
            evaluated_at=moment,
        )

    def last_state(self) -> TrimState | None:
        """返回最近一拍校正结果（仅供展示与审计）。"""

        with self._lock:
            return self._last_state

    def snapshot(self) -> dict:
        state = self.last_state()
        with self._lock:
            return {
                "mode": self._mode.value,
                "enabled": self._mode is not TrimMode.DISABLED,
                "o2_pct": None if self._o2_pct is None else round(self._o2_pct, 3),
                "o2_setpoint_pct": self._limits.o2_setpoint_pct,
                "trim_pct": round(self._trim * 100.0, 3),
                "trim_max_pct": self._limits.trim_max_pct,
                "ratio_base": self._limits.ratio_base,
                "ratio_min_safe": self._limits.ratio_min_safe,
                "sensor_ok": self._sample_ok,
                "invalid_reason": self._sample_invalid_reason,
                "epoch": self._epoch,
                "fallbacks": self._fallbacks,
                "recoveries": self._recoveries,
                "samples": self._samples,
                "rejected": self._rejected,
                "last_state": state.as_dict() if state else None,
            }
