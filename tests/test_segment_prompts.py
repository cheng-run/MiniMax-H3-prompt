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
    # 每段只含自己的镜头块（指令行里的交叉引用 `(from [Shot 1])` 允许保留）
    assert "[Shot 1] Live-action" not in segments[0].text or segments[0].shot_number == 1
    assert "[Shot 1] Live-action" not in segments[1].text
    assert "[Shot 2]" not in segments[0].text
    assert "[Shot 1] Live-action" not in segments[2].text
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


# ---------------------------------------------------------------------------
# T2（issue #3）：soundscape/music 按段重写为英文摘要句（官方 §4.6/§4.7），
# 删除时间窗裁切措辞
# ---------------------------------------------------------------------------

class FakeLLM:
    def __init__(self, reply="rewritten"):
        self.last_request = ""
        self._reply = reply

    def invoke(self, request):
        self.last_request = request
        return self._reply


def test_rewrite_instruction_no_crop_wording():
    """回退流重写指令不再要求「按时间窗裁切/删除」，改为按段重写英文摘要句。"""
    segments = split_shots_from_prompt(SAMPLE_PROMPT, total_duration=12.0)
    llm = FakeLLM()
    rewrite_segment_prompt(segments[1], SAMPLE_PROMPT, llm)
    request = llm.last_request
    assert "一律删除" not in request
    assert "只保留本段时间窗内" not in request
    # 官方 §4.6/§4.7 纪律：英文摘要句 + 句数上限 + 无时间戳
    assert "1-4" in request
    assert "1-3" in request
    assert "English" in request
    assert "no timestamps" in request or "无时间戳" in request


def test_rewrite_instruction_asks_rewrite_not_crop():
    """指令语义是「为本段重写」而非「从整条裁出」。"""
    segments = split_shots_from_prompt(SAMPLE_PROMPT, total_duration=12.0)
    llm = FakeLLM()
    rewrite_segment_prompt(segments[1], SAMPLE_PROMPT, llm)
    request = llm.last_request
    assert "按段重写" in request or "rewrite" in request.lower()


def test_segment_v2_request_english_summary_soundscape():
    """v2 主路径：字段 4/5 要求英文摘要句（1-4/1-3 句、无时间戳），不再裁时间窗。"""
    from minimax_h3_prompt.segment_planner import SegmentPlan
    from minimax_h3_prompt.segment_prompts import build_segment_v2_request

    plan = SegmentPlan(index=1, start_s=4, end_s=10, shots_in_segment=(2,),
                       summary="猫进店跳上柜台", end_hook="猫前爪搭上柜台沿")
    request = build_segment_v2_request(plan, [plan], {"shot_table": "x"}, None)
    # soundscape/music 字段要求英文摘要句
    assert "1-4 句" in request and "1-3 句" in request
    assert "English" in request
    # 裁切措辞已删
    assert "超出的一律删" not in request
    assert "只保留本段时间窗内的配乐" not in request


# ---------------------------------------------------------------------------
# T3（issue #5）：提示词模板产出官方英文格式（I2VA 指令行 + 锚定句 + 中文摘要层）
# ---------------------------------------------------------------------------

def _v2_plans():
    from minimax_h3_prompt.segment_planner import SegmentPlan
    return [
        SegmentPlan(index=0, start_s=0, end_s=4, shots_in_segment=(1,),
                    summary="深夜便利店全景，店员打盹，片尾玻璃门被顶开窄缝",
                    end_hook="玻璃门刚被顶开窄缝，猫眼从门缝探出"),
        SegmentPlan(index=1, start_s=4, end_s=10, shots_in_segment=(2,),
                    summary="橘猫进店跳上柜台蹭店员的手",
                    end_hook="猫前爪搭上柜台沿"),
    ]


def test_segment_v2_request_official_format_discipline():
    """v2 请求模板要求官方英文格式：I2VA 指令行、英文正文、节拍纪律、单 Shot 默认。"""
    from minimax_h3_prompt.segment_prompts import build_segment_v2_request

    plans = _v2_plans()
    request = build_segment_v2_request(plans[1], plans, {"shot_table": "x"}, None)
    # 官方结构要求出现：I2VA 指令行模板
    assert "For the target video, at 0.00 seconds into the target video," in request
    assert "<Picture 1> (from [Shot 1]) is fully referenced." in request
    # 实体首次出场纪律
    assert "首次出场" in request
    # 节拍距段尾 ≥1s 纪律
    assert "距段尾" in request or "1s" in request
    # 单 Shot 默认
    assert "[Shot 1]" in request


def test_segment_v2_request_no_legacy_structures():
    """v2 请求不再含 GLOBAL_LOCK/BRIDGE_FROM/END_HOOK 组装块。"""
    from minimax_h3_prompt.segment_prompts import build_segment_v2_request

    plans = _v2_plans()
    request = build_segment_v2_request(plans[1], plans, {"shot_table": "x"}, None)
    # 组装块不再注入；模板「禁止」节的禁令说明（不写 `X:`）允许出现
    assert "GLOBAL_LOCK（" not in request
    assert "BRIDGE_FROM（" not in request
    assert "END_HOOK（" not in request
    # 不存在任何把 X: 字段注入请求正文的组装行（禁令行都是「不写 `X:`」形式）
    for marker in ("GLOBAL_LOCK", "BRIDGE_FROM", "END_HOOK"):
        assert f"不写 `{marker}:`" in request  # 禁令在
        lines_with_marker = [ln for ln in request.splitlines()
                             if marker in ln and not ln.strip().startswith(("- 不写", "；"))]
        assert lines_with_marker == [], f"{marker} 出现在非禁令行：{lines_with_marker}"


def test_write_segment_v2_output_official_shape():
    """v2 写段 mock：模板纪律 + validator 断言新产出过 T1 新裁判。"""
    from minimax_h3_prompt.segment_planner import SegmentPlan
    from minimax_h3_prompt.segment_prompts import I2VA_ANCHOR_LINE, write_segment_v2
    from minimax_h3_prompt.tools.h3_validator import validate_base

    official_reply = (
        f"{I2VA_ANCHOR_LINE}\n\n"
        "integrated_multimodal_description: [Shot 1] Live-action, cinematic, "
        "a close shot frames an orange tabby cat landing on a checkout counter "
        "under warm light. At 00:02.000, the cat rubs its head against the "
        "clerk's hand. The scene settles on the cat leaning into her palm.\n\n"
        "overall_soundscape: A low refrigerator hum continues while soft purring "
        "rises near the counter. The clerk's sleeve rustles as the cat leans in.\n\n"
        "non_diegetic_music: N/A"
    )

    plans = _v2_plans()
    text = write_segment_v2(plans[1], plans, {"shot_table": "x"}, None, FakeLLM(official_reply))
    assert text == official_reply
    issues = validate_base(text, duration=6.0, variant="I2VA")
    assert issues == []


def test_split_keeps_i2va_instruction_line_for_later_segments():
    """官方 I2VA 指令行含 `(from [Shot 1])`，机械拆分后续段必须保留（T3 评审发现）。"""
    segments = split_shots_from_prompt(SAMPLE_PROMPT, total_duration=12.0)
    assert "fully referenced" in segments[1].text
    assert "fully referenced" in segments[2].text
