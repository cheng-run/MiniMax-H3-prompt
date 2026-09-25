"""阶段 1 首帧修改循环：意见按轮累积（issue #23 / T1）。

用户实测（ADR 0005）：连提两次意见，第二次重出的产物里第一条的改动整体消失。
本文件走真实调用链——``_phase1_new`` + 真节点 + 真落盘——只打桩 LLM 与交互输入，
断言全落在**外部产物**上：发给模型的请求文本、落盘的会话状态、台账 jsonl。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from minimax_h3_prompt.graph import nodes
from minimax_h3_prompt.session_store import load_session
from minimax_h3_prompt.ui import wizard
from minimax_h3_prompt.user_revisions import FRAME_ROUNDS_FILENAME

TOPIC = "秋日庭院里的橘猫"
FIRST_FEEDBACK = "天空改成黄昏，要暖金色"
SECOND_FEEDBACK = "院中加几片红枫"


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


def _run_loop(tmp_path, monkeypatch, *, feedbacks: list[str]):
    """跑 _phase1_new 的修改循环：每条意见提一次，最后答「没有更多意见」。

    返回 ``(session, requests)``——requests 是每轮重出真正发给 frame agent 的请求文本。
    """
    prompts = iter(
        [TOPIC, "", "", "1"]
        + [value for item in feedbacks for value in ("1", item)]  # 1 = 只重出画面提示词
    )
    confirms = iter([True] * len(feedbacks) + [False])
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
        return (
            {"fl2va_prompt_bundle": _bundle_payload("初版"), "shot_table": "[Shot 1] 庭院空镜"},
            object(),
            {"frame_prompt_engineer": object()},
        )

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
                                  feedbacks=[FIRST_FEEDBACK, SECOND_FEEDBACK])

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
                           feedbacks=[FIRST_FEEDBACK, SECOND_FEEDBACK])

    assert session is not None
    assert session.brief.plot == TOPIC
    assert FIRST_FEEDBACK not in session.brief.plot
    raw = json.loads((session.directory / "session-state.json").read_text(encoding="utf-8"))
    assert raw["brief"]["plot"] == TOPIC, "落盘的 brief 里主题被意见污染了"
    assert raw["stage_state"]["user_revisions"], "修订清单没进持久化白名单（崩一次即丢）"


def test_ledger_records_each_round_with_snapshot_and_product(tmp_path, monkeypatch):
    """每轮重出留一行台账：轮次、当轮意见、全量快照、该轮产物、是否用基线。"""
    session, _ = _run_loop(tmp_path, monkeypatch,
                           feedbacks=[FIRST_FEEDBACK, SECOND_FEEDBACK])
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
    assert rows[1]["base_used"] is False, "基线（T3）尚未实现，这里必须如实记 False"


def test_single_feedback_leaves_one_ledger_row(tmp_path, monkeypatch):
    """只提一条时台账只有一行——「每轮重出追加一行」不是「每次运行追加一行」。"""
    session, _ = _run_loop(tmp_path, monkeypatch, feedbacks=[FIRST_FEEDBACK])
    assert session is not None
    rows = [json.loads(line) for line in
            (session.directory / FRAME_ROUNDS_FILENAME).read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["round"] == 1
    assert session.stage_state["frame_round"] == 1
