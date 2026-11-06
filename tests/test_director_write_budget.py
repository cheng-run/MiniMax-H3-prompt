"""导演台写段遵守段内素材预算（#188）：子集 ⊆ 预算，差值只在清单上说一句。

真实事故（#180）：第 5 段的预算说 9 张，写段却挂了 10 张——第 10 张是定义块里的
一致性锚句沿闭包拉进来的 ``<Picture 22>``，正文根本没拍它。规划与写段各推一套口径，
直到打包才以一句不点名的话出现。

缝（全部是票面点名的公开入口）：

1. ``build_segment_request``——请求里给**可执行的预算清单**，不是一句抽象约束。
2. ``write_director_segments``——子集超出预算 = 一条校验错误，进有界重写；重试用尽
   由 #185 的硬失败接住。子集少于预算不阻断；没有预算文件的旧会话不走这条校验。
3. ``render_written_listing``——出站清单在该段下写「本段预算 N、实用 M」。

断言只碰外部可观察行为：请求字符串里的关键行、落盘 JSON、异常文案。
"""
from __future__ import annotations

import json

import pytest

from minimax_h3_prompt.director.planning import (
    AssetBudget,
    BudgetPicture,
    SegmentBudget,
    write_asset_budget,
)

# 真实事故的两个主体：父亲（出镜）与前院/车道（只作一致性锚）。写段正文点名这两张
# 就是「挂了预算外的图」——预算只给了父亲。
_DEFINITIONS = {
    "subject_definitions": (
        "<Subject 1> is the father. His appearance comes from <Picture 1>.\n"
        "<Subject 2> is the front yard and the family car, whose view comes "
        "from <Picture 22>."),
    "retention_analysis": (
        "<Picture 1>: fully_preserved.\n"
        "<Picture 22>: fully_preserved."),
}
_REGISTRY = ((1, "父亲_综合角色板"), (22, "BG05_住宅前院与家庭车辆车道"))
_BUDGET = SegmentBudget(pictures=(BudgetPicture(number=1, name="父亲_综合角色板"),))


def _segment(index, segment_type, start, end, declared, export, guided):
    from minimax_h3_prompt.director.frames import DirectorSegment

    return DirectorSegment(
        index=index, type=segment_type, guided=guided,
        start_frame=start, end_frame=end,
        declared_frames=declared, export_frames=export,
        summary=f"段 {index} 剧情", end_hook=f"段 {index} 落定画面",
        give_images=(),
    )


def _plan():
    from minimax_h3_prompt.director.frames import DirectorPlan

    return DirectorPlan(mode="r2v", total_frames=107,
                        segments=(_segment(1, "opening", 0, 107, 107, 107, False),))


def _two_segment_plan():
    from minimax_h3_prompt.director.frames import DirectorPlan

    return DirectorPlan(mode="r2v", total_frames=214,
                        segments=(_segment(1, "opening", 0, 107, 107, 107, False),
                                  _segment(2, "continue", 107, 214, 107, 107, True)))


def _reply(plan, pictures, segment=None):
    """写段回复：正文点名给定的原号（正例会引用预算内的图）。"""
    from minimax_h3_prompt.director.prompts import render_timeline_note

    segment = segment or plan.segments[0]
    note = render_timeline_note(plan, segment)
    refs = ", ".join(f"<Picture {number}>" for number in pictures)
    return (
        "subject_definitions:\nplaceholder\n\n"
        "summary:\n[reference generation] the father waits in the entryway.\n\n"
        "retention_analysis:\nplaceholder\n\n"
        "detailed_description:\n"
        f"{note}\n"
        f"[Shot 1] {refs} stands in the entryway as the door opens.\n\n"
        "overall_soundscape:\nRain taps the roof.\n\n"
        "non_diegetic_music:\nN/A"
    )


class _ScriptedLLM:
    """按调用序号回放回复；记录每次出站请求（断言「模型看到了什么」）。"""

    def __init__(self, replies) -> None:
        self.replies = list(replies)
        self.requests: list[str] = []

    def invoke(self, request, config=None):
        self.requests.append(str(request))
        index = min(len(self.requests) - 1, len(self.replies) - 1)
        return self.replies[index]


def _assets_by_original(session_dir):
    from minimax_h3_prompt.director.prompts import load_segment_assets

    entries = load_segment_assets(session_dir)[1]
    return [row["original"] for row in entries]


# ---------------------------------------------------------------------------
# 写段请求：预算清单必须可执行（原号 = 语义名），不是一句抽象约束
# ---------------------------------------------------------------------------

def test_request_carries_the_budget_as_an_executable_checklist():
    """模型拿到的是一份「哪几张能挂」的清单，而不是「别超 9 张」这种靠自觉的话。"""
    from minimax_h3_prompt.director.prompts import build_segment_request

    plan = _plan()
    request = build_segment_request(
        plan, plan.segments[0], sections=_DEFINITIONS, budget=_BUDGET)

    assert "<Picture 1> = 父亲_综合角色板" in request, \
        "预算清单要写「原号 = 语义名」，模型才按真实登记挑图"
    assert "本段预算清单就是上限" in request, "票面要求写明的上限口径（#188 AC4）"
    assert "清单外" in request, "要说清清单外的图不许挂"
    # 只核**预算清单那一段**：定义块本身要原样下发（含锚图），那不是可挂清单。
    checklist = request.split("【本段参考图预算清单】", 1)[1].split("## 词表切片", 1)[0]
    assert "<Picture 22>" not in checklist, \
        "预算外的图（锚图）不该出现在可执行清单里"


def test_request_without_a_budget_keeps_the_generic_slot_limit():
    """旧会话没有预算：请求照旧只有「每段最多 9 张」，不凭空造一份空清单。"""
    from minimax_h3_prompt.director.prompts import build_segment_request

    plan = _plan()
    request = build_segment_request(plan, plan.segments[0], sections=_DEFINITIONS)

    assert "本段预算清单就是上限" not in request
    assert "本段参考图预算" in request and "9 张" in request


# ---------------------------------------------------------------------------
# 写段：超出预算 = 校验错误进有界重写；修好就正常落盘
# ---------------------------------------------------------------------------

def test_budget_violation_is_written_back_and_the_fix_lands(tmp_path):
    """第一版挂了预算外的 ``<Picture 22>``，被退回后只用 ``<Picture 1>`` —— 真实现场。

    预算从**盘上**读（向导在写段之前已由 #186 落盘），不靠调用方再传一遍。
    """
    from minimax_h3_prompt.director import prompts

    plan = _plan()
    session_dir = tmp_path / "GEN001"
    write_asset_budget(session_dir, AssetBudget(segments={1: _BUDGET}))

    llm = _ScriptedLLM([_reply(plan, (1, 22)), _reply(plan, (1,))])
    written = prompts.write_director_segments(
        plan, llm=llm, session_dir=session_dir,
        sections=_DEFINITIONS, registry=_REGISTRY)

    assert len(llm.requests) == 2, "预算违规必须退回重写，而不是照写"
    assert "DIRECTOR_BUDGET_VIOLATION" in llm.requests[1], \
        "重写请求要带着预算违规这条问题清单"
    assert "<Picture 22>" in llm.requests[1], "点名多出来的那张图"

    assert written[0].issues == ()
    assert _assets_by_original(session_dir) == [1], \
        "落盘的子集只能有预算内的图"
    assert prompts.segment_path(session_dir, 1).is_file()


def test_subset_smaller_than_budget_is_not_blocked_and_the_listing_shows_the_gap(tmp_path):
    """预算 2 张、实用 1 张：不阻断，只在出站清单提一句差值（§180 决定 14）。"""
    from minimax_h3_prompt.director import prompts

    plan = _plan()
    session_dir = tmp_path / "GEN001"
    budget = SegmentBudget(pictures=(
        BudgetPicture(number=1, name="父亲_综合角色板"),
        BudgetPicture(number=22, name="BG05_住宅前院与家庭车辆车道"),
    ))
    write_asset_budget(session_dir, AssetBudget(segments={1: budget}))

    written = prompts.write_director_segments(
        plan, llm=_ScriptedLLM([_reply(plan, (1,))]), session_dir=session_dir,
        sections=_DEFINITIONS, registry=_REGISTRY)

    assert written[0].issues == ()
    assert _assets_by_original(session_dir) == [1]

    listing = prompts.render_written_listing(plan, written, budget=AssetBudget(
        segments={1: budget}))
    assert "本段预算 2、实用 1" in listing


def test_budget_violation_that_never_heals_is_rejected_not_written(tmp_path):
    """重试用尽仍挂预算外的图：硬失败点名段号与那张图，坏段只留 ``.rejected.md``。"""
    from minimax_h3_prompt.director import prompts

    plan = _plan()
    session_dir = tmp_path / "GEN001"
    write_asset_budget(session_dir, AssetBudget(segments={1: _BUDGET}))

    with pytest.raises(prompts.DirectorSegmentRejectedError) as excinfo:
        prompts.write_director_segments(
            plan, llm=_ScriptedLLM([_reply(plan, (1, 22))]), session_dir=session_dir,
            sections=_DEFINITIONS, registry=_REGISTRY)

    message = str(excinfo.value)
    assert "第 1 段" in message, "失败信息必须点名段号"
    assert "DIRECTOR_BUDGET_VIOLATION" in message
    assert "<Picture 22>" in message, "失败信息必须点名多出来的那张图"
    assert not prompts.segment_path(session_dir, 1).exists(), "坏段不许写正式文件名"
    assert prompts.rejected_segment_path(session_dir, 1).is_file()
    assert not prompts.segment_assets_path(session_dir).exists(), \
        "硬失败的段不许留下子集记录（段词与挂图会不同源）"


def test_old_session_without_a_budget_file_skips_the_budget_check(tmp_path):
    """没有 ``asset-budget.json`` 的旧会话照旧写：预算校验由打包期出声（#189）。"""
    from minimax_h3_prompt.director import prompts

    plan = _plan()
    session_dir = tmp_path / "GEN_OLD"

    written = prompts.write_director_segments(
        plan, llm=_ScriptedLLM([_reply(plan, (1, 22))]), session_dir=session_dir,
        sections=_DEFINITIONS, registry=_REGISTRY)

    assert written[0].issues == (), "旧会话没有预算可比，不该报预算违规"
    assert _assets_by_original(session_dir) == [1, 22], "旧会话照子集挂图"


def test_listing_omits_the_budget_line_when_there_is_no_budget(tmp_path):
    """没有预算就不写「本段预算」行（否则每段凭空多一句谎话）。"""
    from minimax_h3_prompt.director import prompts

    plan = _plan()
    session_dir = tmp_path / "GEN_OLD"
    written = prompts.write_director_segments(
        plan, llm=_ScriptedLLM([_reply(plan, (1,))]), session_dir=session_dir,
        sections=_DEFINITIONS, registry=_REGISTRY)

    listing = prompts.render_written_listing(plan, written)
    assert "本段预算" not in listing


def test_failed_budget_check_does_not_leave_a_partial_assets_table(tmp_path):
    """预算违规且重试用尽时，段内素材子集表一个字节都不写（同上：不同源）。"""
    from minimax_h3_prompt.director import prompts

    plan = _plan()
    session_dir = tmp_path / "GEN001"
    write_asset_budget(session_dir, AssetBudget(segments={1: _BUDGET}))

    with pytest.raises(prompts.DirectorSegmentRejectedError):
        prompts.write_director_segments(
            plan, llm=_ScriptedLLM([_reply(plan, (1, 22))]), session_dir=session_dir,
            sections=_DEFINITIONS, registry=_REGISTRY)

    assert not prompts.segment_assets_path(session_dir).exists()


def test_budget_line_counts_the_mounted_subset_not_the_pictures_in_the_text(tmp_path):
    """「实用 M」= 真正挂上去的子集张数（来自写段期的子集记录），不是正文里出现的标签数。"""
    from minimax_h3_prompt.director import prompts

    plan = _plan()
    session_dir = tmp_path / "GEN001"
    write_asset_budget(session_dir, AssetBudget(segments={1: _BUDGET}))
    written = prompts.write_director_segments(
        plan, llm=_ScriptedLLM([_reply(plan, (1,))]), session_dir=session_dir,
        sections=_DEFINITIONS, registry=_REGISTRY)

    assert written[0].used_pictures == 1
    assert json.loads(prompts.segment_assets_path(session_dir).read_text(
        encoding="utf-8"))["1"][0]["name"] == "父亲_综合角色板"


def test_segments_written_before_a_hard_failure_keep_their_subset_record(tmp_path):
    """第 2 段硬失败时，第 1 段的子集记录必须已经落盘（#190 复审）。

    改动前子集表只在循环结束后整份写：第 K 段失败会连前面已写段的记录一起丢，
    打包期照空表挂图——「段词在、挂图不在」，而且不报错。
    """
    from minimax_h3_prompt.director import prompts

    plan = _two_segment_plan()
    session_dir = tmp_path / "GEN001"
    # 第 2 段的预算只给父亲；它的回复照旧点了前院/车道 → 子集超预算，重试用尽即硬失败。
    write_asset_budget(session_dir, AssetBudget(segments={2: _BUDGET}))

    with pytest.raises(prompts.DirectorSegmentRejectedError):
        prompts.write_director_segments(
            plan,
            llm=_ScriptedLLM([_reply(plan, (1,)), _reply(plan, (1, 22), segment=plan.segments[1])]),
            session_dir=session_dir, sections=_DEFINITIONS, registry=_REGISTRY)

    assets = prompts.load_segment_assets(session_dir)
    assert set(assets) == {1}, "已写出的第 1 段子集必须先落盘；失败的第 2 段不许留记录"
    assert [row["original"] for row in assets[1]] == [1]
    assert prompts.segment_path(session_dir, 1).is_file()
    assert not prompts.segment_path(session_dir, 2).exists()
