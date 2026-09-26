"""模具成型：合模、吹气与灌模。"""

from .blow import BlowOutcome, BlowStation
from .close import Closure, MoldCloser
from .fill import FillOutcome, MoldFill

__all__ = ["BlowOutcome", "BlowStation", "Closure", "MoldCloser", "FillOutcome", "MoldFill"]
