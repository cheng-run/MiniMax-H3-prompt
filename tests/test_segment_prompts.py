"""prompt 按镜头拆分与逐段重写测试（纯文本逻辑，不依赖资产库）。"""
from pathlib import Path

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


def test_both_segment_paths_carry_coexist_discipline():
    """两条分段路径（v2 规划式 + 回退式）都必须带「快机位 + 多拍不同段共存」纪律。

    这是模板层的锚：改模板名/重构时纪律句丢了会在此报红（两条路径都要改，别只改主路径）。
    """
    from minimax_h3_prompt.segment_prompts import (
        _REWRITE_INSTRUCTION,
        _SEGMENT_V2_INSTRUCTION,
    )

    for name, tpl in (("v2", _SEGMENT_V2_INSTRUCTION), ("fallback", _REWRITE_INSTRUCTION)):
        assert "不得同段共存" in tpl, f"{name} 模板缺共存禁令"
        assert "降机位" in tpl, f"{name} 模板缺默认处置（降机位）"


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


# ---------------------------------------------------------------------------
# 首帧锚定：分段请求必须以「图片实际画面」为准
# bug：同一 GEN 里主提示词锚定了首帧真实画面，但喂给 H3 的 segments/shot-01.md
# 写的是分镜表里的「空店 + 店员趴着 + 无猫」→ 与首帧图直接矛盾，首段崩。
# ---------------------------------------------------------------------------

# 取自 2026-09-22 实测：用户提交的首帧图读图结果（截断版）
FIRST_FRAME_DESC = (
    "画面为竖构图，从便利店敞开的玻璃门外向内拍摄：室内灯光明亮，左侧是摆满瓶装与盒装商品的货架；"
    "右侧收银台后站着一名年轻男性店员；前景下方偏左处，一只橘色虎斑猫站在门口地砖上，尾巴高高竖起。"
)
LAST_FRAME_DESC = "猫的侧脸贴住店员手背，店员指腹没入猫头顶软毛，暖光自侧后勾出绒边。"


def _frame_state(**extra):
    state = {
        "shot_table": "[Shot 1] 深夜空店内，店员趴在柜台后……\n\n[Shot 2] At 00:04.500，橘猫顶开玻璃门……",
        "fl2va_frame_descriptions": [
            {"role": "first", "description": FIRST_FRAME_DESC},
            {"role": "last", "description": LAST_FRAME_DESC},
        ],
    }
    state.update(extra)
    return state


def test_segment_v2_request_carries_first_frame_description():
    """段 1 请求必须注入首帧读图结果，并声明画面压过分镜表。"""
    from minimax_h3_prompt.segment_prompts import build_segment_v2_request

    plans = _v2_plans()
    request = build_segment_v2_request(plans[0], plans, _frame_state(), None)
    assert FIRST_FRAME_DESC in request, "分段请求未注入首帧实际画面 → 段 1 会照分镜表写错开场状态"
    assert "唯一事实源" in request
    # 冲突时的裁定方向：分镜表让位于图片
    assert "以画面为准" in request


def test_segment_v2_request_carries_last_frame_description_for_final_segment():
    """末段请求必须注入尾帧读图结果，正文结尾要落到该状态。"""
    from minimax_h3_prompt.segment_prompts import build_segment_v2_request

    plans = _v2_plans()
    request = build_segment_v2_request(plans[-1], plans, _frame_state(), None)
    assert LAST_FRAME_DESC in request


# ---------------------------------------------------------------------------
# 本段 Shot 定位：只喂本段覆盖的镜头，不灌整条分镜表
# bug：state 里从没有 shot_text_N，每段都拿到整条 2226 字分镜表 → 其他时间窗的
# 事件被一起喂给 H3，与「时间窗纪律」直接冲突。
# ---------------------------------------------------------------------------

SHOT_TABLE = (
    "# 分镜镜头表\n\n"
    "## [Shot 1] 起 · 深夜的容器\n店员趴在暖光柜台后，台面内侧摆着白瓷碟小鱼干。\n\n"
    "## [Shot 2] 承 · 不速之客\nAt 00:04.500，橘猫顶开玻璃门，侧身挤入店内。\n\n"
    "## [Shot 3] 转 · 轻盈的登场\nAt 00:08.000，猫在柜台前蓄力跃起，四爪落上台面暖光锥中央。\n"
)


def test_segment_v2_request_scopes_shot_text_to_this_segment():
    """段 1 的请求只含 Shot 1 原文，其他镜头的剧情不得进入（时间窗纪律）。"""
    from minimax_h3_prompt.segment_prompts import build_segment_v2_request

    plans = _v2_plans()
    state = _frame_state(shot_table=SHOT_TABLE)
    request = build_segment_v2_request(plans[0], plans, state, None)
    assert "白瓷碟小鱼干" in request         # 本段 Shot 1 原文在
    assert "四爪落上台面暖光锥中央" not in request  # Shot 3（其他时间窗）不在
    assert "只含本段覆盖的镜头" in request


def test_segment_v2_request_handles_last_frame_only_variant():
    """仅尾帧模式（L2VA）：段 1 的 Picture 1 是尾帧图，不能写成首帧图。"""
    from minimax_h3_prompt.segment_prompts import build_segment_v2_request

    plans = _v2_plans()
    state = {
        "shot_table": SHOT_TABLE,
        "fl2va_frame_descriptions": [{"role": "last", "description": LAST_FRAME_DESC}],
    }
    request = build_segment_v2_request(plans[0], plans, state, None)
    assert "尾帧图" in request
    assert LAST_FRAME_DESC in request


def test_segment_v2_request_does_not_invite_negating_visible_entities():
    """模板不得把 `No cat is visible in the frame.` 当范例推荐——那正是首段跑偏的原句。"""
    from minimax_h3_prompt.segment_prompts import build_segment_v2_request

    request = build_segment_v2_request(_v2_plans()[0], _v2_plans(), _frame_state(), None)
    # 例句可以出现（说明「什么时候才该用」），但必须带上「首帧图里已有的实体绝不能否定」的限定
    assert "只有该实体在本段首帧（Picture 1）里确实不存在时" in request
    assert "绝不能" in request


def test_segment_v2_request_flags_unscoped_fallback():
    """分镜表没有 [Shot N] 标记时回退整表，但必须显式标注只准取本段内容。"""
    from minimax_h3_prompt.segment_prompts import build_segment_v2_request

    plans = _v2_plans()
    state = _frame_state(shot_table="整条分镜表但没有镜头标记：猫顶开门，跳上柜台。")
    request = build_segment_v2_request(plans[0], plans, state, None)
    assert "未能按 Shot 号定位本段镜头" in request
    assert "只准取本段时间窗内的内容" in request


# ---------------------------------------------------------------------------
# 回退路径（规划失败时）同样要锚定帧图
# bug：segment_planner 规划失败 → 回退 split_shots_from_prompt + rewrite_segment_prompt，
# 该分支不注入帧读图结果 → 规划一失败，首帧不锚定的缺陷原样复发。
# ---------------------------------------------------------------------------

def test_rewrite_segment_prompt_carries_first_frame_anchor():
    """回退路径的第一段同样要以首帧实际画面为锚。"""
    from minimax_h3_prompt.segment_prompts import rewrite_segment_prompt

    segments = split_shots_from_prompt(SAMPLE_PROMPT, total_duration=12.0)
    llm = FakeLLM()
    rewrite_segment_prompt(
        segments[0], SAMPLE_PROMPT, llm,
        state={"fl2va_frame_descriptions": [{"role": "first", "description": FIRST_FRAME_DESC}]},
        is_first=True,
    )
    assert FIRST_FRAME_DESC in llm.last_request
    assert "唯一事实源" in llm.last_request


def test_rewrite_segment_prompt_carries_last_frame_anchor():
    """回退路径的末段要带尾帧落点。"""
    from minimax_h3_prompt.segment_prompts import rewrite_segment_prompt

    segments = split_shots_from_prompt(SAMPLE_PROMPT, total_duration=12.0)
    llm = FakeLLM()
    rewrite_segment_prompt(
        segments[-1], SAMPLE_PROMPT, llm,
        state={"fl2va_frame_descriptions": [{"role": "last", "description": LAST_FRAME_DESC}]},
        is_last=True,
    )
    assert LAST_FRAME_DESC in llm.last_request


def test_rewrite_segment_prompt_without_state_stays_generic():
    """没有帧读图结果时不注入帧描述块（T2VA / 未提交帧图），只留通用锚定句。"""
    from minimax_h3_prompt.segment_prompts import rewrite_segment_prompt

    segments = split_shots_from_prompt(SAMPLE_PROMPT, total_duration=12.0)
    llm = FakeLLM()
    rewrite_segment_prompt(segments[0], SAMPLE_PROMPT, llm, is_first=True)
    assert "读图结果" not in llm.last_request  # 「读图结果」只随真实帧描述出现
    assert "Picture 1 是用户提交的首帧图" in llm.last_request


# ---------------------------------------------------------------------------
# 时间基 + 段落形状 + 交付前校验
#
# bug（2026-09-22，GEN003 实测）：v2 与回退两条重写路径都用「绝对片时」框定本段，
# 却从未规定正文里 `At 00:XX.XXX` 用哪个基准 → LLM 跨段随机选边：段 2/3 写成绝对秒
# （4.0/5.2/6.4/6.9，而该片段只有 4s），段 1/4 写成相对秒；人工验收坏的正是 2/3。
# 官方 base-en.txt：`target video` 指被生成的这条片段，切点时间必须落在时长内。
# 另外全部官方案例正文都以 `integrated_multimodal_description: [Shot 1] ` 开头，
# 缺该标记会让首行 `(from [Shot 1])` 引用悬空（本仓库 validator 直接报 NO_SHOT）。
#
# 而 validator 本就能用真实产物把好坏分开（段 2/3 报 LAST_TIMESTAMP_TOO_CLOSE_TO_END，
# 段 1/4 干净）——规则在、接线不在：分段是唯一「产出即交付」的路径，从不校验。
# ---------------------------------------------------------------------------

FIXTURES = Path(__file__).parent / "fixtures"


class SequencedLLM:
    """按顺序返回多条回复并记录每次请求；回复用完后一直返回最后一条。"""

    def __init__(self, replies: list[str]):
        self._replies = list(replies)
        self.requests: list[str] = []

    @property
    def calls(self) -> int:
        return len(self.requests)

    def invoke(self, request):
        self.requests.append(request)
        index = min(len(self.requests), len(self._replies)) - 1
        return self._replies[index]


def _bad_segment_text() -> str:
    """真实坏产物：GEN003 段 2（绝对时间戳 + 无 [Shot 1] 标记）。

    末尾换行按 ``write_segment_v2`` 的既有契约去掉（LLM 回复同样会被 strip）。
    """
    raw = (FIXTURES / "segment_absolute_timestamps_bad.md").read_text(encoding="utf-8")
    return raw.strip()


def _good_segment_text() -> str:
    """合规样本：直接用官方正样本 fixture（4-10s 时长零 error），不再手写一份。"""
    raw = (FIXTURES / "official_shot01_i2va.md").read_text(encoding="utf-8")
    return raw.strip()


def test_v2_request_declares_segment_relative_timebase():
    """v2 请求必须把写段时间基定死为「本段 0 起」，绝对片时只作剧情定位。"""
    from minimax_h3_prompt.segment_prompts import build_segment_v2_request

    plans = _v2_plans()  # 第 2 段：4-10s，时长 6s
    request = build_segment_v2_request(plans[1], plans, {"shot_table": "x"}, None)
    assert "0-6s" in request, "写段时间窗必须以本段 0 起，否则 LLM 会写绝对片时"
    assert "仅作剧情定位" in request, "绝对片时必须显式降级为「只用于定位、不得写进时间戳」"


def test_v2_request_teaches_shot1_marker():
    """模板示例必须带 [Shot 1] 标记，否则首行 (from [Shot 1]) 引用悬空。"""
    from minimax_h3_prompt.segment_prompts import build_segment_v2_request

    plans = _v2_plans()
    request = build_segment_v2_request(plans[1], plans, {"shot_table": "x"}, None)
    assert "[Shot 1] Live-action" in request


def test_rewrite_request_declares_segment_relative_timebase():
    """回退路径的逐段重写同样要定死时间基（两条路径都得改，别只改主路径）。"""
    from minimax_h3_prompt.segment_prompts import rewrite_segment_prompt

    segments = split_shots_from_prompt(SAMPLE_PROMPT, total_duration=15.0)
    assert segments[1].start_seconds == 5.0  # 第 2 镜：整片 5-10s，时长 5s
    llm = FakeLLM()
    rewrite_segment_prompt(segments[1], SAMPLE_PROMPT, llm)
    assert "0-5s" in llm.last_request
    assert "仅作剧情定位" in llm.last_request
    assert "绝对片时 5s – 10s" in llm.last_request  # 既有契约：绝对位置仍要传、且标明只作定位
    assert "Shot 2" in llm.last_request


def test_validate_segment_flags_real_bad_artifact():
    """真实坏产物必须被判不合格（这条是本次缺陷的可复现红线）。"""
    from minimax_h3_prompt.segment_prompts import validate_segment

    issues = validate_segment(_bad_segment_text(), 4.0, start_s=4.0)
    assert "LAST_TIMESTAMP_TOO_CLOSE_TO_END" in {i.code for i in issues if i.severity == "error"}, \
        "4s 片段的 6.9s 节拍没被拦下"
    assert "TIMESTAMPS_LOOK_ABSOLUTE" in {i.code for i in issues}


def test_validate_segment_catches_absolute_base_where_error_checks_cannot():
    """error 级抓不到的那半个洞：本段 start < duration 时绝对片时与合法写法同形。

    复现：段 2（start=4s、时长 6s）写成绝对 ``At 00:04.000``/``00:04.600``（应为 0.0/0.6），
    绝对秒落在 [0,6) 内 → error 级全绿。GEN003 段 2/3 被抓到只因 6.9s 超过了 4s 片段，
    凡是 start < duration 的段都会漏网，故用 warning 启发式补上。
    """
    from minimax_h3_prompt.segment_prompts import I2VA_ANCHOR_LINE, segment_errors, validate_segment

    text = (
        f"{I2VA_ANCHOR_LINE}\n\n"
        "integrated_multimodal_description: [Shot 1] Live-action, cinematic, a full shot "
        "frames the store. At 00:04.000, the door is nudged open. At 00:04.600, the cat "
        "slips in low. The scene settles on the door seam.\n\n"
        "overall_soundscape: A low hum sits under the quiet room tone.\n\n"
        "non_diegetic_music: N/A"
    )
    assert segment_errors(text, 6.0, start_s=4.0) == [], "前提失效：error 级本应抓不到这个洞"
    assert "TIMESTAMPS_LOOK_ABSOLUTE" in {i.code for i in validate_segment(text, 6.0, start_s=4.0)}

    # 合法的相对写法（本段第 4 秒）不得误报——这正是它只报 warning 的原因
    legal = text.replace("At 00:04.000", "At 00:00.000").replace("At 00:04.600", "At 00:00.600")
    assert "TIMESTAMPS_LOOK_ABSOLUTE" not in {i.code for i in validate_segment(legal, 6.0, start_s=4.0)}

    # 本段从整片 0 起时该启发式无意义（start_s == 0 不做此项检查）
    assert "TIMESTAMPS_LOOK_ABSOLUTE" not in {i.code for i in validate_segment(text, 6.0)}


def test_validate_segment_accepts_compliant_text():
    """合规产物零 error——否则重试循环会把好文本也判死。"""
    from minimax_h3_prompt.segment_prompts import validate_segment

    issues = validate_segment(_good_segment_text(), 6.0)
    assert [i for i in issues if i.severity == "error"] == []


def test_write_segment_v2_reasks_when_timestamps_overrun_segment():
    """不合格产出必须先重写；重写请求要带上校验反馈供 LLM 修正。"""
    from minimax_h3_prompt.segment_prompts import write_segment_v2

    llm = SequencedLLM([_bad_segment_text(), _good_segment_text()])
    plans = _v2_plans()  # 第 2 段时长 6s
    text = write_segment_v2(plans[1], plans, {"shot_table": "x"}, None, llm)

    assert llm.calls == 2, "首次产出不合格却没有重写"
    assert text == _good_segment_text()
    assert "LAST_TIMESTAMP_TOO_CLOSE_TO_END" in llm.requests[1], "重写请求没带上失败原因"


def test_write_segment_v2_retry_is_bounded_and_never_silent():
    """始终不合格时：有界重试，且交回原文（返回 None 会变成向导里的静默跳过）。"""
    from minimax_h3_prompt.segment_prompts import MAX_SEGMENT_ATTEMPTS, write_segment_v2

    llm = SequencedLLM([_bad_segment_text()])
    plans = _v2_plans()
    text = write_segment_v2(plans[1], plans, {"shot_table": "x"}, None, llm)

    assert llm.calls == MAX_SEGMENT_ATTEMPTS, "重试次数无界"
    assert text == _bad_segment_text()


# ---------------------------------------------------------------------------
# 「快机位 + 多拍动作」同段共存检查（issue #11，2026-09-23 钉 seed 2×2 实测裁定：
# error 级、多拍阈值 ≥3 个时间戳、物证正负样本在 tests/fixtures/flicker_arms/）
# ---------------------------------------------------------------------------

ARMS = Path(__file__).parent / "fixtures" / "flicker_arms"


def _arm(name: str) -> str:
    suffix = "" if name.endswith(".md") else ".txt"
    return (ARMS / f"{name}{suffix}").read_text(encoding="utf-8")


def test_coexist_check_flags_real_flaring_artifacts():
    """负样本必须报红：armA（00017，flicker 4.865）与 original（00012，4.163）。"""
    from minimax_h3_prompt.segment_prompts import validate_segment

    for name in ("armA_H1_state_only", "original_00012"):
        issues = validate_segment(_arm(name), 4.0)
        codes = {i.code for i in issues if i.severity == "error"}
        assert "FAST_CAMERA_MULTI_BEAT_COEXIST" in codes, f"{name}（致闪物证）未被报红"


def test_coexist_check_single_factor_green():
    """单因素不得误报：R1 只有快机位（2 拍，0.173）、R2 只有多拍（慢机位，0.78）。"""
    from minimax_h3_prompt.segment_prompts import validate_segment

    for name in ("R1_camera_only", "R2_action_only"):
        issues = validate_segment(_arm(name), 4.0)
        codes = {i.code for i in issues if i.severity == "error"}
        assert "FAST_CAMERA_MULTI_BEAT_COEXIST" not in codes, f"{name} 是单因素反例，不该报"


def test_coexist_check_positive_samples_clean():
    """票面正样本全绿：executed_00014（静态低运动 0.092）。"""
    from minimax_h3_prompt.segment_prompts import validate_segment

    issues = validate_segment(_arm("executed_00014"), 4.0)
    assert "FAST_CAMERA_MULTI_BEAT_COEXIST" not in {i.code for i in issues}


def test_coexist_check_runs_even_without_duration():
    """无时长（duration=None）时检查仍运行——判定只用绝对节拍数，
    漏传时长不得让检查静默失效（「规则在、接线不在」同型风险的护栏）。"""
    from minimax_h3_prompt.segment_prompts import validate_segment

    issues = validate_segment(_arm("armA_H1_state_only"))
    assert "FAST_CAMERA_MULTI_BEAT_COEXIST" in {i.code for i in issues}


def test_coexist_check_two_beats_still_green():
    """快机位 + 2 拍是合法边界（R1 物证 0.173），加一个节拍才越线。"""
    from minimax_h3_prompt.segment_prompts import I2VA_ANCHOR_LINE, validate_segment

    text = (
        f"{I2VA_ANCHOR_LINE}\n\n"
        "integrated_multimodal_description: [Shot 1] Live-action, cinematic, a fast low "
        "tracking shot with small amplitude at fast speed frames the store. "
        "At 00:00.500, the door swings open. At 00:02.000, the cat lands on the counter. "
        "The shot settles on the landed cat.\n\n"
        "overall_soundscape: A low hum sits under the room tone.\n\n"
        "non_diegetic_music: N/A"
    )
    assert "FAST_CAMERA_MULTI_BEAT_COEXIST" not in {
        i.code for i in validate_segment(text, 4.0)
    }


def test_coexist_rewrites_segment_until_clean():
    """重写循环消费新检查：首稿共存 → 带原因重写 → 合格稿收工。"""
    from minimax_h3_prompt.segment_prompts import write_segment_v2

    good = _arm("executed_00014")
    llm = SequencedLLM([_arm("armA_H1_state_only"), good])
    plans = _v2_plans()  # 第 2 段时长 6s
    text = write_segment_v2(plans[1], plans, {"shot_table": "x"}, None, llm)

    assert llm.calls == 2
    assert text == good
    assert "FAST_CAMERA_MULTI_BEAT_COEXIST" in llm.requests[1], "重写请求没带上共存问题"


def test_gate_surfaces_coexist_issue_for_gen004_shot02(tmp_path, monkeypatch, capsys):
    """接线断言（调用点）：GEN004 shot-02.md 走向导闸门必须当面报红，不得静默交付。

    票面闸门缺口：validate_segment 对该文本曾返回零 issue，而它正是产出坏片的文本。
    物证已固化为 fixture（原件在未跟踪的 output/sessions/ 下，CI 不可达）。
    """
    from types import SimpleNamespace

    from minimax_h3_prompt import segment_prompts as _sp, session_store
    from minimax_h3_prompt.brief_parser import Brief
    from minimax_h3_prompt.segment_planner import SegmentPlan
    from minimax_h3_prompt.ui import wizard

    bad = _arm("gen004_shot02.md")
    brief = Brief(mode="base", variant="I2VA", duration=10.0, style="写实", plot="便利店橘猫")
    generation_dir = tmp_path / "GEN001"
    generation_dir.mkdir()
    session = session_store.SessionState(
        directory=generation_dir, brief=brief,
        stage_state={"shot_table": "[Shot 1] 深夜空店内……"},
        status=session_store.STATUS_COMPLETED,
    )
    plans = [SegmentPlan(index=0, start_s=0, end_s=4, shots_in_segment=(1,),
                         summary="深夜便利店全景", end_hook="玻璃门刚被顶开窄缝")]
    monkeypatch.setattr(_sp, "write_segment_v2", lambda *a, **k: bad)
    monkeypatch.setattr(wizard, "_prompt", lambda *a, **k: "")
    monkeypatch.setattr(wizard, "_confirm", lambda *a, **k: False)

    wizard._run_segmented_flow_v2(brief, session, plans, {}, SimpleNamespace())

    out = capsys.readouterr().out
    assert "FAST_CAMERA_MULTI_BEAT_COEXIST" in out, "闸门对新检查静默了"
    assert "可立即复制到 H3" not in out
