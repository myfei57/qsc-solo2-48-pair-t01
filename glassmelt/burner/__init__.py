"""燃烧器：助燃风、点火顺序、氧量校正与分区。"""

from .air import AirState, CombustionAir
from .draft import air_closed_before_draft_stop, close_combustion_air
from .ignite import IgnitionPhase, IgnitionUnit
from .o2trim import O2TrimController, TrimMode, TrimState
from .zone import ZoneMap

__all__ = [
    "AirState",
    "CombustionAir",
    "IgnitionPhase",
    "IgnitionUnit",
    "O2TrimController",
    "TrimMode",
    "TrimState",
    "ZoneMap",
    "air_closed_before_draft_stop",
    "close_combustion_air",
]
