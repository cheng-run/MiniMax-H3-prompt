"""CLI 入口测试。"""
import json

import pytest

from minimax_h3_prompt.main import main


def test_project_cli_init_show_and_validate(tmp_path, capsys):
    root = tmp_path / "Assets"

    assert main([
        "project", "--root", str(root), "init",
        "--topic-id", "rainy-night",
        "--project-id", "project-001",
        "--title", "雨夜短片",
    ]) == 0
    init_output = json.loads(capsys.readouterr().out)
    assert init_output["project_id"] == "project-001"

    assert main([
        "project", "--root", str(root), "show",
        "--topic-id", "rainy-night",
        "--project-id", "project-001",
    ]) == 0
    show_output = json.loads(capsys.readouterr().out)
    assert show_output["shot_count"] == 0
    assert show_output["status"]["shot_plan"] == "draft"

    assert main([
        "project", "--root", str(root), "validate",
        "--topic-id", "rainy-night",
        "--project-id", "project-001",
    ]) == 0
    assert "SHOT_PLAN_NOT_LOCKED" in capsys.readouterr().out


def test_project_cli_validation_returns_error_for_malformed_document(tmp_path, capsys):
    root = tmp_path / "Assets"
    assert main([
        "project", "init", "--root", str(root),
        "--topic-id", "topic", "--project-id", "project", "--title", "Title",
    ]) == 0
    capsys.readouterr()
    project_dir = root / "topic" / "projects" / "project"
    (project_dir / "bible.json").write_text("[]", encoding="utf-8")

    assert main([
        "project", "--root", str(root), "validate",
        "--topic-id", "topic", "--project-id", "project",
    ]) == 1
    assert "PROJECT_DOCUMENT_INVALID" in capsys.readouterr().out
def test_project_cli_generate_and_show_prompts(tmp_path, monkeypatch, capsys):
    root = tmp_path / "Assets"
    brief_path = tmp_path / "brief.md"
    brief_path.write_text("## 剧情\n雨夜街角收到旧信。\n", encoding="utf-8")
    assert main([
        "project", "--root", str(root), "init", "--topic-id", "topic",
        "--project-id", "project", "--title", "Title",
    ]) == 0
    capsys.readouterr()

    from minimax_h3_prompt.generation import result_from_state
    from minimax_h3_prompt.graph import pipeline

    def fake_structured(brief, config, *, generation_id, topic_id, project_id, frame_descriptions=None):
        assert frame_descriptions == []
        return result_from_state({
            "script": "剧本", "final_prompt": "视频",
            "fl2va_prompt_bundle": {
                "scene_anchor": "rainy night street corner",
                "first": {
                    "zimage": {"positive_prompt": "A person stands at a rainy night street corner."},
                    "flux2": {"positive_prompt": "Flux.2 view of the same rainy night street corner."},
                },
                "last": {
                    "zimage": {"positive_prompt": "The same person lowers an envelope at the same rainy night street corner."},
                    "flux2": {"positive_prompt": "Flux.2 view of the same person lowering an envelope at the same street corner."},
                },
                "continuity_constraints": ["Keep the same person, envelope, street corner, rain, and lighting."],
            },
        }, brief, generation_id=generation_id, topic_id=topic_id, project_id=project_id)

    monkeypatch.setattr(pipeline, "run_pipeline_structured", fake_structured)
    assert main([
        "project", "--root", str(root), "generate-prompts", "--topic-id", "topic",
        "--project-id", "project", "--brief", str(brief_path), "--generation-id", "GEN001",
    ]) == 0
    generated = json.loads(capsys.readouterr().out)
    assert generated["generation_id"] == "GEN001"

    assert main([
        "project", "--root", str(root), "show-prompts", "--topic-id", "topic",
        "--project-id", "project", "--generation", "GEN001", "--kind", "first-frame", "--raw",
    ]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert "rainy night street corner" in shown["artifacts"]["first-frame"]


def test_project_cli_generate_dry_run_does_not_call_pipeline(tmp_path, monkeypatch, capsys):
    root = tmp_path / "Assets"
    brief_path = tmp_path / "brief.md"
    brief_path.write_text("## 剧情\n主题。\n", encoding="utf-8")
    assert main([
        "project", "--root", str(root), "init", "--topic-id", "topic",
        "--project-id", "project", "--title", "Title",
    ]) == 0
    capsys.readouterr()
    from minimax_h3_prompt.graph import pipeline
    monkeypatch.setattr(pipeline, "run_pipeline_structured", lambda *a, **k: pytest.fail("dry-run 不应调用管线"))
    assert main([
        "project", "--root", str(root), "generate-prompts", "--topic-id", "topic",
        "--project-id", "project", "--brief", str(brief_path), "--dry-run",
    ]) == 0
    assert json.loads(capsys.readouterr().out)["dry_run"] is True


def test_cli_brief_respects_l2va(tmp_path, capsys):
    """brief 声明尾帧 → dry-run 显示变体 L2VA（不再被硬编码 FL2VA 覆盖）。"""
    brief_path = tmp_path / "brief_l2va.md"
    brief_path.write_text("输入: 尾帧\n\n## 剧情\n主题。\n", encoding="utf-8")
    assert main(["--brief", str(brief_path), "--dry-run"]) == 0
    assert "变体: L2VA" in capsys.readouterr().out


def test_cli_brief_defaults_fl2va(tmp_path, capsys):
    """brief 无变体声明 → 缺省 FL2VA。"""
    brief_path = tmp_path / "brief.md"
    brief_path.write_text("## 剧情\n主题。\n", encoding="utf-8")
    assert main(["--brief", str(brief_path), "--dry-run"]) == 0
    assert "变体: FL2VA" in capsys.readouterr().out


def _patch_execution(monkeypatch):
    """把 execution 新命令函数替换为记录调用的桩，验证 CLI 分发与退出码。"""
    import minimax_h3_prompt.execution as ex

    calls: dict = {"args": None}

    def fake_plan(store, topic, project, shot, *, workflow_root=None):
        calls["args"] = ("plan", topic, project, shot)
        return {
            "shot_id": shot, "prompt": "P", "generation_id": "SH001-G001",
            "prompt_node": {"node_id": "187", "input": "value"}, "input_slots": [],
            "output_node": {"node_id": "168", "node_type": "VHS_VideoCombine"}, "expected": {},
            "workflow_path": "wf", "workflow_sha256": "h",
            "profile": {"id": "p", "status": "candidate", "evidence_level": "runtime_pending"},
            "manual_steps": [], "notes": [], "blockers": [],
            "topic_id": topic, "project_id": project, "output_prefix": "",
        }

    def fake_render(card):
        return f"# {card['shot_id']} 指引"

    def fake_import_generation(store, topic, project, generation, source, *, inbox_root=None):
        calls["args"] = ("import-output", topic, project, generation, str(source))
        return {"generation_id": generation, "shot_id": "SH001", "task_status": "executed", "asset": {}}

    def fake_review(store, topic, project, entity, kind, outcome, *, reviewer=""):
        calls["args"] = ("review", topic, project, entity, kind, outcome, reviewer)
        return {"entity_type": "asset", "entity_id": entity, "status": "approved"}

    def fake_check(store, topic, project, *, shot_id=None, workflow_root=None):
        calls["args"] = ("verify", topic, project, shot_id)
        return {
            "topic_id": topic, "project_id": project, "document_issues": [],
            "shots": [{"shot_id": "SH001", "can_execute": False, "issues": [], "input_gaps": []}],
        }

    def fake_promote(store, topic, project, profile, status, *, reviewer="", note="", generation_id=""):
        calls["args"] = ("profile", topic, project, profile, status, generation_id)
        return {"profile_id": profile, "status": status, "evidence_level": "static_verified", "verification_records": []}

    monkeypatch.setattr(ex, "build_execution_card", fake_plan)
    monkeypatch.setattr(ex, "render_card", fake_render)
    monkeypatch.setattr(ex, "import_generation", fake_import_generation)
    monkeypatch.setattr(ex, "apply_review", fake_review)
    monkeypatch.setattr(ex, "check_executability", fake_check)
    monkeypatch.setattr(ex, "promote_profile", fake_promote)
    return calls


def test_project_cli_new_commands_dispatch(tmp_path, monkeypatch, capsys):
    calls = _patch_execution(monkeypatch)
    root = str(tmp_path / "Assets")

    # plan：操作卡打印到 stdout，退出码 0
    assert main([
        "project", "--root", root, "plan",
        "--topic-id", "topic", "--project-id", "project", "--shot", "SH001",
        "--workflow-root", str(tmp_path),
    ]) == 0
    assert "# SH001 指引" in capsys.readouterr().out
    assert calls["args"] == ("plan", "topic", "project", "SH001")

    # import-output：位置参数 generation + source
    assert main([
        "project", "--root", root, "import-output",
        "--topic-id", "topic", "--project-id", "project",
        "SH001-G001", "D:/x/output/clip.mp4",
    ]) == 0
    assert calls["args"] == ("import-output", "topic", "project", "SH001-G001", "D:/x/output/clip.mp4")

    # review：参数原样传入
    assert main([
        "project", "--root", root, "review",
        "--topic-id", "topic", "--project-id", "project",
        "--entity", "C01", "--kind", "visual", "--outcome", "approved", "--reviewer", "me",
    ]) == 0
    assert calls["args"] == ("review", "topic", "project", "C01", "visual", "approved", "me")

    # verify：can_execute=False → 退出码 1
    assert main([
        "project", "--root", root, "verify",
        "--topic-id", "topic", "--project-id", "project", "--shot", "SH001",
    ]) == 1
    assert calls["args"] == ("verify", "topic", "project", "SH001")

    # profile：升级状态
    assert main([
        "project", "--root", root, "profile",
        "--topic-id", "topic", "--project-id", "project",
        "--profile", "h3_fl2va_v2", "--status", "verified",
    ]) == 0
    assert calls["args"] == ("profile", "topic", "project", "h3_fl2va_v2", "verified", "")
