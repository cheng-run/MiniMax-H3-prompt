"""用户修订真源与修订台账（issue #23 / T1）。

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
    log_frame_round,
    record_user_revision,
    render_revision_block,
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
