"""停炉时的助燃风与排烟风机顺序。

停炉的关闭顺序是「助燃风先关 → 排烟风机后停」。风机停机动作在炉压侧
还要现场核对一次助燃风状态；缺了这道核对，炉膛就会被推成正压。这里
把两步的真实执行顺序落到总线，事后按总线记录自查。
"""

from __future__ import annotations

from ..eventbus import Event, EventBus
from .air import CombustionAir

SHUTDOWN_ORDER = ("combustion_air_close", "draft_stop")


def close_combustion_air(air: CombustionAir, draft, bus: EventBus) -> dict:
    """按停机顺序先关助燃风，再停排烟风机，返回执行记录。"""

    executed: list[str] = []
    air_state = air.close()
    executed.append("combustion_air_close")
    bus.publish(
        Event.of(
            "burner",
            "burner",
            "shutdown_step",
            subject="combustion_air_close",
            step_index=len(executed),
            air_epoch=air_state.epoch,
        )
    )
    draft.stop(air.state())
    executed.append("draft_stop")
    bus.publish(
        Event.of(
            "burner",
            "burner",
            "shutdown_step",
            subject="draft_stop",
            step_index=len(executed),
        )
    )
    return {"steps": executed, "air": air.state().as_dict(), "draft": draft.snapshot()}


def air_closed_before_draft_stop(bus: EventBus) -> list[str]:
    """按总线记录自查停机顺序，返回违规说明（空表示合规）。"""

    steps: list[str] = []
    for event in bus.history("burner"):
        if event.action == "shutdown_step":
            steps.append(str(event.payload.get("subject", "")))
    violations: list[str] = []
    for earlier, later in zip(SHUTDOWN_ORDER, SHUTDOWN_ORDER[1:]):
        if earlier not in steps or later not in steps:
            violations.append(f"停机顺序缺少步骤：{earlier} 或 {later} 未执行")
            continue
        if steps.index(earlier) > steps.index(later):
            violations.append(f"停机顺序错误：实际先执行了 {later}")
    if steps and steps[0] != SHUTDOWN_ORDER[0]:
        violations.append(f"停机首个动作必须是 {SHUTDOWN_ORDER[0]}")
    return violations
