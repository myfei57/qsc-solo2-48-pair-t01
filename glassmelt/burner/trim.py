"""含氧量空燃比修正。

固定配比只能保证"表计上的比例"：燃气热值波动、风门漏风、表计漂移都会让
实际空燃比慢慢跑偏，看火工只能凭火焰颜色事后追。这里在固定配比上叠一个
有界修正系数：按烟气含氧量与目标的偏差做慢积分，系数每拍变化率受限，
且加风（缺氧）方向比减风方向收得快——缺氧出黑烟是安全事故，富氧只是
费点燃料。测点不可信时先冻结、超时退回固定配比并提示，恢复需连续可信
满确认时间才重新闭环：绝不允许拿着一个不可信的读数继续积分，越烧越偏。
"""

from __future__ import annotations

import enum
import threading
from dataclasses import dataclass

from ..config import OxygenTrimLimits
from ..eventbus import Event, EventBus
from .o2probe import O2Reading


class TrimMode(str, enum.Enum):
    """修正回路运行方式。"""

    DISABLED = "disabled"
    AUTO = "auto"
    HOLD = "hold"
    FIXED = "fixed"


@dataclass(frozen=True)
class TrimOutput:
    """一拍修正结算。"""

    mode: TrimMode
    trim: float
    ratio: float
    target_o2_pct: float
    error_pct: float
    integrating: bool
    o2_credible: bool
    evaluated_at: float

    def as_dict(self) -> dict:
        return {
            "mode": self.mode.value,
            "trim": round(self.trim, 5),
            "ratio": round(self.ratio, 4),
            "target_o2_pct": round(self.target_o2_pct, 3),
            "error_pct": round(self.error_pct, 4),
            "integrating": self.integrating,
            "o2_credible": self.o2_credible,
            "evaluated_at": round(self.evaluated_at, 3),
        }


class OxygenTrim:
    """按烟气含氧量慢收敛的空燃比修正器。"""

    def __init__(self, clock, bus: EventBus, limits: OxygenTrimLimits) -> None:
        self._clock = clock
        self._bus = bus
        self._limits = limits
        self._mode = TrimMode.DISABLED
        self._enabled = False
        self._trim = limits.trim_safe
        self._integral = 0.0
        self._bad_since: float | None = None
        self._good_since: float | None = None
        self._saturated = False
        self._fallbacks = 0
        self._recoveries = 0
        self._lock = threading.RLock()

    @property
    def mode(self) -> TrimMode:
        with self._lock:
            return self._mode

    @property
    def trim(self) -> float:
        """当前修正系数（1.0 即固定配比）。"""

        with self._lock:
            return self._trim

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def enable(self) -> TrimMode:
        """投运修正回路；点火成功、燃烧稳定后才允许调用。"""

        with self._lock:
            self._enabled = True
            if self._mode is TrimMode.DISABLED:
                self._mode = TrimMode.HOLD
            mode = self._mode
        self._bus.publish(
            Event.of(
                "burner",
                "burner",
                "trim_enabled",
                subject="oxygen_trim",
                severity="notice",
                mode=mode.value,
            )
        )
        return mode

    def disable(self, reason: str) -> TrimMode:
        """退出修正回路；系数无扰收回到固定配比。"""

        with self._lock:
            self._enabled = False
            previous = self._mode
            self._mode = TrimMode.DISABLED
            self._bad_since = None
            self._good_since = None
        self._bus.publish(
            Event.of(
                "burner",
                "burner",
                "trim_disabled",
                subject="oxygen_trim",
                severity="notice",
                reason=reason,
                previous_mode=previous.value,
            )
        )
        return previous

    def force_fixed(self, reason: str) -> TrimMode:
        """人工退回固定配比（与测点超时退回走同一条路径）。"""

        with self._lock:
            previous = self._mode
            if self._mode is not TrimMode.FIXED:
                self._mode = TrimMode.FIXED
                self._fallbacks += 1
            self._bad_since = None
            self._good_since = None
        self._bus.publish(
            Event.of(
                "burner",
                "burner",
                "trim_forced_fixed",
                subject="oxygen_trim",
                severity="alarm",
                reason=reason,
                previous_mode=previous.value,
                trim=round(self.trim, 5),
            )
        )
        return previous

    def step(self, reading: O2Reading, load: float, dt_s: float) -> TrimOutput:
        """推进一拍修正：先定运行方式，再结算修正系数。"""

        moment = self._clock.now()
        dt = max(0.001, float(dt_s))
        load = max(0.0, min(1.0, float(load)))
        with self._lock:
            previous_mode = self._mode
            self._update_mode(reading, load, moment)
            transition = (
                (previous_mode, self._mode) if self._mode is not previous_mode else None
            )

            integrating = False
            if (
                self._mode is TrimMode.AUTO
                and reading.credible
                and load >= self._limits.o2_min_load
            ):
                integrating = self._integrate(reading, load, dt)
                desired = self._limits.trim_safe + self._integral
            elif self._mode in (TrimMode.FIXED, TrimMode.DISABLED):
                # 退回与退出都把系数无扰收向固定配比；积分器跟随当前
                # 系数，重新闭环时从现场值起步，不产生扰动。
                desired = self._limits.trim_safe
                self._integral = self._trim - self._limits.trim_safe
            else:
                desired = self._trim

            self._trim = self._slew_toward(desired, dt)
            saturated = self._check_saturation(reading, load)
            entered_saturation = saturated and not self._saturated
            self._saturated = saturated
            output = TrimOutput(
                mode=self._mode,
                trim=self._trim,
                ratio=self._limits.ratio_at(self._trim),
                target_o2_pct=self._limits.target_at_load(load),
                error_pct=self._limits.target_at_load(load) - reading.value_pct,
                integrating=integrating,
                o2_credible=reading.credible,
                evaluated_at=moment,
            )
            transition_payload = (
                {"from_mode": transition[0].value, "to_mode": transition[1].value}
                if transition
                else None
            )
        if transition_payload is not None:
            self._bus.publish(
                Event.of(
                    "burner",
                    "burner",
                    "trim_mode_changed",
                    subject="oxygen_trim",
                    severity="alarm" if self._mode is TrimMode.FIXED else "notice",
                    trim=round(self._trim, 5),
                    o2_credible=reading.credible,
                    reasons=list(reading.reasons),
                    **transition_payload,
                )
            )
        if entered_saturation:
            self._bus.publish(
                Event.of(
                    "burner",
                    "burner",
                    "trim_saturated",
                    subject="oxygen_trim",
                    severity="notice",
                    trim=round(self._trim, 5),
                    message="修正系数已到行程边界，配比偏差超出自动修正能力，请检查燃气热值与表计",
                )
            )
        return output

    def _update_mode(self, reading: O2Reading, load: float, moment: float) -> None:
        """按测点可信度与工况迁移运行方式（调用方须持锁）。"""

        if not self._enabled:
            self._mode = TrimMode.DISABLED
            self._bad_since = None
            self._good_since = None
            return
        if self._mode is TrimMode.FIXED:
            # 只有连续可信满确认时间才允许重新闭环。
            if reading.credible and load >= self._limits.o2_min_load:
                if self._good_since is None:
                    self._good_since = moment
                if moment - self._good_since >= self._limits.recover_require_s:
                    self._mode = TrimMode.AUTO
                    self._recoveries += 1
                    self._good_since = None
            else:
                self._good_since = None
            return
        if not reading.credible:
            self._good_since = None
            if self._bad_since is None:
                self._bad_since = moment
            bad_for = moment - self._bad_since
            if bad_for >= self._limits.fixed_after_s:
                self._mode = TrimMode.FIXED
                self._fallbacks += 1
            elif bad_for >= self._limits.hold_enter_s:
                self._mode = TrimMode.HOLD
            # 短时抖动：保持原方式，本拍不积分（由 step 的 integrating 保证）
            return
        self._bad_since = None
        self._good_since = None
        if load < self._limits.o2_min_load:
            # 低负荷烟气量少、残氧代表性差，冻结系数不闭环。
            self._mode = TrimMode.HOLD
        else:
            self._mode = TrimMode.AUTO

    def _integrate(self, reading: O2Reading, load: float, dt: float) -> bool:
        """慢积分一拍，返回本拍是否真正积了分（调用方须持锁）。"""

        if self._limits.in_deadband(reading.value_pct, load):
            return False
        error = self._limits.target_at_load(load) - reading.value_pct
        # 条件积分抗饱和：系数已顶到边界且偏差还在往外推时不再积。
        if (self._trim >= self._limits.trim_max and error > 0) or (
            self._trim <= self._limits.trim_min and error < 0
        ):
            return False
        step = error * (self._limits.integral_per_min / 100.0) / 60.0 * dt
        lower = self._limits.trim_min - self._limits.trim_safe
        upper = self._limits.trim_max - self._limits.trim_safe
        self._integral = min(upper, max(lower, self._integral + step))
        return True

    def _slew_toward(self, desired: float, dt: float) -> float:
        """按方向受限的速率把系数收向目标（调用方须持锁）。"""

        delta = desired - self._trim
        up = self._limits.slew_up_pct_per_min / 100.0 / 60.0 * dt
        down = self._limits.slew_down_pct_per_min / 100.0 / 60.0 * dt
        delta = min(up, max(-down, delta))
        return min(self._limits.trim_max, max(self._limits.trim_min, self._trim + delta))

    def _check_saturation(self, reading: O2Reading, load: float) -> bool:
        """系数是否顶在行程边界且偏差仍向外推（调用方须持锁）。"""

        if self._mode is not TrimMode.AUTO:
            return False
        error = self._limits.target_at_load(load) - reading.value_pct
        if self._trim >= self._limits.trim_max and error > self._limits.o2_deadband_pct:
            return True
        if self._trim <= self._limits.trim_min and error < -self._limits.o2_deadband_pct:
            return True
        return False

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "mode": self.mode.value,
                "enabled": self.enabled,
                "trim": round(self._trim, 5),
                "ratio": round(self._limits.ratio_at(self._trim), 4),
                "safe_ratio": round(self._limits.safe_ratio(), 4),
                "integral": round(self._integral, 6),
                "trim_range": [self._limits.trim_min, self._limits.trim_max],
                "saturated": self._saturated,
                "fallbacks": self._fallbacks,
                "recoveries": self._recoveries,
            }
