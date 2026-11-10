"""base 短视频的步骤机驱动（D2-2 / issue #216）。

向导只保留「问步骤机下一步是什么，然后替它问用户」：顺序、闸门与续跑起点由
``step_machine`` 单一声明，这里负责把动作接到**既有的**执行器上。执行器是那几份
已经被既有测试钉住的向导函数，行为一字不改——本票买的是「顺序只有一份」，不是重写流程。

范围两处明写：

- 只覆盖 **base 短视频**：``ref`` / ``director`` / 长片分段陪跑是 D6–D8 的范围，
  这里只负责把变了路线的手交回去（``finish_non_base``）。
- 执行器经 ``wizard`` 模块**在调用时**取，不提前绑成参数：既有测试都在 ``wizard``
  模块上打桩，提前绑会把那些桩绕过去。
"""
from __future__ import annotations

from typing import Any

from .. import step_machine
from ..brief_parser import Brief
from ..session_store import (
    STATUS_AWAITING_FRAMES,
    STATUS_COMPLETED,
    STATUS_STAGE1_RUNNING,
)

# 短视频门槛与 ``_phase2_collect_and_finish`` 里的 ``duration > 10`` 同口径。
# 长片下一段就是分段陪跑，那是 D6–D8 的范围，不经这台步骤机。
SHORT_FORM_MAX_SECONDS = 10.0

# 向导的落盘状态 → 步骤机（D2 §4.1）的状态。两边刻意不合并：落盘格式是向导的既有
# 契约，步骤机是 spec 的词汇，混成一套会让「改一处」变成「改两条契约」。
_SESSION_STATUS_TO_STEP = {
    STATUS_AWAITING_FRAMES: step_machine.STATUS_AWAITING_HUMAN,
    STATUS_STAGE1_RUNNING: step_machine.STATUS_INTERRUPTED,
    STATUS_COMPLETED: step_machine.STATUS_COMPLETED,
}


def skips_base_frame_loop(brief: Brief) -> bool:
    """这条路线是否**不走** base 的人机修改循环（锚在 fl2va 首尾帧提示词上的那一份）。

    新跑收尾与两条续接入口共用这一个判据（issue #41 多参、issue #101 导演台）。四处
    各写一份「是不是 ref/是不是 director」的代价是：加第三条路线时总有一个入口漏改，
    表现成那条路线上「重启后忽然要你交帧图」——本仓「改一处漏一处」的老形态。
    """
    return brief.is_ref or brief.is_director


def is_base_short_form(brief: Brief) -> bool:
    """base 路线且不是长片：步骤机今天负责的唯一形态。"""
    return brief.mode == "base" and brief.duration <= SHORT_FORM_MAX_SECONDS


def session_status_to_step(status: str) -> str:
    """向导落盘状态 → 步骤机状态；不在表里就报错（静默兜底会让两条词汇悄悄错位）。"""
    try:
        return _SESSION_STATUS_TO_STEP[status]
    except KeyError:
        raise ValueError(f"步骤机接不了这个会话状态：{status!r}") from None


def facts_from_session(session: Any, *, frames_shown: bool) -> step_machine.Facts:
    """从会话对象抽**事实**：步骤机只吃这些，不碰会话文件也不认识落盘格式。

    ``frames_shown`` 沿用向导既有的语义：本进程里人工闸门是否已经问过。它就是
    「闸门已 resolve、下一步该进阶段 2」那条判据，不再另造一个标志。
    """
    from ..graph.pipeline import _STAGE1_CHAIN

    progress = session.stage_state.get("_progress") or {}
    return step_machine.Facts(
        status=session_status_to_step(session.status),
        last_completed_node=str(progress.get("last_completed_node", "")),
        gate_resolved=frames_shown,
        stage1_chain=tuple(_STAGE1_CHAIN),
    )


def finish_non_base(wizard: Any, config: Any, session: Any, *, frames_shown: bool) -> int:
    """原分派尾部（导演台 → 多参 → base）：路线在「整个重来」里变了时也回到这里。

    顺序不可换：导演台**必须早于** base 的收尾，否则第三条路线会被静默塞进首尾帧链
    的阶段 2（收帧图、写 fl2va 正文），比直接报错更难发现（issue #101 的现场）。
    """
    if session.brief.is_director:
        return wizard._run_director_flow(session)
    if session.brief.mode == "ref":
        return wizard._phase2_ref_finish(config, session)
    return wizard._phase2_collect_and_finish(
        config, session, show_frame_prompts=not frames_shown)


def drive(wizard: Any, config: Any, session: Any, *, frames_shown: bool) -> int:
    """让步骤机领着走，直到终态、或路线不再是 base 短视频。

    每一步都是「问一次、做一步」：闸门由步骤机指出、起点由步骤机算、阶段 2 由步骤机
    放行。执行器返回 ``None`` 表示用户中途放弃（向导原语义），直接以退出码 1 收场。
    """
    while True:
        action = step_machine.next_action(
            facts_from_session(session, frames_shown=frames_shown))
        if action.terminal:
            return 0
        if action.gate == step_machine.ACTION_STAGE1_REVIEW:
            session = wizard._resume_awaiting_frames(config, session)
        elif action.step == step_machine.STEP_STAGE1:
            if action.status == step_machine.STATUS_CREATED:
                session = wizard._phase1_new(config)
            else:
                session = wizard._resume_stage1(config, session)
        elif action.step == step_machine.STEP_STAGE2:
            return wizard._phase2_collect_and_finish(
                config, session, show_frame_prompts=not frames_shown)
        else:
            return finish_non_base(wizard, config, session, frames_shown=frames_shown)
        if session is None:
            return 1
        frames_shown = True
        if not is_base_short_form(session.brief):
            # 修订里的「整个流程重来」会重新问路线：走到别的路线上就交回原分派，
            # 绝不让另一条路线被塞进 base 的阶段 2（issue #101 的那条教训）。
            return finish_non_base(wizard, config, session, frames_shown=frames_shown)


__all__ = [
    "SHORT_FORM_MAX_SECONDS",
    "drive",
    "facts_from_session",
    "finish_non_base",
    "is_base_short_form",
    "session_status_to_step",
    "skips_base_frame_loop",
]
