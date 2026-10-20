"""多参段「修改提示词」一轮（issue #56 AC4/AC5/AC7）：只动画面正文，锁死那两句。

接缝（写作层纯件，LLM 注入打桩）：
- ``segment_prompts.split_ref_segment_detailed``：把 detailed_description 拆成
  「锁死的时间轴注记＋起止状态声明」与「画面正文」——「只动正文」与「那两句被改动
  就响」两处判据的共同切点；
- ``segment_prompts.assert_locked_annotations_intact``：改写结果里出现注记类句子
  时必须与锁定文本逐字一致，否则**响**（AC5：不许静默放行）；
- ``ref_segments.revise_ref_segment_body``：一轮改写（意见 → LLM → 只换正文）。

为什么锁死那两句：相邻段的末态与下一段起始态是**同一瞬间的同一画面**。放开它就要
级联改下一段的 start_state 并回写 plan.json，否则语义层闸门的对照物（声明）与正文
失同步、判据就废了；声明本身写错属于「计划错了」，走「停下 → 重跑分段规划」。
"""
from __future__ import annotations

import pytest

from minimax_h3_prompt.ref_segments import revise_ref_segment_body
from minimax_h3_prompt.segment_planner import SegmentPlan
from minimax_h3_prompt.segment_prompts import (
    assemble_ref_segment,
    assert_locked_annotations_intact,
    render_ref_segment_annotations,
    split_ref_segment_detailed,
)
from minimax_h3_prompt.tools.h3_validator import split_ref_sections

_PLAN = SegmentPlan(
    index=0, start_s=0, end_s=10,
    shots_in_segment=(1, 2),
    summary="女孩把青铜钥匙递出",
    start_state="the jar is held in her right hand",
    end_hook="the jar rests on the wooden table",
)

_DEFS = (
    "<Subject 1> is 红裙女孩 (the girl in <Picture 1>), with long dark hair.\n"
    "<Subject 2> is 青铜钥匙 (the key in <Picture 2>), with an old green patina."
)
_RETENTION = "<Subject 1> (appears in [Shot 1]): fully_preserved - ok."
_BODY = (
    "The segment is a warm autumn two-shot.\n"
    "[Shot 1] A medium shot frames the girl raising the jar.\n"
    "At 00:04.000, the camera settles as she lowers it toward the table."
)


def _segment_text(body: str = _BODY, *, plan: SegmentPlan = _PLAN,
                  total_segments: int = 2, total_s: int = 20) -> str:
    """一段自包含 ref 六段式（组装器注入注记，与生产路径同源）。"""
    return assemble_ref_segment(
        subject_definitions=_DEFS, retention_analysis=_RETENTION,
        task_type="reference generation", summary_body="The girl sets the jar down.",
        detailed_body=body,
        overall_soundscape="Soft room tone continues.",
        non_diegetic_music="N/A",
        annotations=render_ref_segment_annotations(
            plan.index + 1, total_segments, plan.start_s, plan.end_s, total_s,
            plan.start_state, plan.end_hook),
    )


def _locked_of(text: str) -> str:
    return split_ref_segment_detailed(
        split_ref_sections(text)["detailed_description"])[0]


# ---------------------------------------------------------------------------
# 接缝 3a：拆开「锁死声明」与「画面正文」
# ---------------------------------------------------------------------------

def test_locked_annotations_are_split_off_the_body():
    """注记（时间轴＋起止状态）与画面正文分得开，且正文里不再有那两句。

    生产路径上注记由组装器置于 detailed_description 顶部（``assemble_ref_segment``），
    本判据就是「改写只动正文」与「那两句被改动就响」的共同切点。
    """
    text = _segment_text()
    detailed = split_ref_sections(text)["detailed_description"]
    locked, body = split_ref_segment_detailed(detailed)

    assert locked == render_ref_segment_annotations(
        1, 2, 0, 10, 20, _PLAN.start_state, _PLAN.end_hook)
    assert locked.startswith("Segment 1 of 2, covering 0s-10s of the full 20s video.")
    assert "This segment begins with the jar is held in her right hand." in locked
    assert "This segment ends with the jar rests on the wooden table." in locked

    assert body == _BODY
    assert "This segment begins with" not in body
    assert "This segment ends with" not in body


def test_detailed_without_annotation_has_empty_locked_part():
    """没有注记行时锁定部分是空串、正文原样——不把正文的头一行误当注记吃掉。"""
    locked, body = split_ref_segment_detailed(_BODY)
    assert locked == ""
    assert body == _BODY


# ---------------------------------------------------------------------------
# 接缝 3b：那两句被改动就响（AC5）
# ---------------------------------------------------------------------------

def test_changed_end_state_sentence_is_refused():
    """改写结果把锁死的末态句改掉了 → 响错，且报出被改动的是哪一句。"""
    locked = _locked_of(_segment_text())
    tampered = _BODY + "\nThis segment ends with the jar already put down on the table."

    with pytest.raises(ValueError) as exc:
        assert_locked_annotations_intact(tampered, locked)
    assert "This segment ends with" in str(exc.value)
    assert "锁死" in str(exc.value)


def test_changed_timeline_note_is_refused():
    """时间轴注记（段序/起止秒）被改动同样响——它是段间对位的唯一依据。"""
    locked = _locked_of(_segment_text())
    with pytest.raises(ValueError):
        assert_locked_annotations_intact(
            _BODY + "\nSegment 1 of 3, covering 0s-12s of the full 24s video.", locked)


def test_verbatim_locked_sentences_and_clean_body_both_pass():
    """逐字复述那两句（模型多写了一遍）不算改动；正文里根本不含它们也放行。

    这一条是「响错」的负样本：判据是**字面**比对，不是「出现过就报」——模型照抄一遍
    由组装器再注入一次即可，只有**改过**的才算越权。
    """
    locked = _locked_of(_segment_text())
    assert_locked_annotations_intact(_BODY, locked)
    assert_locked_annotations_intact(f"{_BODY}\n{locked}", locked)


def test_annotation_shape_follows_the_canonical_prefix(monkeypatch):
    """识别式与前缀常量**同源，且当场可验**（复审采纳项）。

    把 canonical 前缀改掉，守卫必须跟着改：这一条只有在识别式**按调用时**从常量派生
    时才会过。字面重抄一份形态的写法在这里就红——改掉常量后那份拷贝再也匹配不上，
    守卫成了永远不响的守卫（「永不为红＝没有」）。
    """
    from minimax_h3_prompt import segment_prompts

    locked = _locked_of(_segment_text())
    monkeypatch.setattr(segment_prompts, "_REF_END_STATE_PREFIX", "This clip wraps up with ")
    with pytest.raises(ValueError):
        segment_prompts.assert_locked_annotations_intact(
            "This clip wraps up with the jar already put down.", locked)


# ---------------------------------------------------------------------------
# 接缝 4：一轮改写——只换正文，其余逐字保留（AC4/AC5）
# ---------------------------------------------------------------------------

class _StubLLM:
    """记录请求串的假模型（同仓库其他 LLM 用例：请求即契约，抓下来断言）。"""

    def __init__(self, reply: str):
        self.reply = reply
        self.requests: list[str] = []

    def invoke(self, request: str):
        self.requests.append(request)
        return self.reply


def test_revision_replaces_only_the_body(tmp_path):
    """改一轮：正文换了，定义块/注记/soundscape 逐字不动（AC4「写回本段提示词」）。"""
    old = _segment_text()
    new_body = _BODY.replace("raising the jar", "lowering the jar onto the table")
    llm = _StubLLM(new_body)

    new = revise_ref_segment_body(old, "末态应该是罐子已经放下", llm)

    old_sections, new_sections = split_ref_sections(old), split_ref_sections(new)
    assert new_sections["subject_definitions"] == old_sections["subject_definitions"]
    assert new_sections["retention_analysis"] == old_sections["retention_analysis"]
    assert new_sections["overall_soundscape"] == old_sections["overall_soundscape"]
    assert new_sections["non_diegetic_music"] == old_sections["non_diegetic_music"]
    assert _locked_of(new) == _locked_of(old)
    assert "lowering the jar onto the table" in new_sections["detailed_description"]
    assert "raising the jar" not in new_sections["detailed_description"]

    # 请求里必须同时给出正文、锁死声明与意见：模型看不见声明就无从「兑现」它
    request = llm.requests[0]
    assert "raising the jar" in request
    assert _locked_of(old) in request
    assert "末态应该是罐子已经放下" in request


def test_revision_refuses_a_body_that_tampers_with_the_locked_lines():
    """模型顺手改了锁死句 → 抛错（调用方据此保持原词，不采用这一轮）。"""
    old = _segment_text()
    llm = _StubLLM(_BODY + "\nThis segment ends with the jar already put down.")

    with pytest.raises(ValueError):
        revise_ref_segment_body(old, "把罐子放下", llm)


def test_revision_keeps_the_file_own_declaration_verbatim():
    """「只动画面正文」的字面含义：其余**一字不动**——声明取自段文件，不从 plan 重渲染。

    判据用一份**与 plan 不一致**的段文件（声明句被改过）：若改写顺手按 plan 重渲染，
    这一条就红。交付时校验器已经核过「声明与 plan 对得上」，改写这一轮不该替用户
    改文件里别的东西（那是「重跑分段规划」的事）。
    """
    drifted = _segment_text().replace(
        "This segment ends with the jar rests on the wooden table.",
        "This segment ends with the jar resting on the wooden table.")
    assert _locked_of(drifted) != _locked_of(_segment_text()), "构造的漂移没生效"

    llm = _StubLLM("[Shot 1] The girl lowers the jar toward the table.")
    new = revise_ref_segment_body(drifted, "把罐子放下", llm)
    assert _locked_of(new) == _locked_of(drifted)
    assert "jar resting on the wooden table" in llm.requests[0]


def test_revision_rejects_empty_output():
    """空产出＝没改成，抛错（不许把空正文拼成一段没画面的提示词）。"""
    with pytest.raises(ValueError):
        revise_ref_segment_body(_segment_text(), "随便改改", _StubLLM("   "))
