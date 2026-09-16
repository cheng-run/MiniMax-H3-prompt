"""segment planner（4-10s 整秒分段规划）测试。"""
from minimax_h3_prompt.segment_planner import (
    SegmentPlan,
    parse_segment_plan,
)


def _sample(**overrides):
    payload = {
        "index": 0,
        "start_s": 0,
        "end_s": 4,
        "shots_in_segment": [1],
        "summary": "开场",
        "end_hook": "她起身走出教室",
    }
    payload.update(overrides)
    return payload


def test_parse_valid_two_segments():
    text = """{"segments": [
        {"index": 0, "start_s": 0, "end_s": 4, "shots_in_segment": [1], "summary": "A", "end_hook": "钩子A"},
        {"index": 1, "start_s": 4, "end_s": 9, "shots_in_segment": [2], "summary": "B", "end_hook": "钩子B"}
    ]}"""
    plans = parse_segment_plan(text, total_s=9)
    assert plans is not None
    assert len(plans) == 2
    assert plans[0].duration_s == 4
    assert plans[1].start_s == 4
    assert plans[1].end_hook == "钩子B"


def test_reject_non_integer_boundary():
    # start_s = "4.5" 应int化失败
    text = '{"segments": [{"index": 0, "start_s": "4.5", "end_s": 9, "shots_in_segment": [1]}]}'
    assert parse_segment_plan(text, total_s=9) is None


def test_reject_too_short_segment():
    text = '{"segments": [{"index": 0, "start_s": 0, "end_s": 2, "shots_in_segment": [1]}]}'
    assert parse_segment_plan(text, total_s=2) is None


def test_reject_too_long_segment():
    text = '{"segments": [{"index": 0, "start_s": 0, "end_s": 11, "shots_in_segment": [1]}]}'
    assert parse_segment_plan(text, total_s=11) is None


def test_reject_coverage_gap():
    text = """{"segments": [
        {"index": 0, "start_s": 0, "end_s": 4, "shots_in_segment": [1]},
        {"index": 1, "start_s": 6, "end_s": 10, "shots_in_segment": [2]}
    ]}"""
    assert parse_segment_plan(text, total_s=10) is None


def test_reject_incomplete_coverage():
    # 最后一段没覆盖到 total_s
    text = '{"segments": [{"index": 0, "start_s": 0, "end_s": 4, "shots_in_segment": [1]}]}'
    assert parse_segment_plan(text, total_s=9) is None


def test_end_can_extend_past_total():
    # 末段允许略长（4-10s 硬约束，超 1-2 秒留剪辑余量）
    text = '{"segments": [{"index": 0, "start_s": 0, "end_s": 10, "shots_in_segment": [1]}]}'
    plans = parse_segment_plan(text, total_s=9)
    assert plans is not None
    assert plans[0].end_s == 10


def test_json_in_fence():
    text = '```json\n{"segments": [{"index": 0, "start_s": 0, "end_s": 5, "shots_in_segment": [1], "summary": "A", "end_hook": "B"}]}\n```'
    plans = parse_segment_plan(text, total_s=5)
    assert plans is not None
    assert plans[0].summary == "A"


def test_garbage_returns_none():
    assert parse_segment_plan("not json at all", total_s=10) is None
