"""ref 正文语言守卫（issue #75 AC6）：中文散文判 error，专名/对白/引号内可见文字豁免。

为什么要有这条闸：写段模板与整片模板都要求「全部英文」（``segment_prompts`` 的写段
纪律、``prompts/prompt_engineer.md``），但校验器从来没有语言判据。2026-10-22 的
真实运行里 ``subject_definitions`` 与 ``retention_analysis`` 整段中文照样过闸，
模型拿着中文定义去生成视频——规则写了、闸门没有，本仓「规则在、接线不在」的老形态。
"""
from __future__ import annotations

from minimax_h3_prompt.segment_prompts import validate_ref_segment
from minimax_h3_prompt.tools.h3_validator import validate_ref

# 一份六段式骨架：只有定义/摘要/保留分析三节按用例替换，其余保持合规英文
_ENGLISH_SIX = """subject_definitions:
<Subject 1> is the young woman in <Picture 1>, with long dark hair and a red dress.

summary:
[reference generation] The target video follows the woman as she crosses the garden.

retention_analysis:
<Subject 1> (appears in [Shot 1]): fully_preserved - the red dress is retained.

detailed_description:
The target video is in a cinematic autumn style with warm soft lighting.
[Shot 1] A medium shot frames the garden path as she walks toward the gate.

overall_soundscape:
Soft outdoor ambience with light footsteps.

non_diegetic_music:
N/A
"""

_CHINESE_PROSE = (
    "红裙女孩是一个二十出头的年轻女性，长发及腰，穿着酒红色连衣裙，腰间系着一条细"
    "银色腰带，脚上是深棕色短靴。她站在花园小径的尽头，手里握着一把泛着铜绿的旧钥匙，"
    "目光落在门厅的方向。背景是秋天的梧桐树，暖色的阳光从侧面照过来。"
)


def _codes(issues):
    return {issue.code for issue in issues}


def test_chinese_subject_definitions_is_an_error():
    """中文定义块 → BODY_NOT_ENGLISH（error），并点名是哪一段。"""
    text = _ENGLISH_SIX.replace(
        "<Subject 1> is the young woman in <Picture 1>, with long dark hair and a red dress.",
        f"<Subject 1> {_CHINESE_PROSE}")
    issues = validate_ref(text)
    english = [i for i in issues if i.code == "BODY_NOT_ENGLISH"]
    assert english and english[0].severity == "error"
    assert "subject_definitions" in english[0].message


def test_chinese_retention_and_summary_are_reported_per_section():
    """summary 与 retention_analysis 各自报一条（不是合并成一条含糊的）。"""
    text = _ENGLISH_SIX.replace(
        "[reference generation] The target video follows the woman as she crosses the garden.",
        f"[reference generation] {_CHINESE_PROSE}").replace(
        "<Subject 1> (appears in [Shot 1]): fully_preserved - the red dress is retained.",
        f"<Subject 1> (appears in [Shot 1]): fully_preserved - {_CHINESE_PROSE}")
    english = [i for i in validate_ref(text) if i.code == "BODY_NOT_ENGLISH"]
    assert {("summary" in i.message, "retention_analysis" in i.message) for i in english} == {
        (True, False), (False, True)}


def test_english_body_with_chinese_proper_names_is_not_flagged():
    """英文句中的中文专名（如「红裙女孩」）不算违规——专名本来就该保留原文。"""
    text = _ENGLISH_SIX.replace(
        "<Subject 1> is the young woman in <Picture 1>, with long dark hair and a red dress.",
        "<Subject 1> is 红裙女孩 (the woman in <Picture 1>), holding 青铜钥匙.")
    assert "BODY_NOT_ENGLISH" not in _codes(validate_ref(text))


def test_dialogue_inside_d_tags_is_exempt():
    """``<d>[语言] … </d>`` 内的对白按语言写，不是散文——不计入中文占比。"""
    text = _ENGLISH_SIX.replace(
        "Soft outdoor ambience with light footsteps.",
        "Soft outdoor ambience with light footsteps. "
        f"<d>[Chinese] {_CHINESE_PROSE}</d>")
    assert "BODY_NOT_ENGLISH" not in _codes(validate_ref(text))


def test_quoted_visible_text_is_exempt():
    """英文引号内的可见文字（招牌/字幕）保留原文，不算中文散文。"""
    text = _ENGLISH_SIX.replace(
        "Soft outdoor ambience with light footsteps.",
        f'Soft outdoor ambience with light footsteps. A sign reads "{_CHINESE_PROSE}".')
    assert "BODY_NOT_ENGLISH" not in _codes(validate_ref(text))


def test_segment_validator_reports_the_same_language_error():
    """段级校验走同一份判据（写段重写循环因此能修它，不另写一套）。"""
    text = _ENGLISH_SIX.replace(
        "<Subject 1> is the young woman in <Picture 1>, with long dark hair and a red dress.",
        f"<Subject 1> {_CHINESE_PROSE}")
    issues = validate_ref_segment(text, duration=8, segment_number=1, total_segments=1,
                                  start_s=0, total_s=8)
    assert "BODY_NOT_ENGLISH" in _codes(issues)
