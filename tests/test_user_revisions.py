"""用户修订真源与修订台账（#23 / T1；按编号撤销见 #24 / T2）。

用户实测（ADR 0005 物证）：同一主题连提两次意见，第二次重出的产物里第一条的改动
**整体消失**。根因是第 N 轮用循环外的原始 brief 重建，前 N-1 条被丢弃；而意见唯一
的落点是 ``brief.plot`` 这个「主题」字段——它还喂着地点守卫的子串匹配与生图常识判官
的判据，塞散文意见等于让意见随机改写两条闸门的判据。

本文件测真源与台账本身；跨进程的验收（重出请求里到底带没带上一条）在
``tests/test_wizard_revision_loop.py``。
"""
from __future__ import annotations

import json

import pytest

from minimax_h3_prompt.user_revisions import (
    FRAME_ROUNDS_FILENAME,
    LAYER_FRAME,
    active_revisions,
    begin_round,
    log_frame_round,
    next_round,
    record_user_revision,
    remove_user_revision,
    render_baseline_block,
    render_revision_block,
    render_revision_list,
    render_revocation_block,
    revocation_note,
)


def _rows(directory) -> list[dict]:
    path = directory / FRAME_ROUNDS_FILENAME
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_revisions_accumulate_across_rounds():
    """连提两条 → 清单两条都在，各带轮次与层次。"""
    state: dict = {}
    record_user_revision(state, layer=LAYER_FRAME, text="天空改成黄昏")
    record_user_revision(state, layer=LAYER_FRAME, text="院中加红枫")

    active = active_revisions(state)
    assert [item["text"] for item in active] == ["天空改成黄昏", "院中加红枫"]
    assert [item["round"] for item in active] == [1, 2]
    assert all(item["layer"] == LAYER_FRAME for item in active)
    assert state["frame_round"] == 2


def test_round_number_never_rewinds_after_a_revision_is_dropped():
    """按编号回收任意一条（T2）后轮次号不得回退——否则台账里会出现两行 round 相同。

    轮次号若按「清单长度 + 1」推，撤掉一条就会重发一个用过的轮次，台账事后没法按轮对齐。
    """
    state: dict = {}
    record_user_revision(state, layer=LAYER_FRAME, text="一")
    record_user_revision(state, layer=LAYER_FRAME, text="二")
    state["user_revisions"] = [state["user_revisions"][1]]  # 用户撤掉了第 1 条

    entry = record_user_revision(state, layer=LAYER_FRAME, text="三")

    assert entry["round"] == 3


def test_render_block_carries_every_revision_in_order():
    """注入块必须逐条带编号与层次，并写明累积语义（不是只落实最后一条）。"""
    state: dict = {}
    record_user_revision(state, layer=LAYER_FRAME, text="天空改成黄昏")
    record_user_revision(state, layer=LAYER_FRAME, text="院中加红枫")

    block = render_revision_block(active_revisions(state))

    assert "天空改成黄昏" in block and "院中加红枫" in block
    assert block.index("天空改成黄昏") < block.index("院中加红枫")
    assert "只改画面" in block, "层次没进请求——模型无从知道这条是画面级要求"
    assert "每一条都必须落实" in block, "没写明累积语义，模型会当成『只照最后一条改』"


def test_render_block_is_empty_without_revisions():
    """空清单渲染成空串：节点用 _ctx，空值自动跳过——首次自动生成的消息逐字不变。"""
    assert render_revision_block(None) == ""
    assert render_revision_block([]) == ""


def test_blank_revision_is_rejected():
    """空意见是调用方的 bug：静默写进清单会让台账里多一条没有内容的轮次。"""
    with pytest.raises(ValueError):
        record_user_revision({}, layer=LAYER_FRAME, text="   ")


def test_corrupt_entry_is_loud_instead_of_silently_dropped():
    """清单里混进坏条目时不许静默跳过——静默丢掉一条修订正是本票要消灭的故障。"""
    with pytest.raises(ValueError):
        render_revision_block([{"round": 1, "layer": LAYER_FRAME, "text": "  "}])
    with pytest.raises(ValueError):
        active_revisions({"user_revisions": ["这不是条目"]})


def _baseline_bundle() -> dict:
    """上一版完整产物（节点落盘的形态：帧是列表、每项带 model_family）。"""
    return {
        "scene_anchor": "秋日庭院：青石地、院角水缸、墙侧红枫",
        "first": [
            {"frame": "first", "model_family": "zimage",
             "positive_prompt": "首帧-Z：橘猫蹲在青石上，门环有包浆"},
            {"frame": "first", "model_family": "flux2",
             "positive_prompt": "首帧-F：橘猫蹲在青石上，门环有包浆（Flux.2）"},
        ],
        "last": [
            {"frame": "last", "model_family": "zimage", "positive_prompt": "尾帧-Z：橘猫抬头看红枫"},
        ],
        "continuity_constraints": ["同一只橘猫、同一庭院", "门环包浆与茶托位置不变"],
    }


def test_baseline_block_carries_the_whole_previous_product():
    """基线取**完整**产物：首/尾帧正文 + 场景锚 + 连续性约束，不是只喂正文文本。

    场景锚与 continuity_constraints 正是首尾帧连续性那几条 mismatch 闸门校验的字段，
    只喂正文会让基线在被校验的字段上失锚（issue #25）。
    """
    block = render_baseline_block(_baseline_bundle())

    assert "门环有包浆" in block, "基线丢了上一版的画面细节——『未提过的细节要保留』落空"
    assert "橘猫抬头看红枫" in block, "只带了首帧，尾帧没进基线"
    assert "青石地、院角水缸" in block, "场景锚没进基线（它正是连续性闸门校验的字段）"
    assert "门环包浆与茶托位置不变" in block, "连续性约束没进基线"
    assert "原样保留" in block, "没写明未提到的部分不许漂移，模型会当成重新创作（纯重掷）"
    assert "以用户修订为准" in block, "没写明冲突时修订优先（票面 AC-2）"


def test_baseline_block_is_empty_without_a_previous_product():
    """首轮重出（尚无上一版产物）渲染成空串——调用方据此不设键、台账记 False，都不报错。"""
    assert render_baseline_block(None) == ""
    assert render_baseline_block({}) == ""
    assert render_baseline_block("这不是产物") == ""


def test_baseline_block_accepts_the_model_grouped_shape():
    """模型原始输出是「模型名分组」的 dict，与落盘的列表形态并存；两种都要认得。

    认不出来的后果不是报错而是**静默渲染成空块**——基线悄悄消失、台账还记着没上基线，
    正是本模块要消灭的形态。
    """
    block = render_baseline_block({
        "scene_anchor": "秋日庭院",
        "first": {"zimage": {"positive_prompt": "首帧-Z：橘猫蹲在青石上"}},
        "last": {"flux2": {"positive_prompt": "尾帧-F：橘猫抬头看红枫"}},
    })

    assert "首帧-Z：橘猫蹲在青石上" in block
    assert "尾帧-F：橘猫抬头看红枫" in block
    assert "秋日庭院" in block


def test_ledger_appends_one_row_per_round_with_full_snapshot(tmp_path):
    """每轮重出追加一行：轮次、当轮意见、当轮**全量**快照、该轮产物、是否用基线。"""
    state: dict = {}
    record_user_revision(state, layer=LAYER_FRAME, text="天空改成黄昏")
    log_frame_round(tmp_path, round_index=1, layer=LAYER_FRAME,
                    feedback_this_round="天空改成黄昏",
                    active_revisions=active_revisions(state),
                    bundle={"scene_anchor": "庭院", "first": [{"positive_prompt": "第一版"}]},
                    base_used=False)
    record_user_revision(state, layer=LAYER_FRAME, text="院中加红枫")
    log_frame_round(tmp_path, round_index=2, layer=LAYER_FRAME,
                    feedback_this_round="院中加红枫",
                    active_revisions=active_revisions(state),
                    bundle={"scene_anchor": "庭院", "first": [{"positive_prompt": "第二版"}]},
                    base_used=False)

    rows = _rows(tmp_path)
    assert [row["round"] for row in rows] == [1, 2]
    assert [row["feedback_this_round"] for row in rows] == ["天空改成黄昏", "院中加红枫"]
    # 快照而非增量：第 2 行带的是全量清单（两条），不是只带本轮新增那条
    assert [item["text"] for item in rows[0]["active_revisions"]] == ["天空改成黄昏"]
    assert [item["text"] for item in rows[1]["active_revisions"]] == ["天空改成黄昏", "院中加红枫"]
    assert rows[1]["bundle"]["first"][0]["positive_prompt"] == "第二版", "台账记的不是该轮产物"
    assert rows[1]["base_used"] is False
    # 层次既记机器值也记人读标签（与 gate-log 的 action/action_label 同一形状）
    assert rows[1]["layer"] == LAYER_FRAME
    assert rows[1]["layer_label"] == "只改画面"


def test_any_revision_can_be_removed_by_its_screen_number():
    """按编号撤**任意**一条，不必从末尾逐条撤（issue #24 / T2）。

    票面场景：「先改成秋天、再加枫叶、再来个电影级镜头、最后改回春天」，此时要撤的是
    **第 1 条**（秋天）；只能撤末尾的话得把 4→3→2→1 全撤掉再把后三条重说一遍。
    """
    state: dict = {}
    for text in ("改成秋天", "加枫叶", "电影级镜头", "改回春天"):
        record_user_revision(state, layer=LAYER_FRAME, text=text)

    removed = remove_user_revision(state, 1)

    assert removed["text"] == "改成秋天"
    assert [item["text"] for item in active_revisions(state)] == ["加枫叶", "电影级镜头", "改回春天"]
    # 撤中间与撤末尾同样可行——位置编号每次都对应当前清单
    assert remove_user_revision(state, 2)["text"] == "电影级镜头"
    assert remove_user_revision(state, 2)["text"] == "改回春天"
    assert [item["text"] for item in active_revisions(state)] == ["加枫叶"]


def test_out_of_range_revoke_is_loud_and_changes_nothing():
    """编号超界必须报错：静默忽略会让用户以为撤掉了，随后「重出后改动还在」无法对账。"""
    state: dict = {}
    record_user_revision(state, layer=LAYER_FRAME, text="改成秋天")

    for bad in (0, 2, -1, "第1条", None):
        with pytest.raises(ValueError):
            remove_user_revision(state, bad)
    assert [item["text"] for item in active_revisions(state)] == ["改成秋天"], "报错时清单被改动了"


def test_round_keeps_advancing_on_revoke_only_rounds():
    """撤销轮没有新增条目，轮次计数器仍要往前推——否则连续两轮撤销共用同一个轮次号。"""
    state: dict = {}
    record_user_revision(state, layer=LAYER_FRAME, text="一")
    record_user_revision(state, layer=LAYER_FRAME, text="二")

    assert begin_round(state) == 3
    remove_user_revision(state, 2)
    assert begin_round(state) == 4
    assert next_round(state) == 5
    # 只增不减：撤到空清单也不回退（回退会让台账里出现两行 round 相同）
    remove_user_revision(state, 1)
    assert state["user_revisions"] == []
    assert begin_round(state) == 5


def test_screen_list_and_injected_block_share_one_numbering():
    """屏幕清单与注入块必须同一套编号：撤的是「模型看到的第 2 条吗」由此可判。"""
    state: dict = {}
    record_user_revision(state, layer=LAYER_FRAME, text="改成秋天")
    record_user_revision(state, layer=LAYER_FRAME, text="加枫叶")

    listing = render_revision_list(active_revisions(state))
    block = render_revision_block(active_revisions(state))

    assert [line for line in listing.splitlines()] == [
        "1. [只改画面] 改成秋天", "2. [只改画面] 加枫叶"]
    assert listing.splitlines() == block.splitlines()[:2], "两处编号/顺序不一致"


def test_revocation_block_tells_the_model_to_withdraw_the_change():
    """撤销要说给模型听（AC-3）：只摘清单的话，基线的「原样保留」会把已落地的改动保住。

    票面场景：撤掉被第 4 条覆盖掉的旧第 1 条「改成秋天」，第 4 条「改回春天」仍在生效——
    末句把「其余修订为准」写死，正是为了这种同处一地的两条不打架。
    """
    block = render_revocation_block([{"round": 1, "layer": LAYER_FRAME, "text": "改成秋天"}])

    assert "改成秋天" in block, "没写撤的是哪条，模型无从知道要撤回什么"
    assert "不再生效" in block
    assert "撤回" in block, "没要求撤回其造成的画面改动——那它就只会被基线原样保住"
    assert "以其余修订为准" in block, "没写清与仍在生效的其余修订冲突时谁优先"


def test_revocation_note_names_the_revoked_revisions_for_the_ledger():
    """台账本轮记录记的是**文本**不是编号：编号是现念的位置号，撤一条后其余条重排序，
    事后再看编号已经指不回原来那条。"""
    note = revocation_note([{"round": 2, "layer": LAYER_FRAME, "text": "加枫叶"}])

    assert "加枫叶" in note
    assert "无新意见" in note, "撤销轮留白会被后来的人读成漏记"
    assert revocation_note([]) == ""


def test_revocation_block_is_empty_without_revocations():
    """没撤任何条时渲染成空串（`_ctx` 空值跳过）——正常重出轮的消息逐字不变。"""
    assert render_revocation_block(None) == ""
    assert render_revocation_block([]) == ""


def test_screen_list_is_empty_without_revisions():
    """空清单渲染成空串（屏幕据此打「清单为空」），不报错——撤空后还要能重出（AC-6）。"""
    assert render_revision_list(None) == ""
    assert render_revision_list([]) == ""


def test_ledger_snapshot_shows_a_revision_dropped_between_rounds(tmp_path):
    """撤销本身不必单开一列：diff 相邻两轮的快照就能看出撤掉了哪条。"""
    state: dict = {}
    record_user_revision(state, layer=LAYER_FRAME, text="一")
    record_user_revision(state, layer=LAYER_FRAME, text="二")
    log_frame_round(tmp_path, round_index=1, layer=LAYER_FRAME, feedback_this_round="二",
                    active_revisions=active_revisions(state), bundle=None, base_used=False)

    state["user_revisions"] = [state["user_revisions"][0]]  # 撤掉第 2 条
    record_user_revision(state, layer=LAYER_FRAME, text="三")
    log_frame_round(tmp_path, round_index=2, layer=LAYER_FRAME, feedback_this_round="三",
                    active_revisions=active_revisions(state), bundle=None, base_used=False)

    rows = _rows(tmp_path)
    before = {item["text"] for item in rows[0]["active_revisions"]}
    after = {item["text"] for item in rows[1]["active_revisions"]}
    assert before - after == {"二"}, "快照 diff 看不出撤了哪条"
    assert after - before == {"三"}
