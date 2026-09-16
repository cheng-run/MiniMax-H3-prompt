"""prompt 按镜头拆分与逐段重写测试（纯文本逻辑，不依赖资产库）。"""
import pytest

from minimax_h3_prompt.segment_prompts import (
    is_degenerate_durations,
    rewrite_segment_prompt,
    shots_from_prompt,
    split_shots_from_prompt,
)

SAMPLE_PROMPT = """subject_definitions: <Subject 1> a paper plane.

summary: [任务类型] FL2VA test.

retention_analysis: keep the plane consistent.

For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.

[Shot 1] Live-action, the plane glides over a wheat field at dusk.
[Shot 2] At 00:05.000, the camera cuts to the plane diving through clouds.
[Shot 3] At 00:10.000, the shot cuts to the plane landing on a sill.

overall_soundscape: soft wind and distant birds.

non_diegetic_music: gentle piano.
"""


BARE_TIMESTAMP_SAMPLE = """[Shot 1] Close-up of a woman at the door.
At 00:05.000, a cut to the hallway.
At 00:10.000, the hallway is empty.

overall_soundscape: quiet room.

non_diegetic_music: piano.
"""


def test_split_shots_with_bare_timestamps():
    """LLM 漏标 [Shot N]、只写行首 At MM:SS.mmm 时，拆分器应自动补标镜头号。"""
    segments = split_shots_from_prompt(BARE_TIMESTAMP_SAMPLE, total_duration=12.0)
    assert [s.shot_number for s in segments] == [1, 2, 3]
    assert [s.start_seconds for s in segments] == [0.0, 5.0, 10.0]
    # 拆分后的单段里时间戳归零
    assert "At 00:05.000" not in segments[1].text
    assert segments[1].text.count("[Shot 2]") == 1


def test_split_shots_basic():
    segments = split_shots_from_prompt(SAMPLE_PROMPT)
    assert [s.shot_number for s in segments] == [1, 2, 3]
    # 每段都含全局前缀与声音后缀
    for segment in segments:
        assert "subject_definitions:" in segment.text
        assert "overall_soundscape:" in segment.text
        assert "non_diegetic_music:" in segment.text
    # 每段只含自己的镜头块
    assert "[Shot 2]" not in segments[0].text
    assert "[Shot 1]" not in segments[1].text
    # 时间戳归零：块首绝对时间戳被移除
    assert "At 00:05.000" not in segments[1].text
    assert "At 00:10.000" not in segments[2].text
    assert segments[1].text.count("[Shot 2]") == 1


def test_split_shots_single_or_empty():
    assert split_shots_from_prompt("") == []
    single = split_shots_from_prompt("just one prompt without shots")
    assert len(single) == 1
    assert single[0].text.startswith("just one prompt")


def test_shots_from_prompt_infers_durations():
    shots = shots_from_prompt(SAMPLE_PROMPT, total_duration=12.0)
    assert [s.shot_id for s in shots] == ["SH001", "SH002", "SH003"]
    assert [s.duration_seconds for s in shots] == pytest.approx([5.0, 5.0, 2.0])
    assert shots[1].previous_shot_id == "SH001"
    assert shots[2].start_state_derived_from == "SH002"
    assert shots[0].previous_shot_id == ""
    assert "wheat field" in shots[0].action


def test_shot_prompt_carries_time_window():
    segments = split_shots_from_prompt(SAMPLE_PROMPT, total_duration=12.0)
    assert [s.start_seconds for s in segments] == [0.0, 5.0, 10.0]
    assert [s.duration_seconds for s in segments] == pytest.approx([5.0, 5.0, 2.0])


def test_rewrite_segment_prompt_uses_llm_and_window():
    class FakeLLM:
        def __init__(self):
            self.last_request = ""
        def invoke(self, request):
            self.last_request = request
            return "For the target video ... [Shot 1] rewritten, scoped."

    segments = split_shots_from_prompt(SAMPLE_PROMPT, total_duration=12.0)
    llm = FakeLLM()
    rewritten = rewrite_segment_prompt(segments[1], SAMPLE_PROMPT, llm)
    assert rewritten.startswith("For the target video")
    assert "5s" in llm.last_request  # 时间窗已传入（整数秒格式）
    assert "Shot 2" in llm.last_request


def test_rewrite_segment_prompt_fallback_on_llm_error():
    class BrokenLLM:
        def invoke(self, request):
            raise RuntimeError("quota exhausted")

    segments = split_shots_from_prompt(SAMPLE_PROMPT, total_duration=12.0)
    assert rewrite_segment_prompt(segments[0], SAMPLE_PROMPT, BrokenLLM()) is None


def test_is_degenerate_durations():
    assert is_degenerate_durations([5.0, 5.0, 5.0]) is True
    assert is_degenerate_durations([5.0, 5.0]) is False       # 不足 3 镜不判定
    assert is_degenerate_durations([4.0, 6.0, 5.0]) is False
    assert is_degenerate_durations([]) is False


def test_storyboard_uniform_durations_warning():
    """_shot_durations_from_table 应把时间戳镜头表展开为逐镜头时长。"""
    from minimax_h3_prompt.graph.nodes import _shot_durations_from_table

    table = (
        "[Shot 1] opens.\n"
        "[Shot 2] At 00:05.000, mid.\n"
        "[Shot 3] At 00:10.000, close-up.\n"
    )
    durations = _shot_durations_from_table(table, total_duration=15.0)
    assert durations == [5.0, 5.0, 5.0]
    assert is_degenerate_durations(durations) is True
