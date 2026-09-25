"""阶段 1 首帧修改循环：意见按轮累积（#23 / T1）、累积清单可按编号撤销（#24 / T2）。

用户实测（ADR 0005）：连提两次意见，第二次重出的产物里第一条的改动整体消失。
本文件走真实调用链——``_phase1_new`` + 真节点 + 真落盘——只打桩 LLM 与交互输入，
断言全落在**外部产物**上：发给模型的请求文本、落盘的会话状态、台账 jsonl。
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from minimax_h3_prompt.graph import nodes
from minimax_h3_prompt.session_store import load_session
from minimax_h3_prompt.ui import wizard
from minimax_h3_prompt.user_revisions import (
    FRAME_BASELINE_KEY,
    FRAME_REVOKED_KEY,
    FRAME_ROUNDS_FILENAME,
)

TOPIC = "秋日庭院里的橘猫"
FIRST_FEEDBACK = "天空改成黄昏，要暖金色"
SECOND_FEEDBACK = "院中加几片红枫"
THIRD_FEEDBACK = "改成电影级镜头"


def _bundle_payload(label: str) -> dict:
    """I2VA 单帧产物。主题无地点关键词 → 无主题词约束，校验只要求连续性与非空。"""
    return {
        "scene_anchor": "秋日庭院",
        "first": {"zimage": {
            "positive_prompt": f"秋日庭院里的橘猫（{label}）",
            "instructions": ["paste to node 187"],
        }},
        "continuity_constraints": ["保持同一只橘猫与同一庭院"],
    }


def _run_loop(tmp_path, monkeypatch, *, steps: list[tuple[str, Any]],
              initial_bundle: bool = True):
    """跑 _phase1_new 的修改循环；``steps`` 每项是一轮的脚本，最后答「没有更多意见」。

    每轮脚本的两形态：
    - ``("1", "意见文本")``：选「只重出画面提示词」，提这一条意见；
    - ``("3", [编号, ...])``：选「撤销清单里的某条」，按顺序撤这些编号，回车结束撤销
      （编号是**屏幕上当前清单的位置编号**，撤掉一条后其余条会重排序）。

    返回 ``(session, requests)``——requests 是每轮重出真正发给 frame agent 的请求文本。
    ``initial_bundle=False`` 模拟阶段 1 没产出关键帧产物（无上一版可作基线）。
    """
    scripted = [TOPIC, "", "", "1"]
    for kind, payload in steps:
        scripted.append(kind)
        if kind == "1":
            scripted.append(str(payload))
        else:
            scripted.extend(str(number) for number in payload)
            if payload:  # 空载荷＝选了撤销但没有可撤的条目，那一问根本不会出现
                scripted.append("")  # 回车=撤完，按当前清单重出
    prompts = iter(scripted)
    confirms = iter([True] * len(steps) + [False])
    monkeypatch.setattr(wizard, "_prompt", lambda *a, **k: next(prompts))
    monkeypatch.setattr(wizard, "_confirm", lambda *a, **k: next(confirms))
    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)

    requests: list[str] = []
    counter = {"n": 0}

    def fake_run_agent(agent, message):
        requests.append(message)
        counter["n"] += 1
        return json.dumps(_bundle_payload(f"第{counter['n']}版"))

    def fake_run_stage1(brief, config, **kwargs):
        state = {"shot_table": "[Shot 1] 庭院空镜"}
        if initial_bundle:
            state["fl2va_prompt_bundle"] = _bundle_payload("初版")
        return (state, object(), {"frame_prompt_engineer": object()})

    monkeypatch.setattr(nodes, "run_agent", fake_run_agent)
    monkeypatch.setattr(wizard, "run_stage1", fake_run_stage1)
    # max_clarification_rounds=0：本文件考的是首帧修改循环，起步澄清（issue #29）会多
    # 消耗一次交互输入，这里显式关掉——它的题目由 tests/test_wizard_clarification.py 承担。
    config = SimpleNamespace(default_duration=5.0, default_language="Chinese",
                             sessions_root=tmp_path / "sessions",
                             max_clarification_rounds=0)
    return wizard._phase1_new(config), requests


def test_second_frame_feedback_keeps_the_first(tmp_path, monkeypatch):
    """本票的核心：第二次重出**同时**体现两条意见，第一条不被顶掉。"""
    session, requests = _run_loop(tmp_path, monkeypatch,
                                  steps=[("1", FIRST_FEEDBACK), ("1", SECOND_FEEDBACK)])

    assert len(requests) == 2, "两条意见应各触发一次重出"
    assert FIRST_FEEDBACK in requests[0]
    assert FIRST_FEEDBACK in requests[1], "第 2 轮重出把第 1 条意见丢了——正是用户实测的缺陷"
    assert SECOND_FEEDBACK in requests[1]
    assert session is not None
    assert [item["text"] for item in session.stage_state["user_revisions"]] == [
        FIRST_FEEDBACK, SECOND_FEEDBACK]


def test_topic_field_never_carries_feedback(tmp_path, monkeypatch):
    """主题字段只装主题：意见不再经 plot 走私（它会污染地点守卫与常识判官的判据）。"""
    session, _ = _run_loop(tmp_path, monkeypatch,
                           steps=[("1", FIRST_FEEDBACK), ("1", SECOND_FEEDBACK)])

    assert session is not None
    assert session.brief.plot == TOPIC
    assert FIRST_FEEDBACK not in session.brief.plot
    raw = json.loads((session.directory / "session-state.json").read_text(encoding="utf-8"))
    assert raw["brief"]["plot"] == TOPIC, "落盘的 brief 里主题被意见污染了"
    assert raw["stage_state"]["user_revisions"], "修订清单没进持久化白名单（崩一次即丢）"


def test_ledger_records_each_round_with_snapshot_and_product(tmp_path, monkeypatch):
    """每轮重出留一行台账：轮次、当轮意见、全量快照、该轮产物、是否用基线。"""
    session, _ = _run_loop(tmp_path, monkeypatch,
                           steps=[("1", FIRST_FEEDBACK), ("1", SECOND_FEEDBACK)])
    assert session is not None

    lines = (session.directory / FRAME_ROUNDS_FILENAME).read_text(encoding="utf-8").splitlines()
    rows = [json.loads(line) for line in lines]
    assert [row["round"] for row in rows] == [1, 2]
    assert [row["feedback_this_round"] for row in rows] == [FIRST_FEEDBACK, SECOND_FEEDBACK]
    assert [item["text"] for item in rows[1]["active_revisions"]] == [
        FIRST_FEEDBACK, SECOND_FEEDBACK]
    # 该轮产物：第 2 行记的必须是第 2 轮重出的那一版，不能是上一版
    assert "第2版" in rows[1]["bundle"]["first"][0]["positive_prompt"]
    assert "第1版" in rows[0]["bundle"]["first"][0]["positive_prompt"]
    # base_used 是「累积到底生效没有」的分组变量：两轮都以各自上一版为基线，如实记 True
    assert [row["base_used"] for row in rows] == [True, True]


def test_regeneration_uses_the_previous_version_as_baseline(tmp_path, monkeypatch):
    """本票的核心：新一轮重出以上一轮**完整产物**为基线，用户没提过的细节不再漂走。

    实测（票面）：第三轮产物不只丢了枫叶，连第一轮就有的「门环包浆／茶托」也一起漂了——
    那正是纯重掷的代价。这里断言上一版的产物文本真的进了下一轮请求。
    """
    session, requests = _run_loop(tmp_path, monkeypatch,
                                  steps=[("1", FIRST_FEEDBACK), ("1", SECOND_FEEDBACK)])

    assert "初版" in requests[0], "第 1 轮没有以阶段 1 那一版为基线"
    assert "第1版" in requests[1], "第 2 轮没以第 1 轮产物为基线——正是『纯重掷』的形态"
    assert "秋日庭院" in requests[1], "基线的场景锚没进请求（它是连续性闸门校验的字段）"
    assert "以用户修订为准" in requests[1], "没声明冲突时修订优先（AC-2）"
    # 基线是**本轮的入参**不是产物：不得落进会话状态流到阶段 2 / 续接路径上去
    assert session is not None
    raw = json.loads((session.directory / "session-state.json").read_text(encoding="utf-8"))
    assert FRAME_BASELINE_KEY not in raw["stage_state"]


def test_first_regeneration_without_a_previous_product_records_no_baseline(tmp_path, monkeypatch):
    """尚无上一版产物（阶段 1 没产出关键帧）时首轮重出不因此报错，台账如实记 False。

    这一格不能记 True：`base_used` 是事后分组变量，错记会把「纯重掷」的轮次混进
    「上了基线」那组，让累积到底生效的判断失去依据。
    """
    session, requests = _run_loop(tmp_path, monkeypatch, steps=[("1", FIRST_FEEDBACK)],
                                  initial_bundle=None)

    assert session is not None
    assert "修订基线" not in requests[0], "没有上一版产物却发出了基线块"
    rows = [json.loads(line) for line in
            (session.directory / FRAME_ROUNDS_FILENAME).read_text(encoding="utf-8").splitlines()]
    assert rows[0]["base_used"] is False
    assert FIRST_FEEDBACK in requests[0], "没有基线时更要落实用户这条修订本身"


def test_screen_shows_numbered_list_and_round_count(tmp_path, monkeypatch, capsys):
    """AC-1/AC-4：屏幕上有带编号的累积清单，并标出轮次与累积条数。

    编号是撤销时要念的那个编号（与注入块同源），所以层次标签也要一起打出来——只给编号
    而清单在别处，用户无从对照自己撤的是哪条。
    """
    _run_loop(tmp_path, monkeypatch,
              steps=[("1", FIRST_FEEDBACK), ("1", SECOND_FEEDBACK)])

    out = capsys.readouterr().out
    # 票面那句字面形态：「第 N 轮 · 累积 M 条」。N 是**当前**轮次（屏幕上那一版产物的轮次）
    assert "第 2 轮 · 累积 2 条" in out, "屏幕没有标出当前轮次与累积条数"
    # 另打一个下一次的轮次号：撤销轮要靠它事后与台账对账（台账行号＝这一轮的号）
    assert "下一次重出记为第 3 轮" in out, "屏幕没有标出下一次重出记入的轮次"
    assert f"1. [只改画面] {FIRST_FEEDBACK}" in out, "清单没带编号与层次（编号无法与撤销操作对应）"
    assert f"2. [只改画面] {SECOND_FEEDBACK}" in out


def test_any_revision_can_be_revoked_not_just_the_last(tmp_path, monkeypatch):
    """AC-2/AC-3：撤**中间**那条后重出——只有它的改动消失，前后两条仍在。

    票面场景是先改成秋天、再加枫叶、再来个电影级镜头，此时要撤的可能是夹在中间的那条；
    只能撤末尾的话得把后面的全撤掉再重说一遍。
    """
    session, requests = _run_loop(tmp_path, monkeypatch,
                                  steps=[("1", FIRST_FEEDBACK), ("1", SECOND_FEEDBACK),
                                         ("1", THIRD_FEEDBACK), ("3", [2])])

    assert len(requests) == 4, "撤销应触发一次重出"
    # 活动清单的行带层次标签，撤销块的行不带——按层次标签判「还在不在生效清单里」。
    # 被撤那条仍会出现在请求里，但那是在撤销块里（「这些不再生效」），不是作为要求。
    assert f"[只改画面] {SECOND_FEEDBACK}" not in requests[3], "被撤的那条仍在生效清单里"
    assert SECOND_FEEDBACK in requests[3], "撤销没告诉模型撤的是哪一条"
    assert f"[只改画面] {FIRST_FEEDBACK}" in requests[3], "其余各条被连带撤掉了"
    assert f"[只改画面] {THIRD_FEEDBACK}" in requests[3], "其余各条被连带撤掉了"
    assert session is not None
    assert [item["text"] for item in session.stage_state["user_revisions"]] == [
        FIRST_FEEDBACK, THIRD_FEEDBACK]


def test_several_revisions_can_be_revoked_in_one_round_with_renumbering(tmp_path, monkeypatch):
    """一轮内连撤多条：撤掉一条后其余条重排序，下一次念的是**重排后**的编号。

    屏幕每撤一条就重打一次清单正是为这件事——照上一屏的编号接着撤就会撤错人。
    这里先撤第 3 条（清单变两条），再念「2」撤掉的是**原第 2 条**。
    """
    session, requests = _run_loop(tmp_path, monkeypatch,
                                  steps=[("1", FIRST_FEEDBACK), ("1", SECOND_FEEDBACK),
                                         ("1", THIRD_FEEDBACK), ("3", [3, 2])])

    assert len(requests) == 4, "连撤两条只该重出一次"
    assert f"[只改画面] {FIRST_FEEDBACK}" in requests[3], "没撤的那条被连带撤掉了"
    assert f"[只改画面] {SECOND_FEEDBACK}" not in requests[3], "第 2 条没撤掉"
    assert f"[只改画面] {THIRD_FEEDBACK}" not in requests[3], "第 3 条没撤掉"
    # 两条都进了撤销块（要说给模型听，见 AC-3），但都不是「仍在生效」的行
    assert SECOND_FEEDBACK in requests[3] and THIRD_FEEDBACK in requests[3]
    assert session is not None
    assert [item["text"] for item in session.stage_state["user_revisions"]] == [FIRST_FEEDBACK]


def test_revocation_round_tells_the_model_to_withdraw_the_change(tmp_path, monkeypatch):
    """AC-3 的接线：撤销轮必须把「这些修订不再生效、其改动要撤回」说给模型。

    只把条目从注入清单里摘掉是不够的——基线那块明写「用户修订没提到的部分必须原样保留」，
    上一版里因该条而落地的改动正是被它保住的（票面场景：秋日庭院）。
    """
    session, requests = _run_loop(tmp_path, monkeypatch,
                                  steps=[("1", FIRST_FEEDBACK), ("3", [1])])

    assert "不再生效" not in requests[0], "普通重出轮带上了撤销说明——消息本该逐字不变"
    assert FIRST_FEEDBACK in requests[1] and "不再生效" in requests[1], "撤销没告诉模型"
    assert "撤回" in requests[1], "说了撤销却没说要把已落地的改动撤回来"
    # 与基线同为**本轮的入参**：不得落进会话状态流到阶段 2 / 续接路径上去
    assert session is not None
    raw = json.loads((session.directory / "session-state.json").read_text(encoding="utf-8"))
    assert FRAME_REVOKED_KEY not in raw["stage_state"]


def test_revocation_round_is_recorded_in_the_ledger_by_snapshot(tmp_path, monkeypatch):
    """AC-5：台账快照能事后看出「第 N 轮撤掉了哪条」——撤销本身不单开一列。"""
    session, _ = _run_loop(tmp_path, monkeypatch,
                           steps=[("1", FIRST_FEEDBACK), ("1", SECOND_FEEDBACK), ("3", [1])])
    assert session is not None

    rows = [json.loads(line) for line in
            (session.directory / FRAME_ROUNDS_FILENAME).read_text(encoding="utf-8").splitlines()]
    assert [row["round"] for row in rows] == [1, 2, 3], "撤销轮没有自己的轮次号"
    before = {item["text"] for item in rows[1]["active_revisions"]}
    after = {item["text"] for item in rows[2]["active_revisions"]}
    assert before - after == {FIRST_FEEDBACK}, "快照 diff 看不出撤了哪条"
    assert after == {SECOND_FEEDBACK}, "撤销轮带上了本该撤掉的那条"
    assert FIRST_FEEDBACK in rows[2]["feedback_this_round"], "撤销轮的本轮记录读不出撤了什么"
    assert rows[2]["base_used"] is True, "撤销轮同样以上一版为基线，台账要如实记"


def test_regeneration_still_works_after_the_list_is_emptied(tmp_path, monkeypatch, capsys):
    """AC-6：清单被删空后重出仍可用，不崩——空清单就是「不带上任何修订」。"""
    session, requests = _run_loop(tmp_path, monkeypatch,
                                  steps=[("1", FIRST_FEEDBACK), ("3", [1])])
    assert session is not None
    assert len(requests) == 2, "撤空后没有重出"
    # 清单空 → 注入块渲染成空串被 _ctx 跳过（agent 提示词里本来就有「用户修订」字样，
    # 故只能按**块自己的句子**判，按这两个字判会永远为真）
    assert "每一条都必须落实" not in requests[1], "空清单仍注入了修订块"
    assert f"[只改画面] {FIRST_FEEDBACK}" not in requests[1], "撤空的条目仍在生效清单里"
    assert "不再生效" in requests[1], "撤空这一轮没告诉模型这些改动要撤回"
    assert session.stage_state["user_revisions"] == []
    rows = [json.loads(line) for line in
            (session.directory / FRAME_ROUNDS_FILENAME).read_text(encoding="utf-8").splitlines()]
    assert rows[1]["active_revisions"] == []
    out = capsys.readouterr().out
    assert "累积 0 条" in out, "撤空后屏幕没告诉用户清单已空"
    # 撤空那一刻的措辞不能是「没有可撤销的条目」——紧跟上一行的「[已撤销] 第 N 条」，
    # 会被读成刚才那下没撤掉
    assert "清单已撤空" in out


def test_choosing_revoke_with_an_empty_list_does_not_crash(tmp_path, monkeypatch, capsys):
    """清单为空时选「撤销」不崩、也不重出——只是提示没有可撤的条目。"""
    session, requests = _run_loop(tmp_path, monkeypatch, steps=[("3", [])])

    assert session is not None
    assert requests == [], "清单空着却发起了重出"
    out = capsys.readouterr().out
    assert "没有可撤销的条目" in out
    assert "清单已撤空" not in out, "一条都没撤，却说成了「已撤空」"


def test_single_feedback_leaves_one_ledger_row(tmp_path, monkeypatch):
    """只提一条时台账只有一行——「每轮重出追加一行」不是「每次运行追加一行」。"""
    session, _ = _run_loop(tmp_path, monkeypatch, steps=[("1", FIRST_FEEDBACK)])
    assert session is not None
    rows = [json.loads(line) for line in
            (session.directory / FRAME_ROUNDS_FILENAME).read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["round"] == 1
    assert session.stage_state["frame_round"] == 1
