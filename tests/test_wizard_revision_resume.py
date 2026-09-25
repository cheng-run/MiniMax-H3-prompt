"""续接路径上的首帧修改循环（issue #27 / T5）。

两条续接路径原本都只有「展示提示词」、**没有修改入口**（新跑路径 ``_phase1_new`` 有）：

- ``_resume_stage1``：阶段 1 断点续跑，恢复完成只把提示词打出来；
- ``run_wizard`` 里续接到 ``awaiting_frames`` 的会话：不展示清单，直接掉进阶段 2 的收图提问
  ——这条才是「重启后清单仍然非空」的真实现场（用户在首帧循环里提过意见、还没提交帧图就退出）。

本文件断言这两条路径都接上**同一份**修改循环：续接后清单仍在、能接着提意见、能接着重出。
走真实调用链（``_resume_stage1`` / ``run_wizard`` + 真节点 + 真落盘），只打桩 LLM 与交互输入，
断言落在**外部产物**上：发给模型的请求文本、落盘的会话状态、屏幕证据块。
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from minimax_h3_prompt import model_factory, session_store
from minimax_h3_prompt.brief_parser import Brief
from minimax_h3_prompt.graph import nodes
from minimax_h3_prompt.session_store import load_session
from minimax_h3_prompt.ui import wizard

TOPIC = "秋日庭院里的橘猫"
# 续接**之前**就已累积的那一条（人机修改循环的产物，随会话落盘）。
EXISTING = "天空改成黄昏，要暖金色"
# 续接之后新提的那一条。
NEW = "院中加几片红枫"


class _StopPhase2(Exception):
    """阶段 2 的重活在测试里切成哨兵异常——本文件只关心进阶段 2 之前的修改循环。"""


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


def _brief() -> Brief:
    return Brief(mode="base", variant="I2VA", duration=5.0, style="写实电影感", plot=TOPIC)


def _config(tmp_path) -> SimpleNamespace:
    # max_clarification_rounds=0：起步澄清会多消耗交互输入，这里显式关掉——那是
    # tests/test_wizard_clarification.py 的题目。
    return SimpleNamespace(default_duration=5.0, default_language="Chinese",
                           sessions_root=tmp_path / "sessions", max_clarification_rounds=0)


def _scripted(monkeypatch, *, prompts: list[str], confirms: list[bool]) -> list[str]:
    """脚本化交互：``_prompt`` 按序吐 prompts、``_confirm`` 按序吐 confirms。返回提案文案清单。

    ``_prompt`` 会把提示语**打到屏幕上**（真 ``input(text)`` 就是这么做的），于是「提问文案
    里到底写了什么」也能从 capsys 读到，而不只是断言它被调用过。
    """
    pending_prompts, pending_confirms = list(prompts), list(confirms)
    seen: list[str] = []

    def fake_prompt(text, *a, **k):
        seen.append(str(text))
        print(text, end="")
        return pending_prompts.pop(0)

    def fake_confirm(text, *a, **k):
        return pending_confirms.pop(0)

    monkeypatch.setattr(wizard, "_prompt", fake_prompt)
    monkeypatch.setattr(wizard, "_confirm", fake_confirm)
    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)
    return seen


def _fake_llm(monkeypatch) -> list[str]:
    """假 LLM：记录每轮重出真正发给 frame agent 的请求文本，返回一版新产物。"""
    requests: list[str] = []
    counter = {"n": 0}

    def fake_run_agent(agent, message):
        requests.append(message)
        counter["n"] += 1
        return json.dumps(_bundle_payload(f"第{counter['n']}版"))

    monkeypatch.setattr(nodes, "run_agent", fake_run_agent)
    return requests


def _stub_agent_build(monkeypatch) -> None:
    """拦掉「就地构建 model/agents」——续接到等帧图的会话手上没有 run_stage1 的返回值。"""
    monkeypatch.setattr(model_factory, "build_chat_model", lambda: SimpleNamespace())
    monkeypatch.setattr("minimax_h3_prompt.agents.build_role_agents",
                        lambda *a, **k: {"frame_prompt_engineer": object()})


def _rev(round_index: int, text: str) -> dict[str, Any]:
    return {"round": round_index, "layer": "frame", "text": text}


def _session(tmp_path, *, status: str, revisions: list[dict],
             last_node: str = "parallel_visual") -> session_store.SessionState:
    """造一个带累积清单的续接现场：阶段 1 已产出关键帧产物，清单里已有旧修订。

    ``stage1_running`` 那份就是 ``_resume_stage1`` 的真实现场（默认断在 ``parallel_visual``；
    ``last_node`` 传 ``parallel_sound`` 则是「节点全跑完、只差收尾没落盘」的现场）；
    ``awaiting_frames`` 那份是「阶段 1 完成、等帧图」的现场。
    """
    gen = tmp_path / "sessions" / "某主题" / "GEN001"
    stage_state: dict[str, Any] = {
        "shot_table": "[Shot 1] 庭院空镜",
        "fl2va_prompt_bundle": _bundle_payload("初版"),
        "user_revisions": [dict(item) for item in revisions],
        "frame_round": len(revisions),
    }
    if status == session_store.STATUS_STAGE1_RUNNING:
        stage_state["_progress"] = {"last_completed_node": last_node}
    session_store.save_session(gen, _brief(), stage_state, status=status)
    loaded = load_session(gen)
    assert loaded is not None
    return loaded


def test_resume_stage1_offers_the_revision_loop(tmp_path, monkeypatch, capsys):
    """AC-1：阶段 1 续接恢复完成后，累积清单仍在，并且能接着提意见、接着重出。"""
    session = _session(tmp_path, status=session_store.STATUS_STAGE1_RUNNING,
                       revisions=[_rev(1, EXISTING)])
    # 菜单 "1" → 意见正文 → 层次（空串＝回车＝只改画面，issue #28 起每次提意见都要声明）
    _scripted(monkeypatch, prompts=["1", NEW, ""], confirms=[True, False])
    requests = _fake_llm(monkeypatch)
    # 续接的收尾节点（fl2va_frame_prompts 之后的链）不再真跑：本测试的题目是收尾后的循环。
    monkeypatch.setattr(
        wizard, "run_stage1",
        lambda *a, **k: (dict(session.stage_state), SimpleNamespace(),
                         {"frame_prompt_engineer": object()}),
    )

    resumed = wizard._resume_stage1(_config(tmp_path), session)

    assert resumed is not None
    assert [item["text"] for item in resumed.stage_state["user_revisions"]] == [EXISTING, NEW], \
        "续接后清单被吞了，或新意见没进累积真源"
    assert len(requests) == 1, "续接后提了意见却没有重出"
    assert EXISTING in requests[0], "重出没带上续接前就已累积的那条"
    assert NEW in requests[0], "重出没带上本轮新提的那条"
    out = capsys.readouterr().out
    assert f"1. [只改画面] {EXISTING}" in out, "续接后看不到累积清单（没编号就对不上撤销）"
    assert "累积 1 条" in out, "续接后屏幕没标出累积条数"


def test_resumed_awaiting_session_offers_the_revision_loop(tmp_path, monkeypatch, capsys):
    """AC-1：续接到「阶段 1 已完成、等帧图」的会话，也补上了同一个修改循环。

    这条路径今天直接进阶段 2 收图提问：清单看不到、意见提不了——而它正是「重启后清单
    仍然非空」的唯一真实现场。
    """
    session = _session(tmp_path, status=session_store.STATUS_AWAITING_FRAMES,
                       revisions=[_rev(1, EXISTING)])
    _scripted(monkeypatch, prompts=["1", NEW, ""], confirms=[True, False])
    requests = _fake_llm(monkeypatch)
    _stub_agent_build(monkeypatch)
    monkeypatch.setattr(wizard.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(wizard, "_offer_resume", lambda config: session)
    phase2: dict[str, Any] = {}

    def stop_phase2(config, session, *, show_frame_prompts=True):
        phase2["show"] = show_frame_prompts
        raise _StopPhase2()

    monkeypatch.setattr(wizard, "_phase2_collect_and_finish", stop_phase2)

    with pytest.raises(_StopPhase2):
        wizard.run_wizard(_config(tmp_path))

    assert len(requests) == 1, "续接后提了意见却没有重出"
    assert EXISTING in requests[0] and NEW in requests[0], \
        "重出没同时带上续接前的旧条与本轮新条"
    assert phase2["show"] is False, "循环里已经展示过提示词，进阶段 2 不该再刷一遍"
    out = capsys.readouterr().out
    assert f"1. [只改画面] {EXISTING}" in out, "续接后看不到累积清单"


def test_resume_stage1_with_all_nodes_done_still_reaches_the_loop(tmp_path, monkeypatch, capsys):
    """节点全跑完、只差收尾没落盘的续接也要有修改入口（同日实测的第三处同类接线）。

    现场：``_save_progress`` 在**最后一个节点**完成时就记下 ``parallel_sound``，随后
    ``common_sense_qa``（默认开，最多 2 轮 LLM，好几分钟）期间中断 → 会话停在
    ``stage1_running`` 而链上已无下一节点。此前这条路径 ``_resume_stage1`` 直接返回会话：
    提示词不展示、意见提不了，状态还一直挂在「可续接」上。
    """
    session = _session(tmp_path, status=session_store.STATUS_STAGE1_RUNNING,
                       revisions=[_rev(1, EXISTING)], last_node="parallel_sound")
    _scripted(monkeypatch, prompts=["1", NEW, ""], confirms=[True, False])
    requests = _fake_llm(monkeypatch)
    # 这条路径手上没有 run_stage1 的返回值 → 重出时**就地**建 model/agents，必须打桩：
    # 不打的话用例会去读环境里的真 API key，主树有 .env 时绿、干净检出里报「缺失
    # DASHSCOPE_API_KEY」——一个只在本机能过的用例（提交树验证时实测抓到）。
    _stub_agent_build(monkeypatch)

    def must_not_rerun_chain(*a, **k):
        raise AssertionError("链上已无下一节点，却又重跑了阶段 1 的图")

    monkeypatch.setattr(wizard, "run_stage1", must_not_rerun_chain)

    resumed = wizard._resume_stage1(_config(tmp_path), session)

    assert resumed is not None
    assert len(requests) == 1, "这条续接路径还是没有修改入口"
    assert EXISTING in requests[0] and NEW in requests[0], "重出没带上累积修订与本轮新意见"
    assert resumed.status == session_store.STATUS_AWAITING_FRAMES, \
        "收尾没有把状态从 stage1_running 落到等帧图（会话会永远挂在「可续接」上）"
    out = capsys.readouterr().out
    assert f"1. [只改画面] {EXISTING}" in out, "续接后看不到累积清单"
    assert "I2VA 首帧生图提示词" in out, "续接后没有展示生图提示词"


def test_restart_from_a_resume_entry_reruns_phase1_and_drops_the_old_list(tmp_path, monkeypatch,
                                                                         capsys):
    """AC-3/AC-4 在**续接入口**上同样成立：选「重来」要真重跑阶段 1，而不是被这条路径吞掉。

    续接路径的收尾是「循环返回哨兵 → 重跑阶段 1」，这一跳如果漏接，用户选了重来却只看到
    清单被清空、然后被送进阶段 2——清空生效了、「重来」没生效，比不做还糟。
    """
    session = _session(tmp_path, status=session_store.STATUS_AWAITING_FRAMES,
                       revisions=[_rev(1, EXISTING)])
    # 循环里先选「2 重来」；重来后 _phase1_new 重新问主题/时长/风格/变体（那个 "1" 是
    # 变体三选一），再进循环选「1 提意见并重出」、提一条新意见、答层次（空串＝只改画面）
    _scripted(monkeypatch, prompts=["2", TOPIC, "", "", "1", "1", NEW, ""],
              confirms=[True, True, False])
    requests = _fake_llm(monkeypatch)
    _stub_agent_build(monkeypatch)
    monkeypatch.setattr(wizard.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(wizard, "_offer_resume", lambda config: session)
    monkeypatch.setattr(wizard, "_phase2_collect_and_finish",
                        lambda *a, **k: (_ for _ in ()).throw(_StopPhase2()))
    runs: list[str] = []

    def fake_run_stage1(brief, config, **kwargs):
        runs.append(brief.plot)
        return ({"shot_table": "[Shot 1] 重来版",
                 "fl2va_prompt_bundle": _bundle_payload("重来版")},
                SimpleNamespace(), {"frame_prompt_engineer": object()})

    monkeypatch.setattr(wizard, "run_stage1", fake_run_stage1)

    with pytest.raises(_StopPhase2):
        wizard.run_wizard(_config(tmp_path))

    assert runs == [TOPIC], "选了「整个流程重来」却没有重跑阶段 1（这一跳被续接路径漏接了）"
    assert len(requests) == 1 and NEW in requests[0], "重来后提的意见没有重出"
    assert EXISTING not in requests[0], "重来后的请求里还带着被推翻路线上的旧修订"
    # 被推翻的那个会话目录：清单当场清空落盘，新一版从空清单开始
    old = load_session(tmp_path / "sessions" / "某主题" / "GEN001")
    assert old is not None and old.stage_state["user_revisions"] == [], \
        "被推翻的会话清单没清空——重来后崩一次再续接，旧要求又回来了"
    out = capsys.readouterr().out
    assert "已清空累积的修订清单" in out and "会清空当前累积的修订清单" in out


def test_resumed_awaiting_session_can_go_straight_on_without_feedback(tmp_path, monkeypatch):
    """没有意见就一路走：续接后答「没有更多意见」不该触发重出，也不该多建 LLM 客户端。

    这条是「补上循环」的代价检查——续接一条等帧图的会话本是零 LLM 成本的路径，循环必须
    在**真的要重出**时才构建 model/agents。
    """
    session = _session(tmp_path, status=session_store.STATUS_AWAITING_FRAMES,
                       revisions=[_rev(1, EXISTING)])
    _scripted(monkeypatch, prompts=[], confirms=[False])
    _fake_llm(monkeypatch)
    built: list[int] = []
    monkeypatch.setattr(model_factory, "build_chat_model",
                        lambda: built.append(1) or SimpleNamespace())
    monkeypatch.setattr("minimax_h3_prompt.agents.build_role_agents",
                        lambda *a, **k: {"frame_prompt_engineer": object()})
    monkeypatch.setattr(wizard.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(wizard, "_offer_resume", lambda config: session)
    monkeypatch.setattr(wizard, "_phase2_collect_and_finish",
                        lambda *a, **k: (_ for _ in ()).throw(_StopPhase2()))

    with pytest.raises(_StopPhase2):
        wizard.run_wizard(_config(tmp_path))

    assert built == [], "没提意见却已经建了 LLM 客户端（续接等帧图的会话本是零成本路径）"


def test_both_resume_paths_run_the_same_loop(tmp_path, monkeypatch):
    """AC-2「两条路径共用同一份重出逻辑，不是复制一份」——三条入口都走同一个函数。

    这是**结构断言**，与设计稿 Testing Decisions 那句「只断言外部行为……不断言内部函数调用
    顺序或私有结构」**有意相抵**，理由写在下面：行为测试只能证明两条路径**碰巧**表现一致，
    把同一份逻辑复制一份也能全绿，而「改一处、漏一处」正是本仓反复出现的形态（ADR 0005 的
    五处根因里两处就是它）。函数改名时这里会红，那是它该付的代价。
    """
    calls: list[str] = []
    real = wizard._user_revision_loop

    def spy(*args, **kwargs):
        calls.append("loop")
        return real(*args, **kwargs)

    monkeypatch.setattr(wizard, "_user_revision_loop", spy)
    _fake_llm(monkeypatch)
    _stub_agent_build(monkeypatch)
    monkeypatch.setattr(wizard.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(wizard, "_phase2_collect_and_finish",
                        lambda *a, **k: (_ for _ in ()).throw(_StopPhase2()))
    monkeypatch.setattr(wizard, "run_stage1",
                        lambda *a, **k: ({}, SimpleNamespace(),
                                         {"frame_prompt_engineer": object()}))

    # ① 续接阶段 1（断在 parallel_visual）
    stage1 = _session(tmp_path / "a", status=session_store.STATUS_STAGE1_RUNNING,
                      revisions=[_rev(1, EXISTING)])
    _scripted(monkeypatch, prompts=[], confirms=[False])
    wizard._resume_stage1(_config(tmp_path / "a"), stage1)

    # ② 续接到等帧图的会话
    awaiting = _session(tmp_path / "b", status=session_store.STATUS_AWAITING_FRAMES,
                        revisions=[_rev(1, EXISTING)])
    _scripted(monkeypatch, prompts=[], confirms=[False])
    monkeypatch.setattr(wizard, "_offer_resume", lambda config: awaiting)
    with pytest.raises(_StopPhase2):
        wizard.run_wizard(_config(tmp_path / "b"))

    # ③ 新跑路径
    _scripted(monkeypatch, prompts=[TOPIC, "", "", "1"], confirms=[False])
    wizard._phase1_new(_config(tmp_path / "c"))

    assert len(calls) == 3, f"有入口没走共享的修改循环（{len(calls)}/3）——多半是又复制了一份"
