"""prompt 按镜头拆分 + CLI 主题/项目自动探测测试。"""
from pathlib import Path

import pytest

from minimax_h3_prompt.main import _resolve_topic_project, main
from minimax_h3_prompt.project_models import Shot, ShotPlan
from minimax_h3_prompt.project_store import ProjectStore
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


def _seed_project(root: Path, topic_id: str, project_id: str) -> None:
    store = ProjectStore(root)
    store.init_project(topic_id, project_id, "自动探测", duration_seconds=12.0)
    doc = store.load_project(topic_id, project_id)
    shot = Shot(shot_id="SH001", shot_number=1, duration_seconds=12.0,
                start_state="s", action="a", end_state="e")
    store.update_shot_plan(topic_id, project_id, ShotPlan(
        doc.shot_plan.shot_plan_id, project_id, "FL2VA", 12.0, (shot,),
    ), overwrite=True)


def test_cli_segment_plan_auto_detect_ids(tmp_path, capsys):
    root = tmp_path / "Assets"
    _seed_project(root, "paper-plane", "project-001")

    code = main(["project", "segment-plan", "--root", str(root)])

    assert code == 0
    out = capsys.readouterr().out
    assert "自动识别" in out
    document = ProjectStore(root).load_project("paper-plane", "project-001")
    segments = document.shot_plan.shots[0].segments
    assert len(segments) == 2  # 12s → 6+6
    assert segments[1].prev_segment_id == segments[0].segment_id


def test_shots_from_prompt_infers_durations():
    shots = shots_from_prompt(SAMPLE_PROMPT, total_duration=12.0)
    assert [s.shot_id for s in shots] == ["SH001", "SH002", "SH003"]
    assert [s.duration_seconds for s in shots] == pytest.approx([5.0, 5.0, 2.0])
    assert shots[1].previous_shot_id == "SH001"
    assert shots[2].start_state_derived_from == "SH002"
    assert shots[0].previous_shot_id == ""
    assert "wheat field" in shots[0].action


def test_plan_segments_synthesizes_from_wizard_prompt(tmp_path):
    """向导项目 ShotPlan 为空：从 generations/*/video-prompt.md 反推镜头再拆段。"""
    from minimax_h3_prompt.execution import plan_segments

    root = tmp_path / "Assets"
    store = ProjectStore(root)
    store.init_project("wizard-topic", "project-001", "机器人", duration_seconds=12.0)
    gen_dir = root / "wizard-topic" / "projects" / "project-001" / "generations" / "GEN001"
    gen_dir.mkdir(parents=True)
    (gen_dir / "video-prompt.md").write_text(SAMPLE_PROMPT, encoding="utf-8")

    result = plan_segments(store, "wizard-topic", "project-001")

    assert result["synthesized_from_prompt"] is True
    assert len(result["segments"]) == 3  # 5s/5s/2s 都在执行窗口内，不再拆分
    document = store.load_project("wizard-topic", "project-001")
    assert len(document.shot_plan.shots) == 3
    assert document.shot_plan.shots[0].segments[0].segment_id == "SEG01-SH001a"


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


def test_resolve_ids_ambiguous_raises(tmp_path):
    root = tmp_path / "Assets"
    _seed_project(root, "topic-a", "project-001")
    _seed_project(root, "topic-b", "project-001")
    store = ProjectStore(root)
    with pytest.raises(ValueError, match="--topic-id"):
        _resolve_topic_project(store, None, None)
