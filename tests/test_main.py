"""CLI 入口测试。"""
import json

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
