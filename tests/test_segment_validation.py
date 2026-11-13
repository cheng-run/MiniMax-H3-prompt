"""D6（#246）：段交接的两道判据（同步技术档 + 异步语义闸门）。

纯函数层用例：段号/技术档自检「复用 check_segment_video 而不是另写一套」靠**帧数档**
咬住（4s 视频 107 帧 vs 6s 计划必须报 error），语义判据的否定词优先级单列（``未达到``
里含 ``达到``，顺序反了会把不合格判成合格）。
"""
from __future__ import annotations

import pytest

from minimax_h3_prompt.segment_validation import (
    build_end_hook_prompt,
    judge_end_hook,
    parse_end_hook_verdict,
    segment_submission_problems,
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
# 语义判据
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("达到", True),
    ("未达到", False),
    ("没有达到该末态", False),
    ("NOT REACHED", False),
    ("Yes, reached.", True),
    ("", None),
    ("我不知道", None),
])
def test_parse_end_hook_verdict(text, expected):
    assert parse_end_hook_verdict(text) is expected


def test_judge_end_hook_feeds_end_hook_and_description_to_the_model():
    llm = _StubLlm("未达到")
    assert judge_end_hook("人物停在门口", "尾帧里人物还在走动", llm) is False
    assert len(llm.prompts) == 1
    prompt = llm.prompts[0]
    assert "人物停在门口" in prompt
    assert "尾帧里人物还在走动" in prompt


def test_judge_end_hook_unparseable_reply_raises():
    """判据服务给了没法用的回复 = 系统故障，调用方要落 failed，不能静默当「未达到」。"""
    with pytest.raises(ValueError):
        judge_end_hook("人物停在门口", "尾帧里人物还在走动", _StubLlm("嗯……"))


def test_judge_end_hook_without_end_hook_passes():
    """旧 plan.json 缺 end_hook → 无末态可判，不阻断（与 CLI 诚实降级同向）。"""
    llm = _StubLlm("未达到")
    assert judge_end_hook("", "任意画面", llm) is True
    assert llm.prompts == []


def test_build_prompt_places_expected_state_and_description_side_by_side():
    prompt = build_end_hook_prompt("灯亮起", "画面仍是暗的")
    assert "灯亮起" in prompt and "画面仍是暗的" in prompt
