"""平台配置。

配置来自 YAML（由 vendored PyYAML 解析），覆盖玻璃液面带、熔化温度区间、
助燃风与燃气上限、供料道稳温带与保持时间、退火告警阈值、料滴质量公差、
合模力下限、网带快慢速与投料秤标定范围。任何缺失或越界取值都在装配阶段
抛出 :class:`ConfigError`，不允许带着半份配置启动控制链。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Mapping

import yaml

from .errors import ConfigError

_MISSING = object()


def _section(mapping: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = mapping.get(key)
    if value is None:
        raise ConfigError(f"配置缺少段落 {key!r}", component="config")
    if not isinstance(value, Mapping):
        raise ConfigError(f"配置段落 {key!r} 必须是映射", component="config")
    return value


def _number(section: Mapping[str, Any], key: str, *, minimum: float | None = None) -> float:
    value = section.get(key, _MISSING)
    if value is _MISSING:
        raise ConfigError(f"配置缺少字段 {key!r}", component="config")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"配置字段 {key!r} 必须是数值", component="config")
    number = float(value)
    if minimum is not None and number < minimum:
        raise ConfigError(f"配置字段 {key!r} 不得小于 {minimum}", component="config")
    return number


@dataclass(frozen=True)
class FurnaceLimits:
    """玻璃液面与熔化温度约束。"""

    level_target_mm: float
    level_band_mm: float
    melt_temp_target_c: float
    melt_temp_min_c: float
    melt_temp_max_c: float
    draft_target_pa: float

    @classmethod
    def from_mapping(cls, section: Mapping[str, Any]) -> "FurnaceLimits":
        limits = cls(
            level_target_mm=_number(section, "level_target_mm", minimum=0.0),
            level_band_mm=_number(section, "level_band_mm", minimum=0.0),
            melt_temp_target_c=_number(section, "melt_temp_target_c"),
            melt_temp_min_c=_number(section, "melt_temp_min_c"),
            melt_temp_max_c=_number(section, "melt_temp_max_c"),
            draft_target_pa=_number(section, "draft_target_pa"),
        )
        if limits.level_band_mm <= 0:
            raise ConfigError("液面允许带必须为正", component="config")
        if not limits.melt_temp_min_c < limits.melt_temp_target_c < limits.melt_temp_max_c:
            raise ConfigError("熔化温度必须满足 下限 < 目标 < 上限", component="config")
        if limits.draft_target_pa >= 0:
            raise ConfigError("炉膛负压目标必须为负值", component="config")
        return limits

    def level_ok(self, level_mm: float) -> bool:
        """液面是否落在允许带内。"""

        return abs(float(level_mm) - self.level_target_mm) <= self.level_band_mm

    def level_high(self, level_mm: float) -> bool:
        """液面是否已高于允许带上沿（继续投料会堆过熔化线）。"""

        return float(level_mm) > self.level_target_mm + self.level_band_mm

    def temp_ok(self, temperature_c: float) -> bool:
        """窑温是否在熔化区间内。"""

        return self.melt_temp_min_c <= float(temperature_c) <= self.melt_temp_max_c

    def draft_ok(self, pressure_pa: float) -> bool:
        """炉膛是否处于负压（越负越安全）。"""

        return float(pressure_pa) <= self.draft_target_pa


@dataclass(frozen=True)
class BurnerLimits:
    """燃烧器与分区燃烧约束。"""

    air_min_flow_m3_h: float
    gas_max_m3_h: float
    ignition_timeout_s: float
    zone_count: int
    flame_min_temp_c: float

    @classmethod
    def from_mapping(cls, section: Mapping[str, Any]) -> "BurnerLimits":
        limits = cls(
            air_min_flow_m3_h=_number(section, "air_min_flow_m3_h", minimum=0.0),
            gas_max_m3_h=_number(section, "gas_max_m3_h", minimum=0.0),
            ignition_timeout_s=_number(section, "ignition_timeout_s", minimum=0.1),
            zone_count=int(_number(section, "zone_count", minimum=1.0)),
            flame_min_temp_c=_number(section, "flame_min_temp_c"),
        )
        if limits.air_min_flow_m3_h <= 0:
            raise ConfigError("助燃风最小流量必须为正", component="config")
        if limits.gas_max_m3_h <= 0:
            raise ConfigError("燃气上限必须为正", component="config")
        return limits

    def air_ok(self, flow_m3_h: float) -> bool:
        """助燃风是否已建立起火所需流量。"""

        return float(flow_m3_h) >= self.air_min_flow_m3_h

    def flame_ok(self, temperature_c: float) -> bool:
        """测得的火焰温度是否达到判燃下限。"""

        return float(temperature_c) >= self.flame_min_temp_c

    def zone_names(self) -> tuple[str, ...]:
        """按分区数生成稳定的分区名。"""

        return tuple(f"zone-{index + 1}" for index in range(self.zone_count))


@dataclass(frozen=True)
class ForehearthLimits:
    """供料道稳温与闸板约束。"""

    stable_band_c: float
    stable_hold_s: float
    gate_min_temp_c: float
    gate_open_pct: float

    @classmethod
    def from_mapping(cls, section: Mapping[str, Any]) -> "ForehearthLimits":
        limits = cls(
            stable_band_c=_number(section, "stable_band_c", minimum=0.0),
            stable_hold_s=_number(section, "stable_hold_s", minimum=0.0),
            gate_min_temp_c=_number(section, "gate_min_temp_c"),
            gate_open_pct=_number(section, "gate_open_pct", minimum=0.0),
        )
        if limits.stable_band_c <= 0:
            raise ConfigError("稳温带必须为正", component="config")
        return limits

    def temperature_ok(self, temperature_c: float) -> bool:
        """料道温度是否已达到开闸板的下限。"""

        return float(temperature_c) >= self.gate_min_temp_c

    def within_band(self, temperature_c: float, setpoint_c: float) -> bool:
        """温度是否落在设定值的稳温带内。"""

        return abs(float(temperature_c) - float(setpoint_c)) <= self.stable_band_c


@dataclass(frozen=True)
class TempLimits:
    """退火温度告警与恒温段约束。"""

    anneal_alarm_c: float
    anneal_clear_c: float
    anneal_target_c: float
    soak_seconds: float

    @classmethod
    def from_mapping(cls, section: Mapping[str, Any]) -> "TempLimits":
        limits = cls(
            anneal_alarm_c=_number(section, "anneal_alarm_c"),
            anneal_clear_c=_number(section, "anneal_clear_c"),
            anneal_target_c=_number(section, "anneal_target_c"),
            soak_seconds=_number(section, "soak_seconds", minimum=0.0),
        )
        if not limits.anneal_target_c < limits.anneal_clear_c < limits.anneal_alarm_c:
            raise ConfigError(
                "退火温度必须满足 目标 < 复位值 < 告警值", component="config"
            )
        return limits

    def alarm_should_set(self, temperature_c: float) -> bool:
        """温度高于告警值时置位退火告警。"""

        return float(temperature_c) >= self.anneal_alarm_c

    def alarm_should_clear(self, temperature_c: float) -> bool:
        """温度回落到复位值以下时告警应当自动复位。"""

        return float(temperature_c) <= self.anneal_clear_c


@dataclass(frozen=True)
class FeederLimits:
    """料滴成型与剪切约束。"""

    gob_mass_target_g: float
    gob_mass_tolerance_g: float
    cut_window_ms: float
    recover_gob_mass_g: float

    @classmethod
    def from_mapping(cls, section: Mapping[str, Any]) -> "FeederLimits":
        limits = cls(
            gob_mass_target_g=_number(section, "gob_mass_target_g", minimum=0.0),
            gob_mass_tolerance_g=_number(section, "gob_mass_tolerance_g", minimum=0.0),
            cut_window_ms=_number(section, "cut_window_ms", minimum=0.0),
            recover_gob_mass_g=_number(section, "recover_gob_mass_g", minimum=0.0),
        )
        if limits.gob_mass_target_g <= 0:
            raise ConfigError("料滴目标质量必须为正", component="config")
        return limits

    def mass_ok(self, mass_g: float) -> bool:
        """料滴质量是否落在公差的成型带内。"""

        return abs(float(mass_g) - self.gob_mass_target_g) <= self.gob_mass_tolerance_g


@dataclass(frozen=True)
class MoldLimits:
    """合模与吹气约束。"""

    close_min_force_kn: float
    blow_pressure_kpa: float
    blow_min_ms: float
    reopen_delay_s: float

    @classmethod
    def from_mapping(cls, section: Mapping[str, Any]) -> "MoldLimits":
        limits = cls(
            close_min_force_kn=_number(section, "close_min_force_kn", minimum=0.0),
            blow_pressure_kpa=_number(section, "blow_pressure_kpa", minimum=0.0),
            blow_min_ms=_number(section, "blow_min_ms", minimum=0.0),
            reopen_delay_s=_number(section, "reopen_delay_s", minimum=0.0),
        )
        if limits.close_min_force_kn <= 0:
            raise ConfigError("合模力下限必须为正", component="config")
        return limits

    def close_ok(self, force_kn: float) -> bool:
        """合模力是否已达到"到位"判据。"""

        return float(force_kn) >= self.close_min_force_kn


@dataclass(frozen=True)
class LehrLimits:
    """退火窑网带与加热器约束。"""

    belt_speed_m_per_min: float
    belt_slow_m_per_min: float
    heater_min_pct: float
    heater_max_pct: float
    anneal_zone_count: int

    @classmethod
    def from_mapping(cls, section: Mapping[str, Any]) -> "LehrLimits":
        limits = cls(
            belt_speed_m_per_min=_number(section, "belt_speed_m_per_min", minimum=0.0),
            belt_slow_m_per_min=_number(section, "belt_slow_m_per_min", minimum=0.0),
            heater_min_pct=_number(section, "heater_min_pct", minimum=0.0),
            heater_max_pct=_number(section, "heater_max_pct", minimum=0.0),
            anneal_zone_count=int(_number(section, "anneal_zone_count", minimum=1.0)),
        )
        if limits.belt_slow_m_per_min <= 0:
            raise ConfigError("网带低速必须为正", component="config")
        if limits.belt_slow_m_per_min >= limits.belt_speed_m_per_min:
            raise ConfigError("网带低速必须小于正常速度", component="config")
        if limits.heater_min_pct >= limits.heater_max_pct:
            raise ConfigError("加热器下限功率必须小于上限", component="config")
        return limits

    def belt_ok(self, speed_m_per_min: float) -> bool:
        """网带速度是否已恢复到正常档。"""

        return float(speed_m_per_min) >= self.belt_speed_m_per_min

    def belt_release_step(self) -> float:
        """每次提速的步长，按快慢档差值的四分之一取。"""

        return (self.belt_speed_m_per_min - self.belt_slow_m_per_min) / 4.0

    def zone_names(self) -> tuple[str, ...]:
        """按退火段数生成稳定的段名。"""

        return tuple(f"anneal-{index + 1}" for index in range(self.anneal_zone_count))


@dataclass(frozen=True)
class OxygenTrimLimits:
    """烟气含氧量空燃比修正约束。

    修正回路在固定空燃比（理论配比 × 基准过剩空气系数）上缓慢叠加一个有界
    修正系数：系数有界、每拍变化率有界，测点不可信或工况不适合闭环时退回
    固定配比。这里的每个取值都是燃烧安全边界，装配阶段必须全部校验，不
    允许带半份阈值投运。
    """

    ratio_air_per_gas: float
    excess_air_base: float
    trim_min: float
    trim_max: float
    trim_safe: float
    o2_target_pct: float
    o2_deadband_pct: float
    o2_low_load_target_bias_pct: float
    o2_min_load: float
    o2_valid_min_pct: float
    o2_valid_max_pct: float
    o2_slew_pct_per_s: float
    o2_freeze_noise_pct: float
    o2_freeze_hold_s: float
    filter_tau_s: float
    integral_per_min: float
    slew_up_pct_per_min: float
    slew_down_pct_per_min: float
    hold_enter_s: float
    fixed_after_s: float
    recover_require_s: float
    plausibility_tolerance_pct: float
    cross_limiter_bias_pct: float
    air_min_operating_m3_h: float

    @classmethod
    def from_mapping(cls, section: Mapping[str, Any]) -> "OxygenTrimLimits":
        values = {
            "ratio_air_per_gas": _number(section, "ratio_air_per_gas", minimum=0.1),
            "excess_air_base": _number(section, "excess_air_base", minimum=1.0),
            "trim_min": _number(section, "trim_min", minimum=0.5),
            "trim_max": _number(section, "trim_max", minimum=0.5),
            "trim_safe": _number(section, "trim_safe", minimum=0.5),
            "o2_target_pct": _number(section, "o2_target_pct", minimum=0.0),
            "o2_deadband_pct": _number(section, "o2_deadband_pct", minimum=0.0),
            "o2_low_load_target_bias_pct": _number(
                section, "o2_low_load_target_bias_pct", minimum=0.0
            ),
            "o2_min_load": _number(section, "o2_min_load", minimum=0.0),
            "o2_valid_min_pct": _number(section, "o2_valid_min_pct", minimum=0.0),
            "o2_valid_max_pct": _number(section, "o2_valid_max_pct", minimum=0.0),
            "o2_slew_pct_per_s": _number(section, "o2_slew_pct_per_s", minimum=0.0),
            "o2_freeze_noise_pct": _number(
                section, "o2_freeze_noise_pct", minimum=0.0
            ),
            "o2_freeze_hold_s": _number(section, "o2_freeze_hold_s", minimum=0.1),
            "filter_tau_s": _number(section, "filter_tau_s", minimum=0.0),
            "integral_per_min": _number(section, "integral_per_min", minimum=0.0),
            "slew_up_pct_per_min": _number(
                section, "slew_up_pct_per_min", minimum=0.0
            ),
            "slew_down_pct_per_min": _number(
                section, "slew_down_pct_per_min", minimum=0.0
            ),
            "hold_enter_s": _number(section, "hold_enter_s", minimum=0.0),
            "fixed_after_s": _number(section, "fixed_after_s", minimum=0.0),
            "recover_require_s": _number(section, "recover_require_s", minimum=0.1),
            "plausibility_tolerance_pct": _number(
                section, "plausibility_tolerance_pct", minimum=0.0
            ),
            "cross_limiter_bias_pct": _number(
                section, "cross_limiter_bias_pct", minimum=0.0
            ),
            "air_min_operating_m3_h": _number(
                section, "air_min_operating_m3_h", minimum=0.0
            ),
        }
        limits = cls(**values)
        if limits.excess_air_base <= 1.0:
            raise ConfigError(
                "基准过剩空气系数必须大于 1（固定配比本身要走贫燃）",
                component="config",
            )
        if not limits.trim_min < 1.0 < limits.trim_max:
            raise ConfigError(
                "修正系数必须满足 下限 < 1.0 < 上限", component="config"
            )
        if not (
            limits.trim_min <= limits.trim_safe <= limits.trim_max
            and limits.trim_safe >= 1.0
        ):
            raise ConfigError(
                "退回用安全修正系数必须落在 [下限, 上限] 内且不小于 1",
                component="config",
            )
        if not limits.o2_valid_min_pct < limits.o2_target_pct < limits.o2_valid_max_pct:
            raise ConfigError("含氧量目标必须落在有效量程内", component="config")
        if limits.o2_deadband_pct <= 0:
            raise ConfigError("含氧量死区必须为正", component="config")
        if not limits.hold_enter_s < limits.fixed_after_s:
            raise ConfigError(
                "冻结确认时间必须短于转固定配比时间", component="config"
            )
        if limits.integral_per_min <= 0:
            raise ConfigError("积分速率必须为正", component="config")
        if min(limits.slew_up_pct_per_min, limits.slew_down_pct_per_min) <= 0:
            raise ConfigError("加风与减风两个方向的修正速率必须为正", component="config")
        # 目标含氧量必须落在修正系数行程可达的残氧范围内，否则闭环永远
        # 顶在边界上，越烧越偏。
        reachable = (
            limits.o2_at_ratio(limits.ratio_at(limits.trim_min)),
            limits.o2_at_ratio(limits.ratio_at(limits.trim_max)),
        )
        if not reachable[0] < limits.o2_target_pct < reachable[1]:
            raise ConfigError(
                "含氧量目标必须落在修正行程可达范围内 "
                f"({reachable[0]:.2f} ~ {reachable[1]:.2f})",
                component="config",
            )
        return limits

    def ratio_at(self, trim: float) -> float:
        """给定修正系数下的有效空燃体积比。"""

        return self.ratio_air_per_gas * self.excess_air_base * float(trim)

    def safe_ratio(self) -> float:
        """测点不可信时退回的固定空燃比（即今天的固定配比）。"""

        return self.ratio_at(self.trim_safe)

    def o2_at_ratio(self, ratio: float) -> float:
        """按简化物性估算给定空燃比对应的干基残氧（%）。"""

        excess = max(0.0, float(ratio) - self.ratio_air_per_gas)
        return 20.9 * excess / (float(ratio) + 1.0)

    def target_at_load(self, load_fraction: float) -> float:
        """低负荷时目标含氧量整体抬高，避免低温段缺氧冒黑烟。"""

        load = max(0.0, min(1.0, float(load_fraction)))
        if load >= self.o2_min_load:
            return self.o2_target_pct
        return self.o2_target_pct + self.o2_low_load_target_bias_pct

    def in_deadband(self, o2_pct: float, load_fraction: float) -> bool:
        """实测含氧量是否落在目标死区内（死区内不动作）。"""

        target = self.target_at_load(load_fraction)
        return abs(float(o2_pct) - target) <= self.o2_deadband_pct


@dataclass(frozen=True)
class BatchLimits:
    """投料与配比约束。"""

    charge_batch_kg: float
    scale_offset_max_kg: float
    mix_tolerance_pct: float
    charge_window_s: float

    @classmethod
    def from_mapping(cls, section: Mapping[str, Any]) -> "BatchLimits":
        limits = cls(
            charge_batch_kg=_number(section, "charge_batch_kg", minimum=0.0),
            scale_offset_max_kg=_number(section, "scale_offset_max_kg", minimum=0.0),
            mix_tolerance_pct=_number(section, "mix_tolerance_pct", minimum=0.0),
            charge_window_s=_number(section, "charge_window_s", minimum=0.0),
        )
        if limits.charge_batch_kg <= 0:
            raise ConfigError("单次投料量必须为正", component="config")
        return limits

    def offset_ok(self, offset_kg: float) -> bool:
        """标定偏移量是否在允许范围内。"""

        return abs(float(offset_kg)) <= self.scale_offset_max_kg

    def mix_ok(self, deviation_pct: float) -> bool:
        """配比偏差是否在公差内。"""

        return abs(float(deviation_pct)) <= self.mix_tolerance_pct


@dataclass(frozen=True)
class PlatformConfig:
    """一份完整的平台配置。"""

    name: str
    project_id: str
    storage_root: str
    furnace: FurnaceLimits
    burner: BurnerLimits
    forehearth: ForehearthLimits
    temp: TempLimits
    feeder: FeederLimits
    mold: MoldLimits
    lehr: LehrLimits
    batch: BatchLimits
    oxygen: OxygenTrimLimits
    pages: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> "PlatformConfig":
        meta = _section(mapping, "platform")
        name = meta.get("name")
        project_id = meta.get("project_id")
        storage_root = meta.get("storage_root")
        if not isinstance(name, str) or not name:
            raise ConfigError("platform.name 必须是非空字符串", component="config")
        if not isinstance(project_id, str) or not project_id:
            raise ConfigError("platform.project_id 必须是非空字符串", component="config")
        if not isinstance(storage_root, str) or not storage_root:
            raise ConfigError("platform.storage_root 必须是非空字符串", component="config")

        limits = _section(mapping, "limits")
        pages = mapping.get("pages")
        if not isinstance(pages, list) or not pages:
            raise ConfigError("pages 必须是非空的页面名列表", component="config")
        for page in pages:
            if not isinstance(page, str) or not page.strip():
                raise ConfigError("pages 中的页面名必须是非空字符串", component="config")

        return cls(
            name=name,
            project_id=project_id,
            storage_root=storage_root,
            furnace=FurnaceLimits.from_mapping(_section(limits, "furnace")),
            burner=BurnerLimits.from_mapping(_section(limits, "burner")),
            forehearth=ForehearthLimits.from_mapping(_section(limits, "forehearth")),
            temp=TempLimits.from_mapping(_section(limits, "temp")),
            feeder=FeederLimits.from_mapping(_section(limits, "feeder")),
            mold=MoldLimits.from_mapping(_section(limits, "mold")),
            lehr=LehrLimits.from_mapping(_section(limits, "lehr")),
            batch=BatchLimits.from_mapping(_section(limits, "batch")),
            oxygen=OxygenTrimLimits.from_mapping(_section(limits, "oxygen")),
            pages=tuple(pages),
        )


def load_config(path: str) -> PlatformConfig:
    """从 YAML 文件读取配置。"""

    if not os.path.isfile(path):
        raise ConfigError(f"配置文件不存在: {path}", component="config")
    with open(path, "r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    if not isinstance(raw, Mapping):
        raise ConfigError("配置文件顶层必须是映射", component="config")
    return PlatformConfig.from_mapping(raw)


def default_config() -> PlatformConfig:
    """内置默认配置，供未指定配置文件时装配使用。"""

    return PlatformConfig(
        name="GlassMelt",
        project_id="line-control-py",
        storage_root="var/state",
        furnace=FurnaceLimits(
            level_target_mm=1120.0,
            level_band_mm=35.0,
            melt_temp_target_c=1425.0,
            melt_temp_min_c=1380.0,
            melt_temp_max_c=1470.0,
            draft_target_pa=-18.0,
        ),
        burner=BurnerLimits(
            air_min_flow_m3_h=5200.0,
            gas_max_m3_h=620.0,
            ignition_timeout_s=6.0,
            zone_count=6,
            flame_min_temp_c=660.0,
        ),
        forehearth=ForehearthLimits(
            stable_band_c=6.0,
            stable_hold_s=8.0,
            gate_min_temp_c=1180.0,
            gate_open_pct=100.0,
        ),
        temp=TempLimits(
            anneal_alarm_c=585.0,
            anneal_clear_c=575.0,
            anneal_target_c=560.0,
            soak_seconds=900.0,
        ),
        feeder=FeederLimits(
            gob_mass_target_g=186.0,
            gob_mass_tolerance_g=4.0,
            cut_window_ms=240.0,
            recover_gob_mass_g=172.0,
        ),
        mold=MoldLimits(
            close_min_force_kn=42.0,
            blow_pressure_kpa=320.0,
            blow_min_ms=180.0,
            reopen_delay_s=4.0,
        ),
        lehr=LehrLimits(
            belt_speed_m_per_min=8.4,
            belt_slow_m_per_min=4.2,
            heater_min_pct=18.0,
            heater_max_pct=92.0,
            anneal_zone_count=5,
        ),
        batch=BatchLimits(
            charge_batch_kg=640.0,
            scale_offset_max_kg=12.0,
            mix_tolerance_pct=1.5,
            charge_window_s=180.0,
        ),
        oxygen=OxygenTrimLimits(
            # 理论空燃比 10.0；固定配比按基准过剩空气系数 1.15 运行，
            # 对应残氧约 2.5%，修正回路再围绕它微调。
            ratio_air_per_gas=10.0,
            excess_air_base=1.15,
            # 修正系数只允许在 ±8% 内收，退回时回到 1.00（即固定配比）。
            trim_min=0.92,
            trim_max=1.08,
            trim_safe=1.00,
            o2_target_pct=3.0,
            o2_deadband_pct=0.3,
            o2_low_load_target_bias_pct=0.8,
            o2_min_load=0.4,
            o2_valid_min_pct=0.5,
            o2_valid_max_pct=12.0,
            # 单拍变化超过 1.0%/s 视为毛刺；一阶滤波时间常数 8 秒。
            o2_slew_pct_per_s=1.0,
            o2_freeze_noise_pct=0.05,
            o2_freeze_hold_s=30.0,
            filter_tau_s=8.0,
            # 慢收敛：每 1% 含氧偏差每分钟最多积 0.5% 修正量；
            # 加风（缺氧）方向每分钟最多 2%，减风方向最多 1%。
            integral_per_min=0.5,
            slew_up_pct_per_min=2.0,
            slew_down_pct_per_min=1.0,
            # 测点异常 20 秒先冻结；90 秒不恢复转固定配比并提示。
            hold_enter_s=20.0,
            fixed_after_s=90.0,
            recover_require_s=60.0,
            plausibility_tolerance_pct=1.5,
            cross_limiter_bias_pct=1.0,
            air_min_operating_m3_h=2000.0,
        ),
        pages=("overview", "operations", "records"),
    )


def config_to_mapping(cfg: PlatformConfig) -> dict:
    """把配置还原成可打印／可回写的映射。"""

    return {
        "platform": {
            "name": cfg.name,
            "project_id": cfg.project_id,
            "storage_root": cfg.storage_root,
        },
        "limits": {
            "furnace": {
                "level_target_mm": cfg.furnace.level_target_mm,
                "level_band_mm": cfg.furnace.level_band_mm,
                "melt_temp_target_c": cfg.furnace.melt_temp_target_c,
                "melt_temp_min_c": cfg.furnace.melt_temp_min_c,
                "melt_temp_max_c": cfg.furnace.melt_temp_max_c,
                "draft_target_pa": cfg.furnace.draft_target_pa,
            },
            "burner": {
                "air_min_flow_m3_h": cfg.burner.air_min_flow_m3_h,
                "gas_max_m3_h": cfg.burner.gas_max_m3_h,
                "ignition_timeout_s": cfg.burner.ignition_timeout_s,
                "zone_count": cfg.burner.zone_count,
                "flame_min_temp_c": cfg.burner.flame_min_temp_c,
            },
            "forehearth": {
                "stable_band_c": cfg.forehearth.stable_band_c,
                "stable_hold_s": cfg.forehearth.stable_hold_s,
                "gate_min_temp_c": cfg.forehearth.gate_min_temp_c,
                "gate_open_pct": cfg.forehearth.gate_open_pct,
            },
            "temp": {
                "anneal_alarm_c": cfg.temp.anneal_alarm_c,
                "anneal_clear_c": cfg.temp.anneal_clear_c,
                "anneal_target_c": cfg.temp.anneal_target_c,
                "soak_seconds": cfg.temp.soak_seconds,
            },
            "feeder": {
                "gob_mass_target_g": cfg.feeder.gob_mass_target_g,
                "gob_mass_tolerance_g": cfg.feeder.gob_mass_tolerance_g,
                "cut_window_ms": cfg.feeder.cut_window_ms,
                "recover_gob_mass_g": cfg.feeder.recover_gob_mass_g,
            },
            "mold": {
                "close_min_force_kn": cfg.mold.close_min_force_kn,
                "blow_pressure_kpa": cfg.mold.blow_pressure_kpa,
                "blow_min_ms": cfg.mold.blow_min_ms,
                "reopen_delay_s": cfg.mold.reopen_delay_s,
            },
            "lehr": {
                "belt_speed_m_per_min": cfg.lehr.belt_speed_m_per_min,
                "belt_slow_m_per_min": cfg.lehr.belt_slow_m_per_min,
                "heater_min_pct": cfg.lehr.heater_min_pct,
                "heater_max_pct": cfg.lehr.heater_max_pct,
                "anneal_zone_count": cfg.lehr.anneal_zone_count,
            },
            "batch": {
                "charge_batch_kg": cfg.batch.charge_batch_kg,
                "scale_offset_max_kg": cfg.batch.scale_offset_max_kg,
                "mix_tolerance_pct": cfg.batch.mix_tolerance_pct,
                "charge_window_s": cfg.batch.charge_window_s,
            },
            "oxygen": {
                "ratio_air_per_gas": cfg.oxygen.ratio_air_per_gas,
                "excess_air_base": cfg.oxygen.excess_air_base,
                "trim_min": cfg.oxygen.trim_min,
                "trim_max": cfg.oxygen.trim_max,
                "trim_safe": cfg.oxygen.trim_safe,
                "o2_target_pct": cfg.oxygen.o2_target_pct,
                "o2_deadband_pct": cfg.oxygen.o2_deadband_pct,
                "o2_low_load_target_bias_pct": cfg.oxygen.o2_low_load_target_bias_pct,
                "o2_min_load": cfg.oxygen.o2_min_load,
                "o2_valid_min_pct": cfg.oxygen.o2_valid_min_pct,
                "o2_valid_max_pct": cfg.oxygen.o2_valid_max_pct,
                "o2_slew_pct_per_s": cfg.oxygen.o2_slew_pct_per_s,
                "o2_freeze_noise_pct": cfg.oxygen.o2_freeze_noise_pct,
                "o2_freeze_hold_s": cfg.oxygen.o2_freeze_hold_s,
                "filter_tau_s": cfg.oxygen.filter_tau_s,
                "integral_per_min": cfg.oxygen.integral_per_min,
                "slew_up_pct_per_min": cfg.oxygen.slew_up_pct_per_min,
                "slew_down_pct_per_min": cfg.oxygen.slew_down_pct_per_min,
                "hold_enter_s": cfg.oxygen.hold_enter_s,
                "fixed_after_s": cfg.oxygen.fixed_after_s,
                "recover_require_s": cfg.oxygen.recover_require_s,
                "plausibility_tolerance_pct": cfg.oxygen.plausibility_tolerance_pct,
                "cross_limiter_bias_pct": cfg.oxygen.cross_limiter_bias_pct,
                "air_min_operating_m3_h": cfg.oxygen.air_min_operating_m3_h,
            },
        },
        "pages": list(cfg.pages),
    }
