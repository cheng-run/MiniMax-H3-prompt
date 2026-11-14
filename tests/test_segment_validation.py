"""D6（#246/#270）：段交接的两道判据（同步技术档 + 异步语义提示）。

纯函数层用例：段号/技术档自检「复用 check_segment_video 而不是另写一套」靠**帧数档**
咬住（4s 视频 107 帧 vs 6s 计划必须报 error）。

语义判据自 #270 起**只产出提示**：模型调用入口返回「差异说明」字符串（空串＝无提示），
不再返回布尔；调用方没有任何分支能因它把段判为不合格。判据提示词改「关键事实冲突」
口径：人物自身左右、具体哪只手、视线朝向、未入画部位不参与判定。
"""
from __future__ import annotations

import pytest

from minimax_h3_prompt.segment_validation import (
    build_end_hook_prompt,
    judge_end_hook,
    parse_end_hook_reply,
    segment_submission_problems,
    semantic_warning_text,
    validation_incomplete_text,
)
from conftest import write_h3_tier_mp4


class _Reply:
    def __init__(self, content: str) -> None:
        self.content = content


class _StubLlm:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.prompts: list[str] = []

    def invoke(self, prompt: str, config=None) -> _Reply:
        self.prompts.append(prompt)
        return _Reply(self.reply)


# ---------------------------------------------------------------------------
# 同步提交自检
# ---------------------------------------------------------------------------

def test_matching_segment_and_tier_passes(tmp_path):
    video = write_h3_tier_mp4(tmp_path / "seg01.mp4", 6)
    problems = segment_submission_problems(
        current_index=1, number=1, video_path=video, expected_seconds=6)
    assert problems == []


def test_wrong_segment_number_is_a_problem(tmp_path):
    video = write_h3_tier_mp4(tmp_path / "seg01.mp4", 6)
    problems = segment_submission_problems(
        current_index=1, number=2, video_path=video, expected_seconds=6)
    assert any("段号" in reason and "不一致" in reason for reason in problems)


def test_missing_file_is_a_problem_not_a_warning(tmp_path):
    """check_segment_video 对读不到的文件只报 warning；这一层必须补成不合格。"""
    problems = segment_submission_problems(
        current_index=1, number=1,
        video_path=tmp_path / "not-there.mp4", expected_seconds=6)
    assert any("读不到" in reason for reason in problems)


def test_tier_mismatch_is_reused_from_check_segment_video(tmp_path):
    # 4s 档 = 107 帧；本段计划 6s → check_segment_video 报 segment_video_duration_mismatch。
    video = write_h3_tier_mp4(tmp_path / "seg04.mp4", 4)
    problems = segment_submission_problems(
        current_index=1, number=1, video_path=video, expected_seconds=6)
    assert any("segment_video_duration_mismatch" in reason for reason in problems)


# ---------------------------------------------------------------------------
# 语义判据：只产出「差异说明」，不产出通过/不通过
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("一致\n无", ""),
    ("无冲突\n无", ""),
    ("冲突\n右手在胸前，计划是左手在胸前", "右手在胸前，计划是左手在胸前"),
    ("不一致\n包在左肩", "包在左肩"),
    ("冲突", "冲突"),
    ("", None),
    ("嗯……", None),
])
def test_parse_end_hook_reply(text, expected):
    """回复第一行判关键事实是否冲突，第二行是差异说明；空串＝无提示。"""
    assert parse_end_hook_reply(text) == expected


def test_judge_end_hook_returns_difference_note_not_a_bool():
    llm = _StubLlm("冲突\n脚没有入画，计划要求双脚一前一后")
    note = judge_end_hook("人物停在门口，双脚一前一后", "尾帧里人物停在门口", llm)
    assert note == "脚没有入画，计划要求双脚一前一后"
    assert len(llm.prompts) == 1
    assert "人物停在门口，双脚一前一后" in llm.prompts[0]
    assert "尾帧里人物停在门口" in llm.prompts[0]


def test_judge_end_hook_consistent_returns_empty_note():
    """无关键事实冲突时返回空串：调用方据此不写提示，也不是「未达到」。"""
    assert judge_end_hook("人物停在门口", "尾帧里人物停在门口", _StubLlm("一致\n无")) == ""


def test_judge_end_hook_unparseable_reply_raises():
    """判据服务给了没法用的回复＝语义检查未完成，由调用方降级成提示。"""
    with pytest.raises(ValueError):
        judge_end_hook("人物停在门口", "尾帧里人物还在走动", _StubLlm("嗯……"))


def test_judge_end_hook_without_end_hook_returns_no_note():
    """旧 plan.json 缺 end_hook → 无末态可判，不阻断也不提示（与 CLI 诚实降级同向）。"""
    llm = _StubLlm("冲突\n随便")
    assert judge_end_hook("", "任意画面", llm) == ""
    assert llm.prompts == []


def test_build_prompt_only_checks_key_facts_and_excludes_unobservable_details():
    """判据提示词改「关键事实冲突」口径：场景/地点/在场/大动作/关键道具。

    自身左右、具体哪只手、视线朝向、未入画部位不参与判定；描述没覆盖也不算不符。
    """
    prompt = build_end_hook_prompt("人物停在门口，双脚一前一后", "画面仍是暗的")
    assert "人物停在门口，双脚一前一后" in prompt
    assert "画面仍是暗的" in prompt
    assert "关键事实" in prompt
    assert "场景" in prompt and "在场" in prompt and "道具" in prompt
    for excluded in ("左右", "哪只手", "视线", "未入画"):
        assert excluded in prompt


def test_semantic_warning_text_keeps_plan_and_actual_tail_for_review():
    warning = semantic_warning_text(
        "脚没有入画", "人物停在门口，双脚一前一后", "尾帧里人物停在门口")
    assert warning.startswith("语义检查提示：")
    assert "脚没有入画" in warning
    assert "计划末态：人物停在门口，双脚一前一后" in warning
    assert "尾帧画面：尾帧里人物停在门口" in warning


def test_validation_incomplete_text_names_the_reason():
    assert validation_incomplete_text("视觉服务不可用") == "语义检查未完成：视觉服务不可用"
