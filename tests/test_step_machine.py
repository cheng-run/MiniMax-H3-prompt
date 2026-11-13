"""显式步骤机（D2 spec §4 / issue #216）：状态、闸门、合法转移与续跑起点。

本文件钉的是**声明本身**：哪两个状态之间可以走、下一步该干什么、修订从哪一格重跑。
不碰向导、不碰会话文件——那些在 ``test_wizard_base_step_machine.py`` 里按用户行为验。
"""
from __future__ import annotations

import pytest

from minimax_h3_prompt import step_machine as m
from minimax_h3_prompt.graph.pipeline import _STAGE1_CHAIN
from minimax_h3_prompt.user_revisions import LAYER_FRAME, LAYER_SPECS

CHAIN = tuple(_STAGE1_CHAIN)


# ---------------------------------------------------------------------------
# 状态/闸门单一声明
# ---------------------------------------------------------------------------

def test_every_declared_status_is_known_and_terminal_only_for_completed():
    """合法转移表里出现的状态/触发必须都是声明过的；只有「已交付」没有出边。"""
    referenced = {status for status, _trigger in m.LEGAL_TRANSITIONS}
    assert referenced <= set(m.STATUSES), f"表里有没声明的状态：{referenced - set(m.STATUSES)}"
    used_triggers = {trigger for _status, trigger in m.LEGAL_TRANSITIONS}
    assert used_triggers == set(m.TRIGGERS), \
        f"表里的触发与声明不一致：{used_triggers ^ set(m.TRIGGERS)}"

    outgoing = {status for status, _trigger in m.LEGAL_TRANSITIONS}
    assert set(m.STATUSES) - outgoing == set(m.TERMINAL_STATUSES), \
        "除终态外每个状态都要有出边，否则会出现「有状态、无去路」"


def test_can_advance_matches_the_spec_table():
    """spec §4.1 的「可 advance」列：created / failed / cancelled / interrupted。"""
    assert m.can_advance(m.STATUS_CREATED)
    assert m.can_advance(m.STATUS_FAILED)
    assert m.can_advance(m.STATUS_CANCELLED)
    assert m.can_advance(m.STATUS_INTERRUPTED)
    assert not m.can_advance(m.STATUS_RUNNING)
    assert not m.can_advance(m.STATUS_AWAITING_HUMAN)
    assert not m.can_advance(m.STATUS_COMPLETED)


@pytest.mark.parametrize("status,trigger", [
    (m.STATUS_CREATED, m.TRIGGER_STAGE1_DONE),
    (m.STATUS_AWAITING_HUMAN, m.TRIGGER_STAGE2_DONE),
    (m.STATUS_COMPLETED, m.TRIGGER_RETRY),
    (m.STATUS_RUNNING, m.TRIGGER_RESOLVE_APPROVE),
])
def test_illegal_transitions_raise_instead_of_defaulting(status, trigger):
    """没写进表里的组合必须报错：默认放行会让非法转移静默变成合法。"""
    with pytest.raises(m.InvalidTransition):
        m.next_status(status, trigger)


def test_the_happy_path_is_reachable_through_the_declaration():
    """created → running → awaiting_human → running → completed，全部走声明表。"""
    status = m.STATUS_CREATED
    status = m.next_status(status, m.TRIGGER_ADVANCE)
    assert status == m.STATUS_RUNNING
    status = m.next_status(status, m.TRIGGER_STAGE1_DONE)
    assert status == m.STATUS_AWAITING_HUMAN
    status = m.next_status(status, m.TRIGGER_RESOLVE_APPROVE)
    assert status == m.STATUS_RUNNING
    assert m.next_status(status, m.TRIGGER_STAGE2_DONE) == m.STATUS_COMPLETED


# ---------------------------------------------------------------------------
# 从各中断点续跑的起点判定
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("last_node,reason,node", [
    ("", m.RESUME_NO_CHECKPOINT, ""),
    ("不存在的节点", m.RESUME_UNKNOWN_NODE, ""),
    (CHAIN[-1], m.RESUME_EXHAUSTED, ""),
    ("parallel_visual", m.RESUME_FROM_NODE, "fl2va_frame_prompts"),
])
def test_stage1_resume_reasons(last_node, reason, node):
    action = m.stage1_resume(CHAIN, last_node)
    assert action.reason == reason
    assert action.node == node


def test_next_action_created_starts_stage1_from_scratch():
    action = m.next_action(m.Facts(status=m.STATUS_CREATED))
    assert action.step == m.STEP_STAGE1
    assert action.resume is not None and action.resume.reason == m.RESUME_NO_CHECKPOINT


def test_next_action_interrupted_resumes_stage1_from_the_next_node():
    action = m.next_action(m.Facts(status=m.STATUS_INTERRUPTED,
                                  last_completed_node="parallel_visual",
                                  stage1_chain=CHAIN))
    assert action.step == m.STEP_STAGE1
    assert action.resume is not None and action.resume.node == "fl2va_frame_prompts"


def test_next_action_exhausted_chain_asks_to_finish_stage1():
    action = m.next_action(m.Facts(status=m.STATUS_INTERRUPTED,
                                  last_completed_node=CHAIN[-1],
                                  stage1_chain=CHAIN))
    assert action.step == m.STEP_STAGE1
    assert action.resume is not None and action.resume.reason == m.RESUME_EXHAUSTED


def test_next_action_awaiting_human_with_unresolved_gate_points_at_the_gate():
    action = m.next_action(m.Facts(status=m.STATUS_AWAITING_HUMAN, gate_resolved=False))
    assert action.gate == m.ACTION_STAGE1_REVIEW
    assert action.step is None


def test_next_action_awaiting_human_with_resolved_gate_moves_to_stage2():
    action = m.next_action(m.Facts(status=m.STATUS_AWAITING_HUMAN, gate_resolved=True))
    assert action.step == m.STEP_STAGE2
    assert action.gate is None


def test_next_action_completed_is_terminal():
    assert m.next_action(m.Facts(status=m.STATUS_COMPLETED)).terminal


def test_next_action_refuses_running():
    """running 的含义是「有 job 正持有会话」：那时不该来问下一步（spec 的 409）。"""
    with pytest.raises(ValueError):
        m.next_action(m.Facts(status=m.STATUS_RUNNING))


# ---------------------------------------------------------------------------
# 修订后从声明层次重跑阶段 1
# ---------------------------------------------------------------------------

def test_approve_goes_straight_to_stage2():
    decision = m.resolve(m.STATUS_AWAITING_HUMAN, m.DECISION_APPROVE)
    assert decision.status == m.STATUS_RUNNING
    assert decision.step == m.STEP_STAGE2
    assert decision.rerun_from == ""


def test_revise_frame_layer_reruns_only_the_frame_prompt_node():
    decision = m.resolve(m.STATUS_AWAITING_HUMAN, m.DECISION_REVISE, layer=LAYER_FRAME)
    assert decision.status == m.STATUS_RUNNING
    assert decision.step == m.STEP_STAGE1
    assert decision.rerun_from == "fl2va_frame_prompts"


@pytest.mark.parametrize("layer", sorted(LAYER_SPECS))
def test_revise_setting_layer_reruns_from_its_declared_start(layer):
    """起点直接取 ``LAYER_SPECS``：本模块不另抄一份节点名，链一变不会两处不同步。"""
    decision = m.resolve(m.STATUS_AWAITING_HUMAN, m.DECISION_REVISE, layer=layer)
    assert decision.rerun_from == LAYER_SPECS[layer].resume_from


def test_revise_returns_to_the_same_gate():
    """修订跑完要回到**同一个人工闸门**：不是记得再停一次，而是声明里写死。"""
    decision = m.resolve(m.STATUS_AWAITING_HUMAN, m.DECISION_REVISE, layer=LAYER_FRAME)
    assert decision.gate == m.ACTION_STAGE1_REVIEW


def test_default_layer_is_the_frame_layer():
    """既有行为：不提层次＝只改画面（向导那条老路）。"""
    decision = m.resolve(m.STATUS_AWAITING_HUMAN, m.DECISION_REVISE)
    assert decision.rerun_from == "fl2va_frame_prompts"


def test_revision_rerun_from_rejects_unknown_layer():
    with pytest.raises(ValueError):
        m.revision_rerun_from("不存在的层次")


def test_unknown_decision_is_rejected():
    with pytest.raises(ValueError):
        m.resolve(m.STATUS_AWAITING_HUMAN, "maybe")


# ---------------------------------------------------------------------------
# 与既有真源的接线（形态从常量派生，别另抄一份）
# ---------------------------------------------------------------------------

def test_frame_prompt_node_is_on_the_stage1_chain():
    assert m.FRAME_PROMPT_NODE in CHAIN, \
        "画面级重跑的起点不在阶段 1 链上，续跑会从一格不存在的节点起跑"


@pytest.mark.parametrize("layer", sorted(LAYER_SPECS))
def test_setting_layer_starts_are_on_the_stage1_chain(layer):
    assert LAYER_SPECS[layer].resume_from in CHAIN


# ---------------------------------------------------------------------------
# D4-2：帧提交待办（spec §9）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("variant", ["FL2VA", "fl2va", "Fl2va"])
def test_frame_chain_variant_requires_a_frame_submission_gate(variant):
    assert m.needs_frame_submission(variant)


@pytest.mark.parametrize("variant", ["T2VA", "I2VA", "L2VA", "", "FL2V", " FL2VA "])
def test_other_variants_go_straight_to_stage2(variant):
    """非帧链不凭空多出一道待办；拼写错误也不该被当成帧链。"""
    assert not m.needs_frame_submission(variant)


def test_frame_submission_is_a_second_human_action_item_not_a_new_event_type():
    """第二个人工待办改的是 kind，不是事件类型：十种闭合由 EVENT_TYPES 守着。"""
    from minimax_h3_prompt.workbench_store import EVENT_TYPES

    assert m.ACTION_FRAME_SUBMISSION == "frame_submission"
    assert m.ACTION_FRAME_SUBMISSION != m.ACTION_STAGE1_REVIEW
    assert len(EVENT_TYPES) == 10
    assert "frame_submission" not in EVENT_TYPES


def test_submit_frames_is_an_internal_decision_value():
    """帧提交不是接口上的 decision 取值，但它要能被决议事件记住。"""
    assert m.DECISION_SUBMIT_FRAMES not in (m.DECISION_APPROVE, m.DECISION_REVISE)
    assert m.DECISION_SUBMIT_FRAMES == "submit_frames"


# ---------------------------------------------------------------------------
# D6-1：分段阶段标签（spec §5.2）
# ---------------------------------------------------------------------------

def test_segmenting_is_a_step_label_not_a_session_status():
    """``segmenting`` 只用于进度与任务记录；会话状态仍是既有七态（D6-AC1）。"""
    assert m.STEP_SEGMENTING == "segmenting"
    assert m.STEP_SEGMENTING in m.STEPS
    assert set(m.STEPS) == {m.STEP_STAGE1, m.STEP_STAGE2, m.STEP_SEGMENTING}
    assert m.STEP_SEGMENTING not in m.STATUSES
    assert len(m.STATUSES) == 7


def test_progress_stages_derive_from_the_step_labels():
    """进度事件允许的 stage 从步骤常量派生：加标签时不用再改第二份表。"""
    from minimax_h3_prompt.workbench_store import _PROGRESS_STAGES

    assert _PROGRESS_STAGES == frozenset(m.STEPS)
    # 派生之外还要钉住「新标签真的被接受」：只写等式的话，两份表一起漏掉它仍然绿。
    assert m.STEP_SEGMENTING in _PROGRESS_STAGES


@pytest.mark.parametrize("duration,expected", [
    (4, False), (10, False), (10.0, False), (10.1, True), (12, True), (60, True),
])
def test_long_form_threshold_is_duration_over_ten_seconds(duration, expected):
    """长片是产品阈值（>10 秒），不是领域概念；只在一处判定（spec §3）。"""
    assert m.is_long_form(duration) is expected
