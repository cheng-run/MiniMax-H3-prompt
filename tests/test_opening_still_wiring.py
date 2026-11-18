"""#326 接线守卫：开场首帧画面块真的到了模型眼前（两条入口 ＋ 真实写段生产者）。

本仓被同一形态咬过六次：**规则在、接线不在**——组装函数会拼一块文本，但调用方没把
参数传进去，那一块永远不出现，而组装层的单元测试全绿（#322 的矩阵用例正是这种绿）。
所以这里不看组装函数，只看**记录型桩模型抓到的那份真正出站的请求**：

1. 命令行入口：真跑 ``wizard._run_director_flow``（续跑形态、三段的 fl2v），抓
   规划请求与三条写段请求；只有开场段的请求带起点画面块（标题 ＋ 描述原文 ＋
   纪律句），续接段与收尾段一个字不多；
2. 真实写段生产者：真跑 ``write_director_segments``，覆盖 fl2v 的五种段类型（开场、
   续接、运镜·同场景、运镜·切镜、收尾）与无首帧槽的 t2v／r2v；
3. 底线：没有图、描述为空时，生产者的出站请求与不传新参数**逐字节相同**。

为什么必须逐字节：``start_frame`` 的默认值一旦被改成 True（或空描述被当成有描述），
所有既有调用点都会静默多出一块——请求文本没有校验器看得住这种错。
"""
from __future__ import annotations

import re

import pytest

from minimax_h3_prompt.director.frames import (
    DirectorPlan,
    give_images_for,
    is_guided,
)
from minimax_h3_prompt.director.opening_still import OPENING_STILL_BLOCK_HEAD
from minimax_h3_prompt.writing_disciplines import FRAME_CONSISTENCY_DISCIPLINE

# 与全片尾帧描述刻意不同：两个 sidecar 落在同一棵树里，字符串相同就分不清「哪份
# 描述进了哪个请求」。
from test_wizard_opening_still import _OPENING, _by_image, _patch_describe
from test_director_film_end import _RecordLLM, _resumed_fl2v_session, _stub_flow_tail

_WRITER_PREFIX = "本段编号：Segment "


def _segment(index: int, seg_type: str, mode: str, *,
             start_frame: int = 0, export_frames: int = 107):
    """一段合法的段表条目：给图清单从给图契约取，不手写字面量。"""
    from minimax_h3_prompt.director.frames import DirectorSegment

    guided = is_guided(seg_type)
    declared = export_frames - 12 if guided else export_frames
    return DirectorSegment(
        index=index, type=seg_type, guided=guided, start_frame=start_frame,
        end_frame=start_frame + export_frames, declared_frames=declared,
        export_frames=export_frames, summary=f"段 {index} 剧情概述",
        end_hook=f"段 {index} 落定画面",
        give_images=give_images_for(seg_type, mode))


def _body_for(request: str, plan: DirectorPlan, *, r2v: bool = False) -> str:
    """桩模型的写段回复：从请求里读段号，按段表注入与本段一致的时间轴注记。

    注记由真实的 ``render_timeline_note`` 生成——校验器按它判段词是否与段表一致，
    桩若自己抄一份，段号一换就会以「注记不符」报红，把接线守卫淹掉。
    """
    from minimax_h3_prompt.director.prompts import render_timeline_note

    index = int(re.search(r"Segment (\d+)/", request).group(1))
    note = render_timeline_note(plan, plan.segments[index - 1])
    if r2v:
        # r2v 的六节外壳：每节都必须出现且有 <Picture N> 指称。
        return (
            "subject_definitions:\n"
            "<Picture 1> is the woman under the umbrella.\n\n"
            "summary:\n"
            "[reference generation] the woman walks through the rain.\n\n"
            "retention_analysis:\n"
            "<Picture 1>: fully_preserved.\n\n"
            "detailed_description:\n"
            f"{note}\n"
            "[Shot 1] <Picture 1> walks down the rainy street.\n\n"
            "overall_soundscape:\n"
            "Rain drums on the pavement.\n\n"
            "non_diegetic_music:\n"
            "N/A\n"
        )
    return (
        f"{note}\n"
        "integrated_multimodal_description:\n"
        "[Shot 1] A woman in a grey trench coat stands under the shop awning.\n\n"
        "overall_soundscape:\n"
        "Rain drums on the metal awning.\n\n"
        "non_diegetic_music:\n"
        "N/A\n"
    )


class _WiringLLM:
    """记录型桩模型：把每一次**真正出站**的请求原样存下来。"""

    def __init__(self, plan: DirectorPlan, *, r2v: bool = False) -> None:
        self.plan = plan
        self.r2v = r2v
        self.requests: list[str] = []

    def invoke(self, request, config=None):
        text = str(request)
        self.requests.append(text)
        return _body_for(text, self.plan, r2v=self.r2v)


def _requests_for(llm: _WiringLLM, index: int) -> list[str]:
    return [r for r in llm.requests if f"Segment {index}/" in r]


# ---------------------------------------------------------------------------
# 1. 命令行入口：真跑一次完整流程，只有开场段的请求带块
# ---------------------------------------------------------------------------

def test_cli_entry_sends_the_opening_block_only_on_the_opening_segment(
        tmp_path, monkeypatch):
    """续跑形态（收集步骤被跳过）下走完整流程：规划师与开场段都看到图，其它段没有。

    断言三件同源的事实必须一起出现（标题 ＋ 描述原文 ＋ 纪律句）：只断言描述就会
    漏掉「标题丢了、模型不知道这堆字是画面事实」这种半接线。
    """
    from minimax_h3_prompt.ui import wizard

    session = _resumed_fl2v_session(tmp_path)
    llm = _RecordLLM()
    _patch_describe(monkeypatch, _by_image)
    _stub_flow_tail(monkeypatch, llm)

    assert wizard._run_director_flow(session) == 0

    planner = next(r for r in llm.requests if "分段规划师" in r)
    assert OPENING_STILL_BLOCK_HEAD in planner, "规划请求没带起点画面块"
    assert _OPENING in planner
    assert FRAME_CONSISTENCY_DISCIPLINE in planner, "规划请求没带双向一致性纪律句"

    opening = next(r for r in llm.requests if "Segment 1/3" in r)
    assert opening.count(OPENING_STILL_BLOCK_HEAD) == 1, "开场段必须恰好带一份起点画面块"
    assert _OPENING in opening, "描述原文没进开场段的写段请求"
    assert FRAME_CONSISTENCY_DISCIPLINE in opening, "开场段没带双向一致性纪律句"
    assert "写本段正文时以它为准" in opening, "块没写清拿它来写正文"

    others = [r for r in llm.requests
              if "Segment 2/3" in r or "Segment 3/3" in r]
    assert len(others) == 2, "续接段与收尾段各应发一次请求"
    for request in others:
        assert OPENING_STILL_BLOCK_HEAD not in request, "非开场段不该带起点画面块"
        assert _OPENING not in request, "非开场段不该带起点画面描述"
        assert FRAME_CONSISTENCY_DISCIPLINE not in request, \
            "非开场段不该带双向一致性纪律句（它约束的是本段第一帧）"


# ---------------------------------------------------------------------------
# 2. 真实写段生产者：段类型矩阵
# ---------------------------------------------------------------------------

def _fl2v_five_types_plan() -> DirectorPlan:
    """fl2v 的五种段类型各一段：开场、续接、运镜·同场景、运镜·切镜、收尾。"""
    types = ("opening", "continue", "camera_same", "camera_cut", "ending")
    segments = []
    start = 0
    for index, seg_type in enumerate(types, start=1):
        export_frames = 107 if index == 1 else 153
        segment = _segment(index, seg_type, "fl2v",
                           start_frame=start, export_frames=export_frames)
        segments.append(segment)
        start = segment.end_frame
    return DirectorPlan(mode="fl2v", total_frames=start, segments=tuple(segments))


def test_write_producer_sends_the_block_only_on_the_fl2v_opening_segment(tmp_path):
    """五种段类型逐项断言：只有开场段带块。

    切镜段（``camera_cut``）与运镜段（``camera_same``）有首帧槽，但那是段循环里才
    产出的**段首帧图**；写段时盘上还不存在，不能把用户交的开场首帧图塞给它们。
    """
    from minimax_h3_prompt.director.prompts import write_director_segments

    plan = _fl2v_five_types_plan()
    llm = _WiringLLM(plan)
    write_director_segments(plan, llm=llm, session_dir=tmp_path,
                            start_frame=True, start_frame_description=_OPENING)

    for index, seg_type in enumerate(
            ("opening", "continue", "camera_same", "camera_cut", "ending"), start=1):
        requests = _requests_for(llm, index)
        assert len(requests) == 1, f"第 {index} 段应恰好发一次请求"
        request = requests[0]
        if seg_type == "opening":
            assert OPENING_STILL_BLOCK_HEAD in request
            assert _OPENING in request
            assert FRAME_CONSISTENCY_DISCIPLINE in request
        else:
            assert OPENING_STILL_BLOCK_HEAD not in request, \
                f"fl2v 的 {seg_type} 段不该带开场首帧画面块"
            assert _OPENING not in request
            assert FRAME_CONSISTENCY_DISCIPLINE not in request


@pytest.mark.parametrize("mode", ["t2v", "r2v"])
def test_write_producer_never_sends_the_block_without_a_first_frame_slot(
        tmp_path, mode):
    """无首帧槽的模式：盘上真有图、描述也传进来了，开场段请求仍然一个字不多。

    调用方（向导／工作台）已在模式闸前停住；这里证明第二道闸也在：就算某个调用点
    误传了事实，给图契约也会把它挡在请求之外。
    """
    from minimax_h3_prompt.director.prompts import write_director_segments

    segment = _segment(1, "opening", mode)
    plan = DirectorPlan(mode=mode, total_frames=segment.end_frame, segments=(segment,))
    llm = _WiringLLM(plan, r2v=(mode == "r2v"))
    kwargs = {}
    if mode == "r2v":
        # r2v 的定义块是写段请求的必填项（六节外壳逐字取它）。
        kwargs["sections"] = {
            "subject_definitions": "<Picture 1> is the woman under the umbrella.",
            "retention_analysis": "<Picture 1>: fully_preserved.",
        }
        kwargs["registry"] = ((1, "woman"),)
    write_director_segments(plan, llm=llm, session_dir=tmp_path,
                            start_frame=True, start_frame_description=_OPENING,
                            **kwargs)

    requests = _requests_for(llm, 1)
    assert len(requests) == 1
    assert OPENING_STILL_BLOCK_HEAD not in requests[0], \
        f"{mode} 没有首帧槽，却把开场首帧画面块塞进了请求"
    assert _OPENING not in requests[0]
    assert FRAME_CONSISTENCY_DISCIPLINE not in requests[0]


# ---------------------------------------------------------------------------
# 3. 底线：没有图／描述为空时，生产者的出站请求与改前逐字节相同
# ---------------------------------------------------------------------------

def _single_opening_fl2v_plan() -> DirectorPlan:
    segment = _segment(1, "opening", "fl2v")
    return DirectorPlan(mode="fl2v", total_frames=segment.end_frame,
                        segments=(segment,))


def _producer_requests(tmp_path, name: str, **kwargs) -> list[str]:
    from minimax_h3_prompt.director.prompts import write_director_segments

    plan = _single_opening_fl2v_plan()
    llm = _WiringLLM(plan)
    write_director_segments(plan, llm=llm, session_dir=tmp_path / name, **kwargs)
    return llm.requests


def test_write_producer_is_byte_identical_without_an_image_or_a_description(tmp_path):
    """默认参数、盘上无图、描述为空（含纯空白）三种入参的出站请求逐字节相同。

    「默认参数＝改前调用」是本票的底线：新形参一旦改了默认值，所有既有调用点都会
    静默多一块；描述为空时若叠出标题或空行，请求同样不再等于改前。
    """
    baseline = _producer_requests(tmp_path, "baseline")
    assert baseline, "基线请求应当真的发出"

    no_image = _producer_requests(
        tmp_path, "no-image",
        start_frame=False, start_frame_description=_OPENING)
    assert no_image == baseline, "盘上无图时描述单独出现，请求不该有任何变化"

    empty = _producer_requests(
        tmp_path, "empty", start_frame=True, start_frame_description="")
    assert empty == baseline, "描述为空时不该出现块（也不该多出空行）"

    whitespace = _producer_requests(
        tmp_path, "whitespace", start_frame=True,
        start_frame_description="  \n\t ")
    assert whitespace == baseline, "纯空白描述与空描述同一条契约"

    explicit_off = _producer_requests(
        tmp_path, "explicit-off",
        start_frame=False, start_frame_description="")
    assert explicit_off == baseline, "显式关掉与默认参数必须同一条路径"
