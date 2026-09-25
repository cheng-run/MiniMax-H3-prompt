"""生图/视频提示词日常常识 QA 测试：JSON 解析/修复、阶段 1/2 重生成循环（全 mock，不打真实 API）。"""
import json
from types import SimpleNamespace

import pytest

from minimax_h3_prompt.brief_parser import Brief
from minimax_h3_prompt.graph import pipeline
from minimax_h3_prompt.graph import nodes as nodes_mod
from minimax_h3_prompt.tools import frame_sanity
from minimax_h3_prompt.tools.frame_sanity import (
    frame_common_sense_issues,
    prompt_common_sense_issues,
)
from minimax_h3_prompt.tools.h3_validator import ValidationIssue
from minimax_h3_prompt.user_revisions import render_baseline_block


def _brief():
    return Brief(variant="FL2VA", duration=5, plot="中国美女网红旅游 vlog")


def _agents():
    return {"qa": object()}


def test_frame_common_sense_parses_json(monkeypatch):
    monkeypatch.setattr(frame_sanity, "run_agent", lambda agent, msg: json.dumps({
        "issues": [{"severity": "error", "code": "SHOOTING_PERSPECTIVE", "message": "被拍人物手持相机"}],
    }))
    issues = frame_common_sense_issues({
        "scene_anchor": "beach",
        "first": [{"model_family": "zimage", "positive_prompt": "A woman holds a camera on a beach."}],
    }, _brief(), _agents())
    assert len(issues) == 1
    assert issues[0].severity == "error"
    assert issues[0].code == "SHOOTING_PERSPECTIVE"
    assert isinstance(issues[0], ValidationIssue)


def test_frame_common_sense_json_repair(monkeypatch):
    calls = {"n": 0}

    def fake_run_agent(agent, msg):
        calls["n"] += 1
        if calls["n"] == 1:
            return "该提示词违反常识，请修复"  # 非 JSON，无 { 可截取 → 走 repair
        return json.dumps({"issues": [{"severity": "error", "code": "X", "message": "m"}]})

    monkeypatch.setattr(frame_sanity, "run_agent", fake_run_agent)
    issues = frame_common_sense_issues({
        "first": [{"model_family": "zimage", "positive_prompt": "a person"}],
    }, _brief(), _agents())
    assert calls["n"] == 2  # 首次失败 → 修复一次
    assert len(issues) == 1
    assert issues[0].code == "X"


def test_prompt_common_sense_parses(monkeypatch):
    monkeypatch.setattr(frame_sanity, "run_agent", lambda agent, msg: json.dumps({
        "issues": [{"severity": "warning", "code": "PHYSICAL_IMPOSSIBLE", "message": "同一人物同时骑两辆车"}],
    }))
    issues = prompt_common_sense_issues("integrated_multimodal_description: [Shot 1] ...", _brief(), _agents())
    assert len(issues) == 1
    assert issues[0].severity == "warning"
    # 空提示词不审核
    assert prompt_common_sense_issues("", _brief(), _agents()) == []
    assert prompt_common_sense_issues("  ", _brief(), _agents()) == []


def test_stage1_frame_qa_loop_regenerates(monkeypatch):
    """阶段 1 生图 QA：首次发现 error → 注入 frame_prompt_issues 重生成 → 二次通过停止。"""
    config = SimpleNamespace(common_sense_qa=True, max_qa_iterations=2)
    brief = _brief()
    state = {"fl2va_prompt_bundle": {"scene_anchor": "beach", "first": [], "last": []}, "script": "s"}
    agents = _agents()
    calls = {"issues": 0}

    def fake_issues(bundle_dict, brief, agents):
        calls["issues"] += 1
        if calls["issues"] == 1:
            return [ValidationIssue("error", "SHOOTING_PERSPECTIVE", "被拍人物手持相机")]
        return []

    monkeypatch.setattr(frame_sanity, "frame_common_sense_issues", fake_issues)
    regenerated = {"fl2va_prompt_bundle": {"scene_anchor": "beach",
                                           "first": [{"model_family": "zimage", "positive_prompt": "fixed"}]}}
    fake_nodes = {"fl2va_frame_prompts": lambda s: regenerated}
    monkeypatch.setattr(nodes_mod, "make_nodes", lambda *a, **k: fake_nodes)

    out = pipeline._stage1_frame_qa_loop(state, brief, agents, model=None, config=config)

    assert calls["issues"] == 2
    assert out["fl2va_prompt_bundle"]["first"][0]["positive_prompt"] == "fixed"
    assert "frame_prompt_issues" not in out


def test_stage1_qa_loop_never_uses_a_revision_baseline(monkeypatch):
    """自动质检循环对**当前产物**重算（替换语义），不得被修订基线染色（issue #25 / ADR 0005）。

    真实链路里这个键到不了这里（人机循环每轮跑完就摘），但它是「人的累积」那条循环的
    入参：万一旧 state 或续接把它递进来，循环当场摘掉——替换语义逐字不变。
    """
    config = SimpleNamespace(common_sense_qa=True, max_qa_iterations=1)
    brief = _brief()
    state = {
        "brief": brief,  # 真实链路里 run_stage1 把 brief 放进 state，真节点要读它
        "fl2va_prompt_bundle": {"scene_anchor": "beach", "first": [], "last": []},
        "script": "s",
        nodes_mod.FRAME_BASELINE_KEY: render_baseline_block({
            "scene_anchor": "上一版的沙滩与礁石",
            "first": [{"model_family": "zimage", "positive_prompt": "首帧：沙滩上的女郎"}],
        }),
    }
    monkeypatch.setattr(frame_sanity, "frame_common_sense_issues",
                        lambda bundle, brief, agents: [
                            ValidationIssue("error", "SHOOTING_PERSPECTIVE", "被拍人物手持相机")])
    monkeypatch.setattr(pipeline, "stage_saver", SimpleNamespace(save=lambda *a, **k: None))
    captured = {}

    def fake_run_agent(agent, message):
        captured["message"] = message
        return json.dumps({
            "scene_anchor": "a beach at sunset",
            "first": {"zimage": {"positive_prompt": "A woman stands on a beach at sunset."}},
            "last": {"zimage": {"positive_prompt": "The same woman walks away along the beach."}},
            "continuity_constraints": ["Keep the same woman, clothing, and beach location."],
        })

    monkeypatch.setattr(nodes_mod, "run_agent", fake_run_agent)

    pipeline._stage1_frame_qa_loop(state, brief, {"frame_prompt_engineer": object()},
                                   model=None, config=config)

    assert "质检问题" in captured["message"], "循环没走重生成路径，这条断言就成了空转"
    assert "修订基线" not in captured["message"], "自动质检循环吃到了修订基线——替换语义被染色"
    assert "上一版的沙滩与礁石" not in captured["message"]


def test_stage2_qa_includes_common_sense(monkeypatch):
    """阶段 2 QA 循环纳入常识审核：常识 error 触发重生成路径。"""
    config = SimpleNamespace(common_sense_qa=True, max_qa_iterations=1)
    brief = _brief()

    class FakeGraph:
        def stream(self, *a, **k):
            return iter([])

    monkeypatch.setattr(pipeline, "build_pipeline_graph",
                        lambda *a, **k: (FakeGraph(), {"qa": object(), "prompt_engineer": object()}))
    monkeypatch.setattr(pipeline, "validate_prompt", lambda *a, **k: [])
    monkeypatch.setattr(pipeline, "theme_fidelity_issues", lambda *a, **k: [])
    calls = {"cs": 0}

    def fake_cs(prompt, brief, agents):
        calls["cs"] += 1
        return [ValidationIssue("error", "SHOOTING_PERSPECTIVE", "被拍人物手持相机")]

    monkeypatch.setattr(frame_sanity, "prompt_common_sense_issues", fake_cs)
    monkeypatch.setattr(pipeline, "run_agent", lambda agent, msg: "fixed prompt")
    monkeypatch.setattr(pipeline, "assemble_and_repair", lambda p, m, v: ("fixed prompt", "report"))
    monkeypatch.setattr(pipeline, "format_issues", lambda issues: "issues")
    monkeypatch.setattr(pipeline, "_theme_repair_injection", lambda s, b: "rewire")
    monkeypatch.setattr(pipeline, "stage_saver", SimpleNamespace(save=lambda *a, **k: None))

    merged, prompt = pipeline.run_stage2({"script": "s"}, brief, config, model=object())

    assert calls["cs"] >= 1
    assert prompt == "fixed prompt"
    assert merged["final_prompt"] == "fixed prompt"
