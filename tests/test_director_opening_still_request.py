"""#322 写段请求与规划期注入：开场首帧图（``assets/01/start``）的画面事实。

缝（都是外部可观察行为）：

1. ``build_segment_request`` 的出站文本——开场段在**有首帧槽**的模式（i2v / fl2v）
   带「本段起点画面」块；其它模式、其它段类型、空描述、盘上无图时一个字不多；
2. ``write_director_segments`` 的出站请求——组装函数会拼 ≠ 调用方传了（本仓已被咬
   过多次的形态）；
3. ``plan_director_segments`` 的出站请求——规划师也看到同一份起点画面事实，措辞与
   写段侧同源、只差用途半句。

判据一律从给图契约派生（``frames.needs_first_frame_image``）：本文件的参数化矩阵
自己也算一遍这份契约，但**期望值的真源是票面那张表**——「只有开场段 × i2v/fl2v」。
"""
from __future__ import annotations

import json
import re

from minimax_h3_prompt.director.frames import (
    FIRST_SEGMENT_TYPE,
    PLAN_MODES,
    SEGMENT_TYPES,
    DirectorPlan,
    DirectorSegment,
    give_images_for,
    is_guided,
    needs_first_frame_image,
    segment_types_for_mode,
)

import pytest

_DESCRIPTION = "雨夜便利店门口，女人站在檐下，左手撑着黑伞，窗户在暗处亮着暖光。"

# r2v 的定义块是写段请求的必填项（六节外壳的第 1、3 节逐字取它），照既有用例的
# 样本给一份最小的。
_SUBJECT_DEFS = """subject_definitions:
<Picture 1> is a woman in a grey trench coat holding a black umbrella.

retention_analysis:
<Picture 1>: fully_preserved."""


def _request(plan, segment, **kwargs) -> str:
    """矩阵用例的统一入口：r2v 自动补定义块，其它模式与真实调用点同形。"""
    from minimax_h3_prompt.director.prompts import build_segment_request, split_ref_sections

    if plan.mode == "r2v":
        kwargs.setdefault("sections", split_ref_sections(_SUBJECT_DEFS))
    return build_segment_request(plan, segment, **kwargs)


def _segment(index: int, seg_type: str, mode: str, *,
             start_frame: int = 0, export_frames: int = 107) -> DirectorSegment:
    """一段合法的段表条目：给图清单从契约取，不手写字面量。"""
    guided = is_guided(seg_type)
    declared = export_frames - 12 if guided else export_frames
    return DirectorSegment(
        index=index, type=seg_type, guided=guided, start_frame=start_frame,
        end_frame=start_frame + export_frames, declared_frames=declared,
        export_frames=export_frames, summary=f"段 {index} 剧情概述",
        end_hook=f"段 {index} 落定画面",
        give_images=give_images_for(seg_type, mode))


def _single(seg_type: str, mode: str) -> tuple[DirectorPlan, DirectorSegment]:
    segment = _segment(1, seg_type, mode)
    return DirectorPlan(mode=mode, total_frames=segment.end_frame,
                        segments=(segment,)), segment


def test_fl2v_opening_request_carries_the_start_frame_fact():
    """fl2v 开场段：用户交的开场首帧图被读懂成什么，写段请求里要看得到。"""
    from minimax_h3_prompt.director.opening_still import OPENING_STILL_BLOCK_HEAD

    plan, segment = _single("opening", "fl2v")
    request = _request(plan, segment, start_frame=True,
                       start_frame_description=_DESCRIPTION)

    assert OPENING_STILL_BLOCK_HEAD in request, "开场段请求没带起点画面块"
    assert _DESCRIPTION in request, "描述正文没进请求"
    assert "冲突时以画面为准" in request, "没写清与分镜表/剧情概述冲突时以画面为准"


def test_single_segment_fl2v_opening_carries_start_and_end_blocks_together():
    """单段 fl2v：整片就是这一段，起点与终点两块必须并存、互不吞掉。

    位置按 spec §5 排：起点块（本段起点是哪）→ 起点画面块（起点画面是什么）→
    用户修订 → 终点块。同一件事的文字事实与画面事实相邻。
    """
    from minimax_h3_prompt.director.film_end import FILM_END_BLOCK_HEAD
    from minimax_h3_prompt.director.opening_still import OPENING_STILL_BLOCK_HEAD

    film_end_description = "终点画面：男人站在院门里，手里提着灯。"
    revision = "修订：把黑伞换成红伞。"
    plan, segment = _single("opening", "fl2v")
    request = _request(
        plan, segment, film_end=True, film_end_description=film_end_description,
        start_frame=True, start_frame_description=_DESCRIPTION,
        revision_context=revision)

    for head in (OPENING_STILL_BLOCK_HEAD, FILM_END_BLOCK_HEAD):
        assert request.count(head) == 1, f"块标题「{head}」必须出现且只出现一次"
    assert _DESCRIPTION in request and film_end_description in request, \
        "两块各自的描述必须都在：一块出现就吞掉另一块，等于用户白给了一张图"
    # 给图清单说这一段的起终点都有图——与两条画面事实块说的是同一件事。
    assert "本段给图" in request and "首帧" in request and "尾帧" in request

    order = [request.index(text) for text in (
        "【本段起点】", OPENING_STILL_BLOCK_HEAD, revision, FILM_END_BLOCK_HEAD)]
    assert order == sorted(order), "块顺序应为 起点 → 起点画面 → 用户修订 → 终点"
    # 两块之间不许出现互相打架的指称：起点块说第一帧、终点块说最后一帧。
    assert "本段第一帧" in request and "最后一帧" in request


def test_no_block_without_the_image_on_disk_or_without_a_description():
    """盘上无图、描述为空（含纯空白）都不产生块——「默认不改变任何东西」的底线。"""
    from minimax_h3_prompt.director.opening_still import OPENING_STILL_BLOCK_HEAD

    plan, segment = _single("opening", "fl2v")
    cases = {
        "盘上无图": dict(start_frame=False, start_frame_description=_DESCRIPTION),
        "描述为空": dict(start_frame=True, start_frame_description=""),
        "描述纯空白": dict(start_frame=True, start_frame_description=" \n\t "),
    }
    for label, kwargs in cases.items():
        request = _request(plan, segment, **kwargs)
        assert OPENING_STILL_BLOCK_HEAD not in request, f"{label}时不该出现起点画面块"


def test_default_parameters_are_exactly_the_pre_change_request():
    """新形参的默认值必须是「没有这张图、没有描述」：默认路径与改前调用同一条。

    逐字节对照另有一份离线证据（改前/改后同一批入参的请求文本 diff）；这里钉住
    默认值本身——把默认改成 True，所有既有调用点都会静默多出一块。
    """
    for mode in ("t2v", "i2v", "fl2v", "r2v"):
        plan, segment = _single("opening", mode)
        default = _request(plan, segment)
        explicit = _request(plan, segment, start_frame=False,
                            start_frame_description="")
        assert default == explicit


# 票面那张表（spec #317：注入范围严格限定开场段 × 有首帧槽的模式），这里是**字面
# 期望值**：不接受「照实现算一遍」。契约变了这张表会先报红，提醒正式改票面。
_EXPECTED_INJECTED = {("i2v", "opening"), ("fl2v", "opening")}


def _matrix_params():
    params = []
    for mode in PLAN_MODES:
        for seg_type in SEGMENT_TYPES:
            if seg_type not in segment_types_for_mode(mode):
                # i2v 只允许开场段（ADR 0026）：其余组合在本模式下不合法，
                # 写段请求根本不会遇到，显式跳过而不是假装测过。
                params.append(pytest.param(
                    mode, seg_type, marks=pytest.mark.skip(
                        reason=f"{mode} 不允许 {seg_type} 段（ADR 0026），组合不合法")))
            else:
                params.append(pytest.param(mode, seg_type))
    return params


def test_the_ticket_table_matches_the_giving_contract():
    """票面的字面表与给图契约必须一致：不一致时是票面或契约一方漂了，别悄悄改期望。"""
    derived = {
        (mode, seg_type) for mode in PLAN_MODES for seg_type in SEGMENT_TYPES
        if seg_type in segment_types_for_mode(mode)
        and seg_type == FIRST_SEGMENT_TYPE
        and needs_first_frame_image(seg_type, mode)
    }
    assert derived == _EXPECTED_INJECTED


@pytest.mark.parametrize("mode,seg_type", _matrix_params())
def test_injection_matrix_covers_four_modes_by_five_segment_types(mode, seg_type):
    """矩阵：只有开场段 × i2v/fl2v 带起点画面块。

    特别注意 fl2v 的切镜段：它有首帧槽（``needs_first_frame_image`` 为真），但那张图
    是段循环里才产出的段首帧图，写段时盘上还不存在——写段请求不许为它注入这张
    「用户交的开场首帧图」，否则模型会指一张没有的图。
    """
    from minimax_h3_prompt.director.opening_still import OPENING_STILL_BLOCK_HEAD

    plan, segment = _single(seg_type, mode)
    request = _request(plan, segment, start_frame=True,
                       start_frame_description=_DESCRIPTION)

    if (mode, seg_type) in _EXPECTED_INJECTED:
        assert OPENING_STILL_BLOCK_HEAD in request
        assert _DESCRIPTION in request
        assert "本段第一帧" in request
    else:
        assert OPENING_STILL_BLOCK_HEAD not in request, \
            f"{mode} 的 {seg_type} 段不该带开场首帧画面块"
        assert _DESCRIPTION not in request


# ---------------------------------------------------------------------------
# 接线：组装函数会拼 ≠ 调用方传了（真实写段的生产者必须透传）
# ---------------------------------------------------------------------------

_BODY = """{note}
integrated_multimodal_description:
[Shot 1] A woman in a grey trench coat stands under the shop awning in the rain.

overall_soundscape:
Rain drums on the metal awning.

non_diegetic_music:
N/A
"""


class _WriteLLM:
    """记录出站写段请求、回一版合格正文的桩模型。"""

    def __init__(self, plan: DirectorPlan) -> None:
        self.plan = plan
        self.requests: list[str] = []

    def invoke(self, request, config=None):
        text = str(request)
        self.requests.append(text)
        index = int(re.search(r"Segment (\d+)/", text).group(1))
        from minimax_h3_prompt.director.prompts import render_timeline_note

        return _BODY.format(note=render_timeline_note(self.plan,
                                                      self.plan.segments[index - 1]))


def test_write_director_segments_passes_the_start_frame_fact_through(tmp_path):
    """批量写段的生产者逐段透传：只有开场段拿到这块，续接/收尾段一个字不多。

    这是本仓被咬过多次的形态——``build_segment_request`` 会拼不等于调用方传了；
    真跑一次 ``write_director_segments``，看的是**模型眼前**的请求。
    """
    from minimax_h3_prompt.director.opening_still import OPENING_STILL_BLOCK_HEAD
    from minimax_h3_prompt.director.prompts import write_director_segments

    segment1 = _segment(1, "opening", "fl2v")
    segment2 = _segment(2, "continue", "fl2v",
                        start_frame=segment1.end_frame, export_frames=153)
    segment3 = _segment(3, "ending", "fl2v",
                        start_frame=segment2.end_frame, export_frames=153)
    plan = DirectorPlan(mode="fl2v", total_frames=segment3.end_frame,
                        segments=(segment1, segment2, segment3))

    llm = _WriteLLM(plan)
    write_director_segments(plan, llm=llm, session_dir=tmp_path,
                            start_frame=True,
                            start_frame_description=_DESCRIPTION)

    assert len(llm.requests) == 3, "每段各一次请求"
    assert OPENING_STILL_BLOCK_HEAD in llm.requests[0]
    assert _DESCRIPTION in llm.requests[0]
    for request in llm.requests[1:]:
        assert OPENING_STILL_BLOCK_HEAD not in request, "非开场段不该带起点画面块"
        assert _DESCRIPTION not in request


def test_rewrite_request_keeps_the_start_frame_fact():
    """改词与首写必须是**同一份写段上下文**（#298 的既有口径）：改一次不能把
    起点画面事实改丢，否则首写以图为准、改词以剧本为准，两条口径当场分家。"""
    from minimax_h3_prompt.director.opening_still import OPENING_STILL_BLOCK_HEAD
    from minimax_h3_prompt.director.prompts import build_rewrite_request

    plan, segment = _single("opening", "fl2v")
    request = build_rewrite_request(
        plan, segment, current_text="[Shot 1] 女人在檐下抬头看雨。",
        start_frame=True, start_frame_description=_DESCRIPTION)

    assert OPENING_STILL_BLOCK_HEAD in request
    assert _DESCRIPTION in request


# ---------------------------------------------------------------------------
# 规划期：规划师定的第一段边界与落定画面正是要跟图对齐的那处
# ---------------------------------------------------------------------------

_REPLY_THREE = json.dumps({"segments": [
    {"start_s": 0, "end_s": 6, "type": "opening",
     "summary": "女人在檐下抬头看雨", "end_hook": "她迈出檐下，走向雨里"},
    {"start_s": 6, "end_s": 12, "type": "continue",
     "summary": "沿街走向便利店", "end_hook": "她加快脚步穿过水洼，踏上店门口的台阶"},
    {"start_s": 12, "end_s": 18, "type": "ending",
     "summary": "推开店门", "end_hook": "她收伞侧身挤进店门，带进一阵冷风"},
]}, ensure_ascii=False)

_REPLY_SINGLE = json.dumps({"segments": [
    {"start_s": 0, "end_s": 4, "type": "opening",
     "summary": "女人在檐下抬头看雨", "end_hook": "她迈出檐下，走向雨里"},
]}, ensure_ascii=False)


class _PlanLLM:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.requests: list[str] = []

    def invoke(self, request, config=None):
        self.requests.append(str(request))
        return self.reply


def _planning_request(mode: str, **kwargs) -> str:
    from minimax_h3_prompt.director.planning import plan_director_segments

    llm = _PlanLLM(_REPLY_SINGLE if mode == "i2v" else _REPLY_THREE)
    plan_director_segments(
        "分镜表：\n[Shot 1] 雨夜，女人撑伞站在便利店檐下。",
        4.0 if mode == "i2v" else 18.0, llm, mode=mode, **kwargs)
    return llm.requests[0]


@pytest.mark.parametrize("mode", ["i2v", "fl2v"])
def test_planner_request_carries_the_start_frame_description(mode):
    """有首帧槽的模式：规划师的请求里也带起点画面事实，措辞取自同一份渲染器。"""
    from minimax_h3_prompt.director.opening_still import (
        OPENING_STILL_BLOCK_HEAD,
        render_opening_still_block,
    )

    request = _planning_request(mode, start_frame_description=_DESCRIPTION)

    assert OPENING_STILL_BLOCK_HEAD in request, "规划请求没带起点画面块"
    assert _DESCRIPTION in request
    # 与写段侧同源、只差用途半句：规划请求里的就是「规划期」那一版渲染。
    assert render_opening_still_block(_DESCRIPTION, for_planning=True) in request
    assert "规划" in request


@pytest.mark.parametrize("mode,expected", [
    ("t2v", False), ("r2v", False), ("i2v", True), ("fl2v", True)])
def test_planner_injects_only_where_the_giving_contract_has_a_first_slot(mode, expected):
    """模式闸与写段侧同一条：t2v 一个图槽都没有、r2v 的画面来源是参考素材。"""
    from minimax_h3_prompt.director.opening_still import OPENING_STILL_BLOCK_HEAD

    request = _planning_request(mode, start_frame_description=_DESCRIPTION)
    if expected:
        assert OPENING_STILL_BLOCK_HEAD in request
    else:
        assert OPENING_STILL_BLOCK_HEAD not in request
        assert _DESCRIPTION not in request


def test_planner_does_not_inject_an_empty_description():
    """空描述不产生块（与写段侧同一条空值契约）。"""
    from minimax_h3_prompt.director.opening_still import OPENING_STILL_BLOCK_HEAD

    for description in ("", "  \n "):
        request = _planning_request("fl2v", start_frame_description=description)
        assert OPENING_STILL_BLOCK_HEAD not in request


def test_planner_sits_next_to_the_film_end_block_with_start_first():
    """规划期放位说明：起点块在终点块之前（与写段侧「起点 → 终点」同序）。

    两个请求里同一件事的先后一致，模型不会在一处先读到终点、在另一处先读到起点。
    """
    from minimax_h3_prompt.director.film_end import FILM_END_BLOCK_HEAD
    from minimax_h3_prompt.director.opening_still import OPENING_STILL_BLOCK_HEAD

    film_end_description = "终点画面：她停在收银台前，手里拿着一瓶热咖啡。"
    request = _planning_request(
        "fl2v", start_frame_description=_DESCRIPTION,
        film_end_description=film_end_description)

    assert OPENING_STILL_BLOCK_HEAD in request and FILM_END_BLOCK_HEAD in request
    assert request.index(OPENING_STILL_BLOCK_HEAD) < request.index(FILM_END_BLOCK_HEAD)


# ---------------------------------------------------------------------------
# 判据来源：注入闸必须真的问给图契约，而不是模式字面量 / 段类型清单
# ---------------------------------------------------------------------------

def test_write_side_criterion_asks_the_giving_contract(monkeypatch):
    """把契约函数按到 False，写段请求就不能出块；按到 True，t2v 也必须出块。

    两个方向一起缝住「写死 ``mode == "fl2v"``」：真想写死模式字面量，这两个断言
    一个也过不去（r2v/t2v 的开场段本来就不该有块，靠字面量实现的代码只在把契约改成
    True 时才露馅）。
    """
    import minimax_h3_prompt.director.prompts as prompts_mod
    from minimax_h3_prompt.director.opening_still import OPENING_STILL_BLOCK_HEAD

    plan, segment = _single("opening", "fl2v")
    monkeypatch.setattr(prompts_mod, "needs_first_frame_image", lambda *a, **k: False)
    request = _request(plan, segment, start_frame=True,
                       start_frame_description=_DESCRIPTION)
    assert OPENING_STILL_BLOCK_HEAD not in request, "判据没问给图契约"

    monkeypatch.setattr(prompts_mod, "needs_first_frame_image", lambda *a, **k: True)
    plan, segment = _single("opening", "t2v")
    request = _request(plan, segment, start_frame=True,
                       start_frame_description=_DESCRIPTION)
    assert OPENING_STILL_BLOCK_HEAD in request, "判据没问给图契约（t2v 方向）"


def test_planning_side_criterion_asks_the_giving_contract(monkeypatch):
    """规划层同一条：判据必须问 ``needs_first_frame_image``，不许写模式字面量。"""
    import minimax_h3_prompt.director.planning as planning_mod
    from minimax_h3_prompt.director.opening_still import OPENING_STILL_BLOCK_HEAD

    monkeypatch.setattr(planning_mod, "needs_first_frame_image", lambda *a, **k: False)
    request = _planning_request("fl2v", start_frame_description=_DESCRIPTION)
    assert OPENING_STILL_BLOCK_HEAD not in request, "规划层判据没问给图契约"

    monkeypatch.setattr(planning_mod, "needs_first_frame_image", lambda *a, **k: True)
    request = _planning_request("t2v", start_frame_description=_DESCRIPTION)
    assert OPENING_STILL_BLOCK_HEAD in request, "规划层判据没问给图契约（t2v 方向）"
