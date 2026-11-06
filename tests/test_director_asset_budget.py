"""导演台段内素材预算（#186）：规划期就定下每段能用哪几张图。

缝（全部是票面点名的既有公开入口）：

1. ``plan_director_segments``——规划请求里能看到素材清单；规划师给的 ``assets`` 经
   闭包＋锚降级变成独立预算；缺 ``assets`` 是**空预算**，不是「保留全部」。
2. ``asset-budget.json`` 的落盘形态、严格读回与「文件不在＝旧会话空表」。
3. ``plan.json`` 形态不因预算而变（不含任何预算字段）。
4. 向导在**写段之前**落盘预算，出站清单点名「未挑图」。

断言只碰外部可观察行为：请求字符串里的关键行、落盘 JSON、返回值、异常文案。
"""
from __future__ import annotations

import json

import pytest

from minimax_h3_prompt.director.planning import (
    AssetBudget,
    asset_budget_path,
    load_asset_budget,
    plan_director_segments,
    plan_path,
    read_plan_json,
    render_plan_listing,
    write_asset_budget,
    write_plan_json,
)

# 真实事故的形态（#180）：第 1 段的种子只点名父亲；「必须与 Subject 2 一致」把前院
# 拉进来，前院的定义再落到 Picture 22。Picture 22 正文没拍，只作一致性锚。
_DEFINITIONS = (
    "<Subject 1> is the father. His appearance comes from <Picture 1>. "
    "His coat must agree with <Subject 2>.\n"
    "<Subject 2> is the front yard and the family car, whose view comes from "
    "<Picture 22>."
)

_REGISTRY = (
    (1, "父亲_综合角色板"),
    (22, "BG05_住宅前院与家庭车辆车道"),
)

# 30 秒五段（与 test_director_plan 的 _ADR_SHAPE 同形，只多两处 assets）。
_REPLY = """{"segments": [
  {"start_s": 0, "end_s": 6, "type": "opening", "summary": "a", "end_hook": "h1",
   "assets": [1]},
  {"start_s": 6, "end_s": 12, "type": "continue", "summary": "b", "end_hook": "h2",
   "assets": []},
  {"start_s": 12, "end_s": 18, "type": "continue", "summary": "c", "end_hook": "h3"},
  {"start_s": 18, "end_s": 24, "type": "continue", "summary": "d", "end_hook": "h4"},
  {"start_s": 24, "end_s": 30, "type": "continue", "summary": "e", "end_hook": "h5"}
]}"""

_NO_ASSETS = """{"segments": [
  {"start_s": 0, "end_s": 6, "type": "opening", "summary": "a", "end_hook": "h1"},
  {"start_s": 6, "end_s": 12, "type": "continue", "summary": "b", "end_hook": "h2"},
  {"start_s": 12, "end_s": 18, "type": "continue", "summary": "c", "end_hook": "h3"},
  {"start_s": 18, "end_s": 24, "type": "continue", "summary": "d", "end_hook": "h4"},
  {"start_s": 24, "end_s": 30, "type": "continue", "summary": "e", "end_hook": "h5"}
]}"""


class _RecordLLM:
    """记录每次出站请求；回固定 JSON（记 token 让看门狗看到首字节）。"""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.requests: list[str] = []

    def invoke(self, request, config=None):
        self.requests.append(str(request))
        for callback in (config or {}).get("callbacks") or []:
            callback.on_llm_new_token("…")
        return self.reply


def _plan(reply: str = _REPLY, *, mode: str = "r2v"):
    llm = _RecordLLM(reply)
    result = plan_director_segments(
        "分镜表：[Shot 1] 父亲走进院子。", 30.0, llm, mode=mode,
        registry=_REGISTRY, subject_definitions=_DEFINITIONS)
    return result, llm


# ---------------------------------------------------------------------------
# 规划请求：素材清单（编号 + 语义名）
# ---------------------------------------------------------------------------

def test_planner_request_lists_every_registered_picture_by_number():
    """规划师只能按真实登记挑图：请求里必须给出 `<Picture N> = 语义名` 整份清单。"""
    _result, llm = _plan()

    request = llm.requests[0]
    assert "素材清单" in request
    for number, name in _REGISTRY:
        assert f"<Picture {number}> = {name}" in request, f"清单里缺 <Picture {number}>"


def test_only_r2v_requests_carry_the_material_list():
    """t2v 没有参考素材：别的模式不该凭空多出一份挑图清单。

    指令正文里一直写着「素材清单」这一节（五种模式共用一份提示词），所以判据是
    **台账行**在不在，而不是小节标题在不在。
    """
    _result, llm = _plan(mode="t2v")
    request = llm.requests[0]
    for number, name in _REGISTRY:
        assert f"<Picture {number}> = {name}" not in request


# ---------------------------------------------------------------------------
# 闭包 + 锚降级 → 预算
# ---------------------------------------------------------------------------

def test_planner_assets_become_pictures_and_anchors():
    """种子 → 闭包 → 锚降级：出镜进 pictures（占槽），锚进 anchors（不占槽、带 via）。"""
    result, _llm = _plan()
    budget = result.budget

    first = budget.segments[1]
    assert [(item.number, item.name, item.role) for item in first.pictures] == [
        (1, "父亲_综合角色板", "depicted")]
    # Picture 22 不是规划师挑的，是 Subject 2 的定义沿「must agree with」勾进来的。
    assert [(item.number, item.name, item.via) for item in first.anchors] == [
        (22, "BG05_住宅前院与家庭车辆车道", "Subject 1")]
    # 槽位只看 pictures：锚多一张也不改变计数。
    assert first.slot_count == 1


def test_missing_assets_is_an_empty_budget_not_the_whole_registry():
    """缺键／空数组＝空预算：绝不能走闭包「种子为空→保留全部」那条回退。"""
    result, _llm = _plan(_NO_ASSETS)

    for index in range(1, 6):
        entry = result.budget.segments[index]
        assert entry.pictures == (), f"第 {index} 段没挑图却是非空预算"
        assert entry.anchors == ()
    assert result.budget.segments[2].pictures == (), "空数组同样是空预算"


def test_unknown_picture_number_is_a_contract_violation():
    """清单是唯一来源：规划师编一个没登记的号，不能静默塞进预算。"""
    reply = _REPLY.replace('"assets": [1]', '"assets": [99]')
    with pytest.raises(ValueError) as excinfo:
        _plan(reply)
    assert "第 1 段" in str(excinfo.value) and "99" in str(excinfo.value)


def test_assets_must_be_an_integer_array():
    reply = _REPLY.replace('"assets": [1]', '"assets": "1"')
    with pytest.raises(ValueError):
        _plan(reply)


def test_non_r2v_returns_an_empty_budget_and_writes_no_file(tmp_path):
    result, _llm = _plan(mode="t2v")
    assert result.budget.segments == {}
    assert write_asset_budget(tmp_path / "GEN001", result.budget) is None
    assert not asset_budget_path(tmp_path / "GEN001").exists()


# ---------------------------------------------------------------------------
# 落盘 / 读回
# ---------------------------------------------------------------------------

def test_budget_json_round_trips_and_plan_json_stays_budget_free(tmp_path):
    result, _llm = _plan()
    session_dir = tmp_path / "GEN001"

    write_plan_json(plan_path(session_dir), result.plan)
    budget_file = write_asset_budget(session_dir, result.budget)

    assert budget_file == session_dir / "director" / "asset-budget.json"
    raw = json.loads(budget_file.read_text(encoding="utf-8"))
    assert raw["1"] == {
        "pictures": [{"number": 1, "name": "父亲_综合角色板", "role": "depicted"}],
        "anchors": [{"number": 22, "name": "BG05_住宅前院与家庭车辆车道",
                     "via": "Subject 1"}],
    }
    # 没挑图的段也要落一条空预算，段号才能对齐（不是缺键）。
    assert raw["3"] == {"pictures": [], "anchors": []}

    # plan.json 里不许出现任何预算字段：预算只存一份（#180 决定 6 / 用户故事 24）。
    plan_raw = json.loads(plan_path(session_dir).read_text(encoding="utf-8"))
    assert set(plan_raw) == {"route", "mode", "fps", "total_frames", "segments"}
    for segment in plan_raw["segments"]:
        assert not {"assets", "budget", "pictures", "anchors"} & set(segment)

    # 续跑：段表与预算各自从盘上读回，不重跑规划。
    assert read_plan_json(plan_path(session_dir)).to_dict() == result.plan.to_dict()
    assert load_asset_budget(session_dir) == result.budget


def test_missing_budget_file_is_an_empty_budget_for_old_sessions(tmp_path):
    budget = load_asset_budget(tmp_path / "GEN_OLD")
    assert isinstance(budget, AssetBudget)
    assert budget.segments == {}


@pytest.mark.parametrize("payload, message", [
    ("not json", "asset-budget.json"),
    ("[]", "顶层"),
    ('{"1": []}', "对象"),
    ('{"1": {"pictures": [{"name": "x"}]}}', "number"),
    ('{"1": {"pictures": [{"number": "1", "name": "x"}]}}', "number"),
    ('{"1": {"pictures": [], "anchors": [{"name": "x"}]}}', "number"),
    ('{"x": {"pictures": [], "anchors": []}}', "段号"),
])
def test_budget_readback_is_strict(tmp_path, payload, message):
    """文件损坏或形态不对要响：续跑照坏预算挂图就是接错线（#189 会消费它）。"""
    session_dir = tmp_path / "GEN001"
    path = asset_budget_path(session_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(ValueError) as excinfo:
        load_asset_budget(session_dir)
    assert message in str(excinfo.value)


def test_listing_marks_segments_without_picked_pictures():
    """清单要让用户看见「这一段规划师没挑图」，而不是静默给一个空预算。"""
    result, _llm = _plan(_NO_ASSETS)
    text = render_plan_listing(result.plan, budget=result.budget)
    assert text.count("未挑图") == 5


def test_listing_shows_the_slot_count_and_anchors():
    result, _llm = _plan()
    text = render_plan_listing(result.plan, budget=result.budget)
    assert "素材预算：1 张" in text
    assert "锚 1" in text, "锚要能看到，才能解释「为什么只有 1 张却提到了前院」"
