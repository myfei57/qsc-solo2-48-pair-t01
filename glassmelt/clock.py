"""时钟抽象。

生产环境使用 :class:`SystemClock`（真实挂钟 + 单调时钟）；校准、复现与
单元验证使用 :class:`ManualClock`，让稳温保持、退火恒温段和网带提速在不
真实等待的前提下推进。稳温保持时间与点火超时必须能被显式推进，否则这些
顺序约束在验证环境里无法被观察。
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod


class Clock(ABC):
    """时钟基类，只实现各实现共享的格式化逻辑。"""

    @abstractmethod
    def now(self) -> float:
        """返回 Unix 时间戳（秒）。"""

    @abstractmethod
    def monotonic(self) -> float:
        """返回单调递增秒数，用于测量间隔。"""

    @abstractmethod
    def sleep(self, seconds: float) -> None:
        """等待给定秒数。"""

    def utc_iso(self) -> str:
        """把当前时间格式化成审计日志使用的时间戳。"""

        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.now()))


class SystemClock(Clock):
    """真实时钟。"""

    def now(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


class ManualClock(Clock):
    """可手动推进的时钟，``sleep`` 直接推进自身时间。"""

    def __init__(self, start: float = 1_756_000_000.0) -> None:
        self._now = float(start)
        self._mono = 0.0

    def now(self) -> float:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)

    def advance(self, seconds: float) -> float:
        """把两个时间轴同时推进 ``seconds`` 秒，返回推进后的挂钟时间。"""

        if seconds < 0:
            raise ValueError("时钟不能回退")
        self._now += seconds
        self._mono += seconds
        return self._now
