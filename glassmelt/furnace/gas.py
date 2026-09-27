"""燃气阀。

燃气阀是爆炸风险的直接来源：助燃风没有建立之前开阀，炉膛里会先积一团
可燃气体，点火瞬间爆燃。因此开阀动作只接受一份"助燃风已建立"的现场状态，
不接受任何长期有效的推断。
"""

from __future__ import annotations

import threading

from ..errors import InterlockError
from ..eventbus import Event, EventBus


class GasValve:
    """燃气阀本体。"""

    def __init__(self, bus: EventBus, gas_max_m3_h: float) -> None:
        self._bus = bus
        self._max = float(gas_max_m3_h)
        self._flow_m3_h = 0.0
        self._open = False
        self._opens = 0
        self._lock = threading.RLock()

    @property
    def is_open(self) -> bool:
        with self._lock:
            return self._open

    @property
    def flow_m3_h(self) -> float:
        with self._lock:
            return self._flow_m3_h

    def open(self, air_state, fraction: float = 0.6) -> float:
        """在助燃风已建立的条件下开阀，返回燃气流量。"""

        if not getattr(air_state, "established", False):
            raise InterlockError("助燃风未建立，禁止开启燃气阀", component="furnace")
        with self._lock:
            self._flow_m3_h = self._max * max(0.0, min(1.0, float(fraction)))
            self._open = True
            self._opens += 1
            flow = self._flow_m3_h
        self._bus.publish(
            Event.of(
                "furnace",
                "furnace",
                "gas_opened",
                subject="gas_valve",
                severity="notice",
                flow_m3_h=round(flow, 2),
                air_epoch=getattr(air_state, "epoch", -1),
            )
        )
        return flow

    def throttle(self, flow_m3_h: float, air_state) -> float:
        """运行中按配比需求调整燃气流量；风路必须仍在运行。"""

        if getattr(air_state, "flow_m3_h", 0.0) <= 0.0:
            raise InterlockError("助燃风已关闭，禁止调节燃气", component="furnace")
        with self._lock:
            if not self._open:
                raise InterlockError("燃气阀未开启，禁止调节流量", component="furnace")
            self._flow_m3_h = self._max * max(
                0.0, min(1.0, float(flow_m3_h) / self._max)
            )
            return self._flow_m3_h

    def close(self) -> float:
        """关阀，返回关阀前的流量。"""

        with self._lock:
            previous = self._flow_m3_h
            self._flow_m3_h = 0.0
            self._open = False
        self._bus.publish(
            Event.of("furnace", "furnace", "gas_closed", subject="gas_valve")
        )
        return previous

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "open": self._open,
                "flow_m3_h": round(self._flow_m3_h, 2),
                "max_m3_h": self._max,
                "opens": self._opens,
            }
