"""项目文档存储协议测试。"""
import json

import pytest

from minimax_h3_prompt.project_store import ProjectStore


def test_init_load_roundtrip_and_show(tmp_path):
    store = ProjectStore(tmp_path / "Assets")
    document = store.init_project(
        "rainy-night", "project-001", "雨夜短片",
        global_style="cinematic live action",
    )

    loaded = store.load_project("rainy-night", "project-001")
    assert loaded.bible == document.bible
    assert loaded.registry == document.registry
    assert loaded.shot_plan == document.shot_plan
    assert store.show("rainy-night", "project-001")["shot_count"] == 0
    assert (document.directory / "README.md").exists()
    assert "api_key" not in json.dumps(store.show("rainy-night", "project-001"))


def test_init_is_topic_isolated_and_does_not_overwrite(tmp_path):
    store = ProjectStore(tmp_path / "Assets")
    first = store.init_project("topic-a", "project", "A")
    second = store.init_project("topic-b", "project", "B")
    assert first.directory != second.directory
    with pytest.raises(FileExistsError):
        store.init_project("topic-a", "project", "changed")
    assert store.load_project("topic-a", "project").bible.title == "A"


def test_project_ids_and_duration_are_validated(tmp_path):
    store = ProjectStore(tmp_path / "Assets")
    with pytest.raises(ValueError):
        store.init_project("topic", "../outside", "Title")
    with pytest.raises(ValueError, match="时长"):
        store.init_project("topic", "project", "Title", duration_seconds=0)
    with pytest.raises(ValueError, match="标题"):
        store.init_project("topic", "project-2", " ")


def test_validate_reports_references_and_review_gates(tmp_path):
    store = ProjectStore(tmp_path / "Assets")
    document = store.init_project("topic", "project", "Title")
    bible = document.bible.to_dict()
    bible["asset_ids"] = ["C01"]
    (document.directory / "bible.json").write_text(
        json.dumps(bible, ensure_ascii=False), encoding="utf-8"
    )

    issues = store.validate("topic", "project")
    codes = {issue.code for issue in issues}
    assert "ASSET_MISSING" in codes
    assert "SHOT_PLAN_NOT_LOCKED" in codes


def test_validate_handles_malformed_documents(tmp_path):
    store = ProjectStore(tmp_path / "Assets")
    document = store.init_project("topic", "project", "Title")
    (document.directory / "bible.json").write_text("[]", encoding="utf-8")
    issues = store.validate("topic", "project")
    assert issues[0].code == "PROJECT_DOCUMENT_INVALID"


def test_load_rejects_directory_identity_mismatch(tmp_path):
    store = ProjectStore(tmp_path / "Assets")
    document = store.init_project("topic", "project", "Title")
    raw = json.loads((document.directory / "bible.json").read_text(encoding="utf-8"))
    raw["project_id"] = "other"
    (document.directory / "bible.json").write_text(
        json.dumps(raw, ensure_ascii=False), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="目录不一致"):
        store.load_project("topic", "project")
