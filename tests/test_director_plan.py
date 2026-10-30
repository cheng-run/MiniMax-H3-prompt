"""导演台段规划（issue #102）：帧为真源、段类型与总帧落位。

口径按 ADR 0025（2026-10-29 用户裁定）：**每段声明帧都落在 17k+5 网格上**，总长取
「与 目标×24 最接近的合法落位」——30 秒 / 5 段里 720 ≡ 6 (mod 17) 不可达，最近是 719。

只断言外部可观察行为：纯函数的返回值、``plan.json`` 的内容与严格读回的报错、规划
出站清单的文本、向导接线后的落盘与出站。不断言私有函数的调用顺序。
"""
from __future__ import annotations

import json

import pytest

from minimax_h3_prompt.director.frames import (
    SEGMENT_TYPE_LABELS,
    SEGMENT_TYPES,
    align_frames,
    declared_frames_for_export,
    duration_sec_for_declared,
    export_frames_for_declared,
    give_images_for,
    is_guided,
    is_on_grid,
    land_exports,
    validate_segment_types,
)
from minimax_h3_prompt.director.planning import (
    DIRECTOR_PLANNER_ROLE,
    parse_director_plan,
    plan_director_segments,
    plan_path,
    read_plan_json,
    render_plan_listing,
    write_plan_json,
)


# ---------------------------------------------------------------------------
# 17k 网格与声明帧/导出帧换算（ADR 0024 / ADR 0025）
# ---------------------------------------------------------------------------

def test_align_frames_is_17k_plus_5_and_only_rounds_up():
    """插件对齐函数 ``17k+5`` 只向上取整（``frame_align.py:8``）。

    变异检验：把取整方向改成向下，``(108, 124)`` 与 ``(142, 158)`` 立刻变红。
    """
    cases = {0: 5, 1: 5, 5: 5, 6: 22, 21: 22, 22: 22, 23: 39,
             107: 107, 108: 124, 141: 141, 142: 158, 204: 209}
    for raw, expected in cases.items():
        assert align_frames(raw) == expected, f"align_frames({raw}) != {expected}"
    with pytest.raises(ValueError):
        align_frames(-1)


def test_guided_export_is_17_multiple_and_declared_is_12_less():
    """有引导段：导出帧恒为 17 的倍数，声明帧 = 导出帧 − 12（ADR 0024）。

    ADR 0025 的样例：声明 141 → 导出 153；声明 107（无引导）→ 导出 107。
    变异检验：把 ``KEEP_TAIL_FRAMES`` 改成 0，``141`` 那条断言立刻变红。
    """
    assert declared_frames_for_export(153, guided=True) == 141
    assert declared_frames_for_export(204, guided=True) == 192
    assert export_frames_for_declared(141, guided=True) == 153
    assert export_frames_for_declared(declared_frames_for_export(153, guided=True),
                                      guided=True) == 153
    for bad in (16, 17 * 4 + 1):
        with pytest.raises(ValueError):
            declared_frames_for_export(bad, guided=True)


def test_unguided_export_equals_declared_and_must_be_on_grid():
    """无引导段：声明帧 = 导出帧（ADR 0024）；但**声明帧必须在 17k+5 上**（ADR 0025）。

    ADR 0024 的 108 帧算例在 ADR 0025 里被推翻：后端与 UI 都按 ``durationSec`` 吸附，
    108 会被改写成 124。``is_on_grid`` 是这条新约束的唯一判据。
    """
    assert export_frames_for_declared(107, guided=False) == 107
    assert declared_frames_for_export(107, guided=False) == 107
    assert is_on_grid(107) and is_on_grid(141) and is_on_grid(5)
    for off_grid in (0, 4, 6, 108, 140):
        assert not is_on_grid(off_grid), f"{off_grid} 不该被判为网格值"


def test_duration_sec_is_self_consistent_with_declared_frames():
    """包内 ``durationSec`` 与声明帧必须自洽（ADR 0025）：107↔4.0s、141↔5.8s。

    自洽判据就是插件的换算 ``align(round(sec×24)) == 声明帧``（半值向上，与 JS 同号）。
    变异检验：把「优先整秒」去掉，107 会变成 4.4；把取最大改成取最小，141 会变成 5.2。
    """
    assert duration_sec_for_declared(107) == 4.0
    assert duration_sec_for_declared(141) == 5.8
    for declared in (5, 22, 56, 90, 107, 124, 141, 158, 209):
        seconds = duration_sec_for_declared(declared)
        assert align_frames((int(round(seconds * 10)) * 24 + 5) // 10) == declared, \
            f"声明 {declared} 帧 ↔ {seconds}s 不自洽"
    with pytest.raises(ValueError):
        duration_sec_for_declared(108)  # 非网格值反演不出自洽秒数


# ---------------------------------------------------------------------------
# 段类型与给图清单（设计 §3.1 的一张表）
# ---------------------------------------------------------------------------

def test_segment_types_and_labels_are_five_and_derive_guidance():
    """五种段类型、中文显示与引导开关只在 frames.py 一处声明。

    判定规则（spec「段类型与给图规则」）：起点是上一段终点 → 引导开；起点在别处 → 关。
    """
    assert SEGMENT_TYPES == ("opening", "continue", "camera_same", "camera_cut", "ending")
    assert SEGMENT_TYPE_LABELS == {
        "opening": "开场", "continue": "续接", "camera_same": "运镜·同场景",
        "camera_cut": "运镜·切镜", "ending": "收尾定格",
    }
    assert is_guided("opening") is False
    assert is_guided("camera_cut") is False
    assert is_guided("continue") is True
    assert is_guided("camera_same") is True
    assert is_guided("ending") is True
    with pytest.raises(ValueError):
        is_guided("opennig")


def test_invalid_or_missing_type_is_loud_and_first_segment_must_open():
    """非法类型当场报错，不许静默回落（票面硬约束）。

    段 1 必须是 ``opening``：段间引导需要「上一段缓存」且段号 > 0 才生效（CONTEXT
    术语「段间引导」四个条件），首段写成引导型就是一句**做不到的声明**。
    """
    with pytest.raises(ValueError):
        validate_segment_types(["opening", "cut"])
    with pytest.raises(ValueError):
        validate_segment_types([])
    with pytest.raises(ValueError):
        validate_segment_types(["continue", "ending"])
    validate_segment_types(["opening", "continue", "ending"])


def test_give_images_follow_type_and_mode():
    """给图清单从段类型派生；开场段再按模式分派（设计 §3.1 表）。

    续接段什么都不给（只靠段间引导）是最容易写错的一条：误判会让该段静默丢弃首帧。
    """
    assert give_images_for("continue", "r2v") == ()
    assert give_images_for("camera_same", "r2v") == ("end",)
    assert give_images_for("camera_cut", "r2v") == ("first", "end")
    assert give_images_for("ending", "t2v") == ("end",)
    assert give_images_for("opening", "t2v") == ()
    assert give_images_for("opening", "i2v") == ("first",)
    assert give_images_for("opening", "fl2v") == ("first",)
    assert give_images_for("opening", "r2v") == ("assets",)
    with pytest.raises(ValueError):
        give_images_for("opening", "v2v")   # 一期不做（#109）
    with pytest.raises(ValueError):
        give_images_for("opening", "t2vv")


# ---------------------------------------------------------------------------
# 总帧落位：最接近目标的可达总量（AC1/AC4，ADR 0025）
# ---------------------------------------------------------------------------

def test_landing_reproduces_the_adr_0025_example_as_719():
    """30 秒 / 5 段 / 每段提议 144 帧 → 107 + 153×4 = **719**（ADR 0025）。

    720 ≡ 6 (mod 17)，而 1 段无引导时可达总量恒 ≡ 5 (mod 17)：720 取不到，719 最近。
    出站说明必须写「目标 720、实际 719、差 1 帧」。
    """
    types = ["opening", "continue", "continue", "continue", "continue"]
    exports, note = land_exports(types, [144] * 5, 720)
    assert exports == [107, 153, 153, 153, 153]
    assert sum(exports) == 719
    assert note is not None
    assert "目标 720" in note and "719" in note and "差 1 帧" in note


def test_landing_picks_the_nearest_reachable_total_not_a_naive_rounding():
    """29.17 秒（700 帧）/ 5 段：可达总量 ≡ 5 (mod 17)，最近是 702（差 2），不是 700。

    变异检验：把「余额吸附到最近网格」改成「截断到网格下界」，会落到 685（差 15）。
    """
    types = ["opening", "continue", "continue", "continue", "continue"]
    exports, note = land_exports(types, [140] * 5, 700)
    assert sum(exports) == 702
    assert exports[0] == 90 and exports[1:] == [153] * 4
    assert note is not None and "差 2 帧" in note


def test_landing_shrinks_segments_when_the_remainder_does_not_fit():
    """开场只给 0.83 秒时余额装不下：把代价最小的有引导段各退一格，仍取最近可达总量。

    这是「提议与目标挤不下」的真实形态；落位后每段仍必须在网格上、合计 719。
    """
    types = ["opening", "continue", "continue", "continue", "continue"]
    exports, note = land_exports(types, [20, 175, 175, 175, 175], 720)
    assert sum(exports) == 719
    assert note is not None and "差 1 帧" in note
    assert all(export % 17 == 0 for export in exports[1:]), "有引导段导出必须是 17 的倍数"


def test_landing_without_unguided_segment_is_loud():
    """首段必须开场；即便强行全引导也凑不出任意总帧——一律报错，不产出畸形段表。"""
    with pytest.raises(ValueError):
        land_exports(["continue", "continue"], [360, 360], 720)


def test_landing_rejects_targets_too_small_for_the_segment_count():
    with pytest.raises(ValueError):
        land_exports(["opening", "continue", "continue"], [1, 1, 1], 30)


# ---------------------------------------------------------------------------
# LLM 提议 → 确定性层：段表（AC1/AC2/AC3）
# ---------------------------------------------------------------------------

# 30 秒五段、每段提议 6 秒；类型覆盖开场/续接/同场景运镜/切镜/收尾（2 段无引导）。
_THIRTY_SECONDS = """```json
{"segments": [
  {"index": 0, "start_s": 0, "end_s": 6, "type": "opening",
   "summary": "雨夜门口", "end_hook": "她停在便利店门口，右手还搭在伞柄上"},
  {"index": 1, "start_s": 6, "end_s": 12, "type": "continue",
   "summary": "推门进店", "end_hook": "门在身后合上，她站在货架前"},
  {"index": 2, "start_s": 12, "end_s": 18, "type": "camera_same",
   "summary": "环绕货架", "end_hook": "镜头绕到货架另一侧，她的侧脸停在灯下"},
  {"index": 3, "start_s": 18, "end_s": 24, "type": "camera_cut",
   "summary": "切到收银台", "end_hook": "收银台的暖灯亮着，店员抬头"},
  {"index": 4, "start_s": 24, "end_s": 30, "type": "ending",
   "summary": "定格", "end_hook": "她把手里的信封递到柜台上"}
]}
```"""

# ADR 0025 的样例形态：开场 + 四段有引导。
_ADR_SHAPE = """{"segments": [
  {"start_s": 0, "end_s": 6, "type": "opening", "summary": "a", "end_hook": "h1"},
  {"start_s": 6, "end_s": 12, "type": "continue", "summary": "b", "end_hook": "h2"},
  {"start_s": 12, "end_s": 18, "type": "continue", "summary": "c", "end_hook": "h3"},
  {"start_s": 18, "end_s": 24, "type": "continue", "summary": "d", "end_hook": "h4"},
  {"start_s": 24, "end_s": 30, "type": "continue", "summary": "e", "end_hook": "h5"}
]}"""


def test_thirty_seconds_plans_five_segments_landing_on_the_nearest_legal_total():
    """AC1/AC2：30 秒五段 → 段表；导出合计 = 计划总帧，且与 720 的差在可达范围内（≤8）。

    这一段有 2 个无引导段（开场 + 切镜），可达总量 ≡ 10 (mod 17) → 724；有引导段
    声明 = 导出 − 12，无引导段两者相等，**所有声明帧都在 17k+5 网格上**（ADR 0025）。
    """
    plan = parse_director_plan(_THIRTY_SECONDS, 30.0, mode="r2v")

    assert abs(plan.total_frames - 720) <= 8
    assert [segment.index for segment in plan.segments] == [1, 2, 3, 4, 5]
    assert sum(segment.export_frames for segment in plan.segments) == plan.total_frames
    assert [segment.type for segment in plan.segments] == [
        "opening", "continue", "camera_same", "camera_cut", "ending"]
    assert plan.segments[0].start_frame == 0
    assert plan.segments[-1].end_frame == plan.total_frames
    for previous, segment in zip(plan.segments, plan.segments[1:]):
        assert segment.start_frame == previous.end_frame, "段与段之间不许有缝"
    for segment in plan.segments:
        assert is_on_grid(segment.declared_frames), "声明帧必须在 17k+5 上"
        if segment.guided:
            assert segment.export_frames % 17 == 0
            assert segment.declared_frames == segment.export_frames - 12
        else:
            assert segment.export_frames == segment.declared_frames


def test_adr_0025_plan_lands_on_719_and_explains_the_gap():
    """ADR 0025 的样例逐字复现：107 + 4×153 = 719，说明写「目标 720、差 1 帧」。"""
    plan = parse_director_plan(_ADR_SHAPE, 30.0, mode="t2v")
    assert plan.total_frames == 719
    assert [segment.export_frames for segment in plan.segments] == [107, 153, 153, 153, 153]
    assert [segment.declared_frames for segment in plan.segments] == [107, 141, 141, 141, 141]
    assert plan.notes and "目标 720" in plan.notes[0] and "差 1 帧" in plan.notes[0]


def test_segments_carry_type_derived_giving_list():
    """AC3：每段带段类型与给图清单，且清单**从类型派生**（不信 LLM 直接给）。"""
    plan = parse_director_plan(_THIRTY_SECONDS, 30.0, mode="r2v")
    by_type = {segment.type: segment for segment in plan.segments}
    assert by_type["continue"].give_images == ()
    assert by_type["camera_same"].give_images == ("end",)
    assert by_type["camera_cut"].give_images == ("first", "end")
    assert by_type["ending"].give_images == ("end",)
    # opening + r2v：参考素材必填、首帧可选
    assert by_type["opening"].give_images == ("assets",)


@pytest.mark.parametrize("reply, message", [
    # 类型缺失 / 拼错 / 首段不是开场：一律报错，不许静默回落
    ('{"segments": [{"start_s": 0, "end_s": 5, "summary": "a", "end_hook": "h"}]}',
     "段类型非法"),
    ('{"segments": [{"start_s": 0, "end_s": 5, "type": "intro", "end_hook": "h"}]}',
     "段类型非法"),
    ('{"segments": [{"start_s": 0, "end_s": 5, "type": "continue", "end_hook": "h"}]}',
     "必须是 opening"),
    # 边界不从 0 起 / 没覆盖到总时长 / 空 end_hook
    ('{"segments": [{"start_s": 2, "end_s": 30, "type": "opening", "end_hook": "h"}]}',
     "0"),
    ('{"segments": [{"start_s": 0, "end_s": 10, "type": "opening", "end_hook": "h"}]}',
     "30"),
    ('{"segments": [{"start_s": 0, "end_s": 10, "type": "opening", "end_hook": " "},'
     ' {"start_s": 10, "end_s": 30, "type": "continue", "end_hook": "h"}]}',
     "end_hook"),
    # 不是 JSON
    ("我建议这样分段：……", "JSON"),
])
def test_contract_violations_are_loud(reply, message):
    """规划回复的任何契约违规都当场报错——这一条路线没有机械回退可退。"""
    with pytest.raises(ValueError) as excinfo:
        parse_director_plan(reply, 30.0, mode="r2v")
    assert message in str(excinfo.value)


def test_planning_refuses_modes_this_ticket_does_not_cover():
    """一期只做 t2v/i2v/fl2v/r2v 的叙事规划；v2v/rv2v/mixed 不在本票范围。"""
    for mode in ("v2v", "rv2v", "mixed", "T2V"):
        with pytest.raises(ValueError):
            parse_director_plan(_THIRTY_SECONDS, 30.0, mode=mode)


# ---------------------------------------------------------------------------
# plan.json：落点（GENxxx/director/）、形态（设计 §3.2）与严格读回
# ---------------------------------------------------------------------------

def test_plan_json_lives_under_the_director_directory(tmp_path):
    """``GENxxx/director/plan.json``——**不写** ``segments/plan.json``（设计 §3.2）。

    共享读取口 ``tools/ledger.read_plan_segment_count`` 只认 JSON 数组；同路径放 dict
    会让它在导演台会话上静默返回 None。新形态住路线自己的目录，旧读取口零改动。
    """
    session_dir = tmp_path / "GEN001"
    assert plan_path(session_dir) == session_dir / "director" / "plan.json"


def test_plan_json_has_the_design_shape_and_round_trips(tmp_path):
    plan = parse_director_plan(_ADR_SHAPE, 30.0, mode="r2v")
    path = plan_path(tmp_path / "GEN001")
    write_plan_json(path, plan)

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert set(raw) == {"route", "mode", "fps", "total_frames", "segments"}
    assert raw["route"] == "director" and raw["fps"] == 24 and raw["mode"] == "r2v"
    assert raw["total_frames"] == 719
    assert set(raw["segments"][0]) == {
        "index", "type", "guided", "start_frame", "end_frame",
        "declared_frames", "export_frames", "start_s", "end_s",
        "summary", "end_hook", "give_images"}

    assert read_plan_json(path).to_dict() == plan.to_dict()


@pytest.mark.parametrize("mutate, message", [
    (lambda data: data["segments"][1].update(type="opennig"), "未知段类型"),
    (lambda data: data.update(fps=25), "fps"),
    (lambda data: data.update(mode="mixed"), "模式"),
    (lambda data: data.update(route="ref"), "route"),
    (lambda data: data["segments"][1].update(export_frames=139), "17 的倍数"),
    (lambda data: data["segments"][1].update(export_frames=136), "声明帧"),
    (lambda data: data["segments"][0].update(declared_frames=141), "声明帧"),
    (lambda data: data["segments"][0].update(
        export_frames=108, declared_frames=108), "网格"),
    (lambda data: data.update(total_frames=721), "导出合计"),
    (lambda data: data["segments"][0].update(guided=True), "guided"),
    (lambda data: data["segments"][0].update(give_images=["end"]), "给图"),
    (lambda data: data["segments"][0].update(index=2), "index"),
    (lambda data: data["segments"][4].update(end_frame=718), "总帧"),
])
def test_plan_json_readback_is_strict(tmp_path, mutate, message):
    """任何一处与帧契约不符都抛 ``ValueError``：续跑照错表读下去就是接错线。"""
    plan = parse_director_plan(_ADR_SHAPE, 30.0, mode="r2v")
    path = tmp_path / "plan.json"
    write_plan_json(path, plan)
    data = json.loads(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError) as excinfo:
        read_plan_json(path)
    assert message in str(excinfo.value)


# ---------------------------------------------------------------------------
# 出站清单：逐段展示段号 / 类型 / 声明帧 / 导出帧 / 给图清单（AC3）
# ---------------------------------------------------------------------------

def test_listing_shows_every_segment_with_type_frames_and_images():
    plan = parse_director_plan(_THIRTY_SECONDS, 30.0, mode="r2v")
    text = render_plan_listing(plan)

    for segment in plan.segments:
        assert f"段{segment.index}" in text
        assert segment.label in text
        assert str(segment.declared_frames) in text
        assert str(segment.export_frames) in text
    assert "给图：无（只靠段间引导）" in text
    assert "给图：首帧 + 尾帧（终点画面）" in text
    assert "720" in text, "目标总帧必须出现在清单里（与可达总量的差要一起看）"


def test_listing_prints_the_target_vs_actual_gap():
    """AC4：落位与目标不等时，清单里写明「目标 / 实际 / 差几帧」（ADR 0025）。"""
    plan = parse_director_plan(_ADR_SHAPE, 30.0, mode="t2v")
    text = render_plan_listing(plan)
    assert "目标 720" in text and "719" in text and "差 1 帧" in text


# ---------------------------------------------------------------------------
# 规划调用：直调看门狗 + 导演台自己的角色名（成本表要分得清钱花在哪）
# ---------------------------------------------------------------------------

class _FakeLLM:
    def __init__(self, reply):
        self.reply = reply
        self.last_request = ""

    def invoke(self, request, config=None):
        self.last_request = request
        return self.reply


def test_plan_director_segments_calls_llm_with_the_director_instruction():
    llm = _FakeLLM(_ADR_SHAPE)
    plan = plan_director_segments("分镜表：[Shot 1] 雨夜便利店……", 30.0, llm, mode="r2v")

    assert plan.total_frames == 719
    request = llm.last_request
    assert "导演台" in request, "规划指令必须是导演台那一份，不是 base/ref 的"
    assert "17 的倍数" in request or "17k" in request, "指令要让规划师知道帧网格"
    assert "720" in request, "目标总帧要显式给出，规划师不必自己算"
    assert "雨夜便利店" in request


def test_plan_director_segments_uses_its_own_role_for_cost_accounting():
    """角色名单独取：与 ``role_segment_planner`` 混在一处就分不清钱花在哪条路线上。"""
    from minimax_h3_prompt.observability import reporter

    got: list[dict] = []
    reporter.subscribe(got.append)
    try:
        plan_director_segments("分镜表：[Shot 1] 雨夜……", 30.0, _FakeLLM(_ADR_SHAPE),
                               mode="r2v")
    finally:
        reporter.unsubscribe(got.append)

    starts = [event for event in got if event.get("type") == "agent_start"]
    assert starts and all(event["role"] == DIRECTOR_PLANNER_ROLE for event in starts)
    assert DIRECTOR_PLANNER_ROLE != "role_segment_planner"


# ---------------------------------------------------------------------------
# 向导接线（AC3 要产线可达）：规划 → 写 director/plan.json → 出站段表
# ---------------------------------------------------------------------------

def _director_session(tmp_path, shot_table: str = "分镜表：[Shot 1] 雨夜便利店……"):
    from minimax_h3_prompt.brief_parser import Brief
    from minimax_h3_prompt.session_store import SessionState

    return SessionState(
        directory=tmp_path / "GEN001",
        brief=Brief(mode="director", variant="T2VA", director_mode="r2v",
                    duration=30.0, plot="雨夜旧信",
                    # #104 起进导演台流程会先问打包参数；本文件测的是**段表**，
                    # 把三个答案预先填好即可跳过那三问（问一次的逻辑在
                    # ``tests/test_director_pack.py`` 里覆盖）。
                    director_node_id="42", director_continuity_mode="guide"),
        stage_state={"shot_table": shot_table} if shot_table is not None else {},
        status="awaiting_frames",
    )


def test_wizard_director_flow_writes_plan_and_prints_listing(tmp_path, monkeypatch, capsys):
    """真实向导路径上 AC3 可验：段表落 ``director/plan.json``，出站逐段展示。

    写段（#103）在本用例里打桩：本票的接缝是**段表**，逐段方言与落盘由
    ``tests/test_director_prompts.py`` 的向导用例覆盖（那里的假模型会真的回段正文）。
    """
    from minimax_h3_prompt import model_factory
    from minimax_h3_prompt.director import prompts
    from minimax_h3_prompt.ui import wizard

    monkeypatch.setattr(model_factory, "build_chat_model", lambda: _FakeLLM(_ADR_SHAPE))
    monkeypatch.setattr(prompts, "write_director_segments",
                        lambda plan, **kwargs: [])
    # 打包（#104）在本用例里打桩：本票的接缝是**段表**，包的布局与八条约束由
    # ``tests/test_director_pack.py`` 覆盖（r2v 打包还需要素材台账，与本票无关）。
    from types import SimpleNamespace

    from minimax_h3_prompt.director import pack as pack_module

    monkeypatch.setattr(
        pack_module, "prepare_director_pack",
        lambda *args, **kwargs: (SimpleNamespace(), "导演包已构建（本用例打桩）"))
    # #105 起打包之后接逐段陪跑（要用户回导演台跑再回车）：本用例只测段表，输入 q 停下。
    monkeypatch.setattr(wizard, "_prompt", lambda text: "q")
    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)

    assert wizard._run_director_flow(_director_session(tmp_path)) == 0

    plan_file = tmp_path / "GEN001" / "director" / "plan.json"
    assert plan_file.is_file(), "段表必须落 GENxxx/director/plan.json"
    data = json.loads(plan_file.read_text(encoding="utf-8"))
    assert data["total_frames"] == 719

    out = capsys.readouterr().out
    assert "段1" in out and "开场" in out and "续接" in out
    assert "719" in out and "目标 720" in out and "差 1 帧" in out
    assert "导演包" in out


def test_wizard_director_flow_without_shot_table_stops_loudly(tmp_path, capsys):
    """没有分镜表就没有可规划的叙事单元：报错退出，不写半张表、不进阶段 2。"""
    from minimax_h3_prompt.ui import wizard

    assert wizard._run_director_flow(_director_session(tmp_path, shot_table=None)) == 1
    assert "分镜表" in capsys.readouterr().out
    assert not (tmp_path / "GEN001" / "director" / "plan.json").exists()


# ---------------------------------------------------------------------------
# r2v 复用多参素材循环（2026-10-29 用户裁定；复审 S2）
# ---------------------------------------------------------------------------

_ASSET_STATE = {
    "script": "林小满推开门，把旧信放在便利店柜台上。",
    "character_image_prompts": [
        {"name": "林小满", "positive_prompt": "人物·林小满专属生图提示词"},
    ],
    "scene_image_prompts": [
        {"name": "便利店", "positive_prompt": "场景·便利店专属生图提示词"},
    ],
}


class _FlowLLM:
    """规划请求回段表 JSON；回灌请求原词回吐（提示词不动，省一次解析失败）。

    回灌请求靠 ``asset_loop.SETTLED_FACTS_HEAD`` 认出来——那是块标题的唯一真源
    （ADR 0008 裁定二），本文件不另抄一份字面量。
    """

    def __init__(self, planning_reply: str) -> None:
        self.planning_reply = planning_reply
        self.requests: list[str] = []

    def invoke(self, request, config=None):
        from minimax_h3_prompt.asset_loop import CURRENT_PROMPT_HEAD, SETTLED_FACTS_HEAD

        text = str(request)
        self.requests.append(text)
        for callback in (config or {}).get("callbacks") or []:
            callback.on_llm_new_token("…")
        if SETTLED_FACTS_HEAD in text:
            # 回灌：原词回吐，保证 locked_features / 正文逐字不变。
            return text.split(f"{CURRENT_PROMPT_HEAD}\n", 1)[-1].split(
                f"\n\n{SETTLED_FACTS_HEAD}", 1)[0].strip()
        return self.planning_reply


def _stub_director_heavy_steps(monkeypatch, llm) -> None:
    """打桩写段、打包与视觉读图：本组用例的接缝是**流程顺序**，不是产物内容。"""
    from types import SimpleNamespace

    from minimax_h3_prompt import model_factory
    from minimax_h3_prompt.director import pack as pack_module
    from minimax_h3_prompt.director import prompts as prompts_module
    from minimax_h3_prompt.tools import reference_auditor

    monkeypatch.setattr(model_factory, "build_chat_model", lambda: llm)
    monkeypatch.setattr(reference_auditor, "describe_image",
                        lambda path, instruction=None: '{"claims": []}')
    monkeypatch.setattr(prompts_module, "write_director_segments", lambda plan, **kw: [])
    monkeypatch.setattr(
        pack_module, "prepare_director_pack",
        lambda *args, **kwargs: (SimpleNamespace(), "导演包已构建（本用例打桩）"))


def _asset_image(tmp_path, name: str):
    path = tmp_path / "returned" / f"{name}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(name.encode("utf-8"))
    return path


def test_wizard_director_r2v_runs_the_asset_loop_before_planning(
        tmp_path, monkeypatch, capsys):
    """r2v 先走素材循环，循环结束才做段规划并落 ``director/plan.json``（S2）。

    不接这一处：`pack._picture_sources` 只认素材台账里已登记的正本 → r2v 端到端
    不可用（打包必然报「台账是空的」）。t2v 的对照在下一个用例。
    """
    from minimax_h3_prompt.ui import wizard

    session = _director_session(tmp_path)
    session.stage_state.update({key: value for key, value in _ASSET_STATE.items()})
    image = _asset_image(tmp_path, "林小满")
    plan_file = session.directory / "director" / "plan.json"

    llm = _FlowLLM(_ADR_SHAPE)
    _stub_director_heavy_steps(monkeypatch, llm)
    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)

    seen = {"plan_exists_inside_loop": None}

    def fake_prompt(text: str) -> str:
        if "[审查·" in text:
            # 素材循环还没跑完：段表必须**还不存在**（顺序反了就是「先规划后收素材」）。
            seen["plan_exists_inside_loop"] = plan_file.exists()
            return "1"
        if "素材图路径" in text:
            return str(image)
        if "下一个出谁" in text:
            return ""            # 两张：第一张走默认队首
        if "有要重跑的段吗" in text:
            return "q"           # 段表已出，停在陪跑开始前
        raise AssertionError(f"意外的问题：{text}")

    monkeypatch.setattr(wizard, "_prompt", fake_prompt)
    assert wizard._run_director_flow(session) == 0

    out = capsys.readouterr().out
    assert seen["plan_exists_inside_loop"] is False, "素材循环没跑完就规划了"
    assert "[素材循环]" in out and "林小满" in out
    assert plan_file.is_file(), "素材循环跑完后必须接着做段规划并落盘"


def test_wizard_director_t2v_skips_the_asset_loop(tmp_path, monkeypatch, capsys):
    """t2v 不产资产图：一步都不进素材循环（与 #104 的行为逐字相同）。"""
    from minimax_h3_prompt.ui import wizard

    session = _director_session(tmp_path)
    session.brief.director_mode = "t2v"
    _stub_director_heavy_steps(monkeypatch, _FlowLLM(_ADR_SHAPE))
    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)
    monkeypatch.setattr(wizard, "_prompt", lambda text: "q")

    def _boom(*args, **kwargs):
        raise AssertionError("t2v 走进了素材循环")

    # 判据接在共用入口的下游：入口被判错时才咬得住（只打桩入口本身挡不住接线错误）。
    monkeypatch.setattr(wizard, "_asset_loop", _boom)

    assert wizard._run_director_flow(session) == 0
    out = capsys.readouterr().out
    assert "[素材循环]" not in out
    assert (session.directory / "director" / "plan.json").is_file()


def test_wizard_director_r2v_asset_loop_pause_resumes_at_the_same_asset(
        tmp_path, monkeypatch, capsys):
    """循环中途「先到这里」：停在同一阶段，重跑回到素材循环，不重付已出过的词（S2）。

    续接的判据是**当前待办已落盘**（``asset_loop`` 的 ``current``）：重跑直接回到
    第二张的审查，不再问「下一个出谁」；已落账的那张也不重新出词（出词是纯渲染，
    但回灌会花一次 LLM——本用例按模型调用次数钉住「没有重复花钱」）。
    """
    from minimax_h3_prompt.session_store import load_session
    from minimax_h3_prompt.ui import wizard

    session = _director_session(tmp_path)
    session.stage_state.update({key: value for key, value in _ASSET_STATE.items()})
    image = _asset_image(tmp_path, "林小满")
    plan_file = session.directory / "director" / "plan.json"

    llm = _FlowLLM(_ADR_SHAPE)
    _stub_director_heavy_steps(monkeypatch, llm)
    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)

    def first_run_prompt(text: str) -> str:
        if "[审查·" in text:
            return "q" if "便利店" in text else "1"   # 第二张停在审查＝先到这里
        if "素材图路径" in text:
            return str(image)
        if "下一个出谁" in text:
            return ""
        raise AssertionError(f"意外的问题：{text}")

    monkeypatch.setattr(wizard, "_prompt", first_run_prompt)
    assert wizard._run_director_flow(session) == 0
    assert not plan_file.exists(), "循环没清空就不许规划"
    assert "已保留进度" in capsys.readouterr().out

    resumed = load_session(session.directory)
    assert resumed is not None
    calls_before = len(llm.requests)
    asked: list[str] = []

    def second_run_prompt(text: str) -> str:
        asked.append(text)
        if "[审查·" in text:
            assert "便利店" in text, "续跑没有回到同一张待办"
            return "1"
        if "素材图路径" in text:
            return str(image)
        if "下一个出谁" in text:
            raise AssertionError("续跑不该再问「下一个出谁」")
        if "有要重跑的段吗" in text:
            return "q"
        raise AssertionError(f"意外的问题：{text}")

    monkeypatch.setattr(wizard, "_prompt", second_run_prompt)
    assert wizard._run_director_flow(resumed) == 0

    out = capsys.readouterr().out
    assert "[素材循环] 当前待办：便利店" in out
    assert plan_file.is_file(), "续接跑完循环后必须接着规划"
    # 第二段里唯一的模型调用是段规划本身：回灌（如果重做）会多一次。
    assert len(llm.requests) == calls_before + 1


def test_planner_instruction_example_numbers_segments_from_one():
    """H10：规划指令里的示例 ``index`` 必须是 1 起——与段号、出站「段1」同口径。

    示例写 0 起时模型会照抄 0 号段，而 ``read_plan_json`` 的读回判据是
    ``index == position``（1 起）：一次往返就报错，且报的是「第 1 段 index 是 0」。
    """
    from minimax_h3_prompt.director.planning import load_instruction

    text = load_instruction()
    assert '"index": 0' not in text
    assert '"index": 1' in text and '"index": 2' in text
