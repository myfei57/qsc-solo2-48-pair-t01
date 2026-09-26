"""燃烧器：助燃风、点火顺序与分区。"""

from .air import AirState, CombustionAir
from .draft import air_closed_before_draft_stop, close_combustion_air
from .ignite import IgnitionPhase, IgnitionUnit
from .zone import ZoneMap

__all__ = [
    "AirState",
    "CombustionAir",
    "IgnitionPhase",
    "IgnitionUnit",
    "ZoneMap",
    "air_closed_before_draft_stop",
    "close_combustion_air",
]
