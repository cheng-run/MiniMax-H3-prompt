"""Assets 主题目录与资产包协议测试。"""
import json

import pytest

from minimax_h3_prompt.asset_store import AssetStore, create_topic_template
from minimax_h3_prompt.project_models import AssetRecord, prompt_sha256
from minimax_h3_prompt.task_package import TaskPackage
from minimax_h3_prompt.workflow_profiles import WorkflowProfile


def profile() -> WorkflowProfile:
    return WorkflowProfile(
        profile_id="zimage_t2i_v1",
        display_name="Z-Image T2I",
        purpose="test",
        model_family="z-image",
        input_mode="t2i",
        workflow_path="workflow.json",
        workflow_sha256="a" * 64,
        profile_version="1",
        status="candidate",
        evidence_level="runtime_pending",
    )


def task_and_asset(tmp_path):
    prompt = "一位穿红衣的角色站在雨夜街道"
    task = TaskPackage.from_profile(
        "C01-G001",
        "character",
        profile(),
        prompt,
        project_id="project-001",
        topic_id="rainy-night",
    )
    asset = AssetRecord(
        asset_id="C01",
        asset_type="character",
        name="主角",
        version="C01-v001",
        generation_id="C01-G001",
        prompt_sha256=prompt_sha256(prompt),
        workflow_profile_id="zimage_t2i_v1",
        workflow_profile_version="1",
        workflow_sha256="a" * 64,
        status="candidate",
    )
    return task, asset


def test_create_template_and_topic_isolation(tmp_path):
    root = create_topic_template(tmp_path / "Assets")
    assert (root / "README.md").exists()
    assert (root / "_template" / "schemas").is_dir()

    store = AssetStore(root)
    topic = store.ensure_topic("rainy-night", display_name="雨夜主题")
    assert topic.name == "rainy-night"
    assert all((topic / name).is_dir() for name in ("projects", "shared", "catalog", "schemas"))
    assert (root / "inbox").is_dir()
    assert not (topic / "inbox").exists()
    assert not (root / "_template" / "catalog" / "entries.jsonl").exists()

    other = store.ensure_topic("forest-story")
    assert other != topic
    assert not (topic / "forest-story").exists()


def test_rejects_invalid_ids_and_path_traversal(tmp_path):
    store = AssetStore(tmp_path / "Assets")
    with pytest.raises(ValueError):
        store.topic_root("../outside")
    with pytest.raises(ValueError):
        store.plan_bundle("topic", "project", "C01-v001", "C01-G001", "C01-v001")
    with pytest.raises(ValueError):
        store.plan_bundle("topic", "project", "C01", "C01-G001", "S01-v001")
    with pytest.raises(ValueError):
        store.plan_bundle("topic", "project", "C01", "G001", "C01-v001")


def test_write_bundle_roundtrip_and_catalog(tmp_path):
    task, asset = task_and_asset(tmp_path)
    store = AssetStore(tmp_path / "Assets")
    plan = store.write_bundle(task, asset, version="C01-v001")

    assert plan.directory == tmp_path / "Assets" / "inbox" / "rainy-night" / "C01" / "C01-G001" / "C01-v001"
    task_data = json.loads((plan.directory / "task.json").read_text(encoding="utf-8"))
    asset_data = json.loads((plan.directory / "asset.json").read_text(encoding="utf-8"))
    catalog = (tmp_path / "Assets" / "rainy-night" / "catalog" / "entries.jsonl").read_text(encoding="utf-8")
    assert task_data["generation_id"] == "C01-G001"
    assert asset_data["version"] == "C01-v001"
    assert '"asset_id": "C01"' in catalog
    assert '"status": "candidate"' in catalog
    assert "api_key" not in (plan.directory / "asset.json").read_text(encoding="utf-8")


def test_dry_run_does_not_create_bundle(tmp_path):
    task, asset = task_and_asset(tmp_path)
    store = AssetStore(tmp_path / "Assets")
    plan = store.write_bundle(task, asset, version="C01-v001", dry_run=True)
    assert plan.directory.is_absolute()
    assert not plan.directory.exists()
    assert not (tmp_path / "Assets").exists()


def test_duplicate_bundle_does_not_overwrite(tmp_path):
    task, asset = task_and_asset(tmp_path)
    store = AssetStore(tmp_path / "Assets")
    store.write_bundle(task, asset, version="C01-v001")
    with pytest.raises(FileExistsError):
        store.write_bundle(task, asset, version="C01-v001")


def test_task_asset_traceability_is_required(tmp_path):
    task, asset = task_and_asset(tmp_path)
    store = AssetStore(tmp_path / "Assets")
    bad = AssetRecord(
        asset_id="C01",
        asset_type="character",
        name="主角",
        version="C01-v001",
        generation_id="C01-G001",
        prompt_sha256="b" * 64,
        workflow_profile_id="zimage_t2i_v1",
        workflow_profile_version="1",
        workflow_sha256="a" * 64,
        status="candidate",
    )
    with pytest.raises(ValueError, match="prompt_sha256"):
        store.write_bundle(task, bad, version="C01-v001")
