"""segment planner（4-10s 整秒分段规划）测试。"""
from minimax_h3_prompt.segment_planner import (
    FRAME_CONTEXT_HEADER,
    SegmentPlan,
    _load_instruction,
    parse_segment_plan,
    plan_segments,
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


# ---------------------------------------------------------------------------
# 首帧锚定：plan 层不能只由中文分镜表生成
# bug：分镜表写「空店 + 店员趴着」，用户提交的首帧图却是「猫已在店内」，
# plan 层（summary/end_hook）固化了「空店→猫进门」，段 1 就此跑偏。
# ---------------------------------------------------------------------------

SAMPLE_PLAN_JSON = (
    '{"segments": [{"index": 0, "start_s": 0, "end_s": 5, "shots_in_segment": [1],'
    ' "summary": "开场", "end_hook": "钩子"}]}'
)


class FakeLLM:
    def __init__(self, reply=SAMPLE_PLAN_JSON):
        self.last_request = ""
        self._reply = reply

    def invoke(self, request):
        self.last_request = request
        return self._reply


def test_plan_segments_passes_frame_context_to_planner():
    """规划请求必须带上真实首帧画面，并声明它压过分镜表。"""
    llm = FakeLLM()
    plans = plan_segments("分镜表：[Shot 1] 深夜空店内……", 5, llm,
                          frame_context="视频第一帧实际画面：猫已站在门口地砖上")
    assert plans is not None
    assert FRAME_CONTEXT_HEADER in llm.last_request
    assert "猫已站在门口地砖上" in llm.last_request


def test_plan_segments_without_frame_context_omits_anchor_block():
    """没有帧图读图结果时不注入空锚定块（T2VA 视频无关键帧）。"""
    llm = FakeLLM()
    plan_segments("分镜表：[Shot 1] ……", 5, llm)
    assert FRAME_CONTEXT_HEADER not in llm.last_request


def test_planner_instruction_treats_real_frame_as_truth():
    """规划师指令本身写明了：实际画面是唯一事实源，冲突时以画面为准。"""
    instruction = _load_instruction()
    assert "实际画面" in instruction
    assert "以画面为准" in instruction
    # 图中已有的事物不得写成「尚未出现」
    assert "尚未出现" in instruction
