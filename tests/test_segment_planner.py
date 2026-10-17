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


def test_ref_plan_accepts_11_to_15_but_base_stays_conservative():
    """规划验收闸分路线（ADR 0012）：ref 档 4–15（59- 节点契约，issue #46）；
    base 保持 4–10 保守档**硬闸**——工作流 62 节点档未复核，放行 11–15s 就是让
    不听话的 LLM 规划出 ComfyUI 可能选不出时长的段（「≤15s 路径不受影响」）。"""
    text = ('{"segments": [{"index": 0, "start_s": 0, "end_s": 11, "shots_in_segment": [1],'
            ' "summary": "A", "start_state": "s0", "end_hook": "e0"}]}')
    assert parse_segment_plan(text, total_s=11, mode="ref") is not None
    assert parse_segment_plan(text, total_s=11) is None  # base：11s 拒收（行为与 #46 前一致）


def test_reject_too_long_segment():
    text = '{"segments": [{"index": 0, "start_s": 0, "end_s": 16, "shots_in_segment": [1]}]}'
    assert parse_segment_plan(text, total_s=16) is None
    ref_text = ('{"segments": [{"index": 0, "start_s": 0, "end_s": 16, "shots_in_segment": [1],'
                ' "summary": "A", "start_state": "s0", "end_hook": "e0"}]}')
    assert parse_segment_plan(ref_text, total_s=16, mode="ref") is None  # ref 上限 15


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


# ---------------------------------------------------------------------------
# 多参分段（issue #46）：mode="ref" 的规划产物契约
# 多参无桥接帧：end_hook 不再是「下段首帧锚」，与 start_state 一起作为**起止状态声明**
# 进每段提示词（时间轴注记的对位依据），故两者缺一即拒收（拒收→调用方回退机械拆分）。
# ---------------------------------------------------------------------------

def test_parse_ref_plan_requires_start_and_end_states():
    """ref 规划缺 start_state（或缺末态）→ None：状态声明是每段提示词的必带内容。"""
    no_start = ('{"segments": [{"index": 0, "start_s": 0, "end_s": 8,'
                ' "shots_in_segment": [1], "summary": "A", "end_hook": "她合上书"}]}')
    no_end = ('{"segments": [{"index": 0, "start_s": 0, "end_s": 8,'
              ' "shots_in_segment": [1], "summary": "A", "start_state": "她站在门口"}]}')
    assert parse_segment_plan(no_start, total_s=8, mode="ref") is None
    assert parse_segment_plan(no_end, total_s=8, mode="ref") is None


def test_parse_ref_plan_carries_states():
    text = ('{"segments": [{"index": 0, "start_s": 0, "end_s": 8, "shots_in_segment": [1],'
            ' "summary": "A", "start_state": "the girl stands at the gate",'
            ' "end_hook": "the girl hands the key over"}]}')
    plans = parse_segment_plan(text, total_s=8, mode="ref")
    assert plans is not None
    assert plans[0].start_state == "the girl stands at the gate"
    assert plans[0].end_hook == "the girl hands the key over"


def test_base_plan_still_needs_no_start_state():
    """首尾帧链（base）的 plan 契约不变：end_hook 之外不新增必填字段。"""
    text = ('{"segments": [{"index": 0, "start_s": 0, "end_s": 8, "shots_in_segment": [1],'
            ' "summary": "A", "end_hook": "钩子"}]}')
    plans = parse_segment_plan(text, total_s=8)
    assert plans is not None
    assert plans[0].start_state == ""


def test_parse_ref_plan_rejects_boundaries_off_shot_cuts():
    """「边界落在镜头切点」（issue #46 AC1）：给定切点表时，内部边界不在切点上即拒收。

    例外：镜头本身超长（>15s）时允许镜内切——那是「一个长镜头拆成多个执行段」
    （CONTEXT.md「Segment」）唯一允许的非切点边界。
    """
    cuts = (0, 5, 9, 14, 20)
    off_cut = ('{"segments": ['
               '{"index": 0, "start_s": 0, "end_s": 7, "shots_in_segment": [1], "summary": "A",'
               '  "start_state": "s0", "end_hook": "e0"},'
               '{"index": 1, "start_s": 7, "end_s": 20, "shots_in_segment": [2], "summary": "B",'
               '  "start_state": "s1", "end_hook": "e1"}]}')
    assert parse_segment_plan(off_cut, total_s=20, mode="ref", shot_cuts=cuts) is None

    on_cut = ('{"segments": ['
              '{"index": 0, "start_s": 0, "end_s": 9, "shots_in_segment": [1], "summary": "A",'
              '  "start_state": "s0", "end_hook": "e0"},'
              '{"index": 1, "start_s": 9, "end_s": 20, "shots_in_segment": [2], "summary": "B",'
              '  "start_state": "s1", "end_hook": "e1"}]}')
    plans = parse_segment_plan(on_cut, total_s=20, mode="ref", shot_cuts=cuts)
    assert plans is not None
    assert [p.start_s for p in plans] == [0, 9]


def test_parse_ref_plan_allows_split_inside_overlong_shot():
    """单镜 17s（>15）：镜内边界合法（否则该镜头根本装不进 4–15s 窗口）。"""
    cuts = (0, 17)
    split_inside = ('{"segments": ['
                    '{"index": 0, "start_s": 0, "end_s": 10, "shots_in_segment": [1], "summary": "A",'
                    '  "start_state": "s0", "end_hook": "e0"},'
                    '{"index": 1, "start_s": 10, "end_s": 17, "shots_in_segment": [1], "summary": "B",'
                    '  "start_state": "s1", "end_hook": "e1"}]}')
    plans = parse_segment_plan(split_inside, total_s=17, mode="ref", shot_cuts=cuts)
    assert plans is not None


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


# ---------------------------------------------------------------------------
# 用户修订进分段规划（issue #26）
# 规划层产出段边界与每段剧情概述——它看不到修订，段划分就可能与用户要求矛盾。
# 但**不依赖 state**：修订沿用 frame_context 的既有模式，经显式入参进来。
# ---------------------------------------------------------------------------

def test_plan_segments_passes_revision_context_to_planner():
    """规划请求必须带上累积修订与注入优先序。"""
    from minimax_h3_prompt.segment_prompts import render_revision_context

    llm = FakeLLM()
    ctx = render_revision_context(
        {"user_revisions": [{"round": 1, "layer": "frame", "text": "汉服改成现代审美"}]})
    plans = plan_segments("分镜表：[Shot 1] 深夜空店内……", 5, llm, revision_context=ctx)

    assert plans is not None
    assert "汉服改成现代审美" in llm.last_request
    assert "首帧读图结果 > 用户累积修订 > 分镜表" in llm.last_request


def test_plan_segments_without_revisions_omits_block_and_never_takes_state():
    """没有修订时不注入空块；且规划函数**不依赖 state**——修订只能经显式入参进来。"""
    import inspect

    llm = FakeLLM()
    plan_segments("分镜表：[Shot 1] ……", 5, llm)

    assert "用户累积修订" not in llm.last_request
    assert "state" not in inspect.signature(plan_segments).parameters


# ---------------------------------------------------------------------------
# 多参分段规划（issue #46）：mode="ref"
# 多参无桥接帧（连续性靠共享参考素材＋起止状态声明＋时间轴注记对位）；issue #9
# 「分段恒单图开场锚」属首尾帧链，不许被误用到多参分段。
# ---------------------------------------------------------------------------

_REF_PLAN_JSON = (
    '{"segments": [{"index": 0, "start_s": 0, "end_s": 9, "shots_in_segment": [1],'
    ' "summary": "A", "start_state": "the girl stands at the gate",'
    ' "end_hook": "the girl hands the key over"}]}'
)


def test_plan_segments_ref_mode_requests_shot_cut_boundaries_and_states():
    """ref 规划请求必须钉三件事：边界落在镜头切点、4–15s 整数档、起止状态（英文进提示词）。"""
    llm = FakeLLM(_REF_PLAN_JSON)
    plans = plan_segments("分镜表：[Shot 1] ……", 9, llm,
                          mode="ref", shot_cuts=(0, 5, 9))
    assert plans is not None
    request = llm.last_request
    assert "镜头切点" in request
    assert "4-15" in request or "4–15" in request
    assert "start_state" in request and "end_hook" in request
    assert "英文" in request  # 状态声明进英文提示词正文，规划侧直接给英文


def test_plan_segments_ref_mode_rejects_off_cut_reply():
    """规划回复的边界不在切点上 → None（回退机械拆分），不把坏边界交给写段。"""
    off_cut = (
        '{"segments": ['
        '{"index": 0, "start_s": 0, "end_s": 7, "shots_in_segment": [1], "summary": "A",'
        '  "start_state": "s0", "end_hook": "e0"},'
        '{"index": 1, "start_s": 7, "end_s": 12, "shots_in_segment": [2], "summary": "B",'
        '  "start_state": "s1", "end_hook": "e1"}]}'
    )
    llm = FakeLLM(off_cut)
    assert plan_segments("分镜表：[Shot 1] ……", 12, llm,
                         mode="ref", shot_cuts=(0, 5, 8, 12)) is None


# ---------------------------------------------------------------------------
# 机械拆段（issue #46「不均匀硬切，边界落在镜头切点」）：规划失败回退的确定性底座
# ---------------------------------------------------------------------------

from minimax_h3_prompt.segment_planner import pack_shots_into_segments  # noqa: E402


def _check_packed(segments, total_s, cuts):
    """公共结构断言：整数边界、4–15s、无缝覆盖全程。

    「边界落切点」不在这查：借秒/回挪产生的必要镜内边界与超长镜头拆分都是合法例外，
    精确到边界的期望由各用例自己断言（切点纪律见 test_pack_keeps_boundaries_on_shot_cuts）。
    """
    assert segments[0][0] == 0 and segments[-1][1] == total_s
    for (s0, e0), (s1, e1) in zip(segments, segments[1:]):
        assert e0 == s1  # 无缝
    for s, e in segments:
        assert float(s).is_integer() and float(e).is_integer()
        assert 4 <= e - s <= 15


def test_pack_keeps_boundaries_on_shot_cuts():
    """常规镜头群：段边界全部落在切点上（不均匀硬切）。"""
    cuts = [0, 5, 9, 14, 20]
    segments = pack_shots_into_segments([(0, 5), (5, 9), (9, 14), (14, 20)], 20)
    _check_packed(segments, 20, cuts)
    assert all(b in cuts for _, b in segments[:-1])
    assert len(segments) >= 2  # 20s 必然拆多段


def test_pack_merges_short_shots_to_reach_minimum():
    """碎镜（<4s）并邻：2s+2s 并成 4s 段，边界仍在切点上。"""
    cuts = [0, 2, 4, 17]
    segments = pack_shots_into_segments([(0, 2), (2, 4), (4, 17)], 17)
    _check_packed(segments, 17, cuts)
    assert segments[0] == (0, 4)


def test_pack_splits_overlong_shot_inside():
    """单镜 17s（>15）：镜内再切（唯一允许的非切点边界），整数档、末块吸收余数。"""
    cuts = [0, 17]
    segments = pack_shots_into_segments([(0, 17)], 17)
    _check_packed(segments, 17, cuts)
    assert len(segments) == 2


def test_pack_rebalances_tiny_window_before_long_shot():
    """首镜 3s 紧跟 13s 长镜：3s 单独成段违反 4s 下限 → 从长镜借 1s（必要镜内边界）。"""
    cuts = [0, 3, 16]
    segments = pack_shots_into_segments([(0, 3), (3, 16)], 16)
    _check_packed(segments, 16, cuts)
    assert segments == [(0, 4), (4, 16)]


def test_pack_pulls_back_from_previous_when_tail_under_min():
    """末段 2s 不够 4s 下限 → 从上一段挪秒（上段保持 ≥4s），而不是交出违规段。"""
    cuts = [0, 15, 17]
    segments = pack_shots_into_segments([(0, 15), (15, 17)], 17)
    _check_packed(segments, 17, cuts)
    assert segments == [(0, 13), (13, 17)]


def test_pack_single_short_film_keeps_one_segment():
    """≤15s 整条（打包入口不该走到这，但纯函数要定死行为）：原样单段。"""
    assert pack_shots_into_segments([(0, 9)], 9) == [(0, 9)]


def test_ref_planner_instruction_declares_no_bridge_semantics():
    """ref 规划指令必须写明锚定语义（参考素材承担、无桥接帧），不许沿用首尾帧链的
    「下段首帧锚」语义（issue #9 裁定不进多参）。"""
    from minimax_h3_prompt.segment_planner import _load_instruction

    instruction = _load_instruction("ref")
    assert "参考素材" in instruction
    assert "桥接帧" in instruction
    assert "start_state" in instruction
    # 「首帧锚」只允许以**否定式**出现（声明 end_hook 不是它）——凡提及必为免责
    anchor_lines = [l for l in instruction.splitlines() if "首帧锚" in l]
    assert anchor_lines, "应当显式声明 end_hook 与首尾帧链的『下段首帧锚』无关"
    assert all("不是" in l for l in anchor_lines)
