"""ProjectContext 与严格 TaskPackage 加载测试。"""
import json

import pytest

from minimax_h3_prompt.project_context import ProjectContext
from minimax_h3_prompt.project_models import AssetRecord, AssetRegistry
from minimax_h3_prompt.project_store import ProjectStore
from minimax_h3_prompt.task_package import AssetInput, TaskPackage
from minimax_h3_prompt.workflow_profiles import WorkflowProfile


def profile(**overrides) -> WorkflowProfile:
    data = {
        "profile_id": "demo",
        "display_name": "Demo",
        "purpose": "test",
        "model_family": "test",
        "input_mode": "t2i",
        "workflow_path": "workflow.json",
        "workflow_sha256": "a" * 64,
        "profile_version": "1",
        "status": "candidate",
        "evidence_level": "runtime_pending",
    }
    data.update(overrides)
    return WorkflowProfile(**data)


def make_project(tmp_path, *, asset_ids=("C01",)):
    store = ProjectStore(tmp_path / "Assets")
    document = store.init_project("topic", "project", "Title")
    assets = tuple(AssetRecord(asset_id, "character", asset_id) for asset_id in asset_ids)
    registry = AssetRegistry("topic", "project", assets)
    return store, store.update_registry("topic", "project", registry, overwrite=True)


def make_task(tmp_path, p=None, *, topic_id="topic", project_id="project", asset_id="C01"):
    p = p or profile()
    task = TaskPackage.from_profile(
        "C01-G001",
        "character",
        p,
        "角色站在雨夜街道",
        project_id=project_id,
        topic_id=topic_id,
        inputs=(AssetInput(asset_id, "asset.png"),),
    )
    task_dir = tmp_path / "task"
    task.write(task_dir)
    return task, task_dir


def write_profile(tmp_path, p=None):
    path = tmp_path / "profile.json"
    path.write_text(json.dumps((p or profile()).to_dict(), ensure_ascii=False), encoding="utf-8")
    return path


def test_task_package_strict_roundtrip_and_directory_load(tmp_path):
    task, task_dir = make_task(tmp_path)

    restored = TaskPackage.load(task_dir)

    assert restored == task
    assert TaskPackage.load(task_dir / "task.json") == task


def test_task_package_with_status_roundtrip(tmp_path):
    task, task_dir = make_task(tmp_path)

    executed = task.with_status("executed")
    assert executed.status == "executed"
    assert executed.generation_id == task.generation_id
    # 状态变更后写回可重新加载
    executed.write(task_dir)
    assert TaskPackage.load(task_dir).status == "executed"


def test_task_package_rejects_prompt_hash_mismatch(tmp_path):
    _, task_dir = make_task(tmp_path)
    path = task_dir / "task.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["prompt_sha256"] = "b" * 64
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValueError, match="prompt_sha256"):
        TaskPackage.load(task_dir)


def test_task_package_rejects_prompt_file_mismatch(tmp_path):
    _, task_dir = make_task(tmp_path)
    (task_dir / "prompt.md").write_text("另一个提示词", encoding="utf-8")

    with pytest.raises(ValueError, match="prompt.md"):
        TaskPackage.load(task_dir)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("generation_id", "invalid", "generation_id"),
        ("workflow_sha256", "invalid", "workflow_sha256"),
        ("schema_version", "2", "schema_version"),
    ],
)
def test_task_package_rejects_invalid_schema_and_ids(tmp_path, field, value, message):
    _, task_dir = make_task(tmp_path)
    path = task_dir / "task.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw[field] = value
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        TaskPackage.load(task_dir)


def test_project_context_loads_and_keeps_candidate_profile_planning_only(tmp_path):
    store, _ = make_project(tmp_path)
    p = profile()
    profile_path = write_profile(tmp_path, p)
    task, task_dir = make_task(tmp_path, p)

    context = ProjectContext.load(
        store=store,
        topic_id="topic",
        project_id="project",
        profile_path=profile_path,
        task_package_path=task_dir,
    )

    assert context.project.project_id == "project"
    assert context.profile.profile_id == "demo"
    assert context.task_package.generation_id == task.generation_id
    assert context.can_execute is False
    assert any(issue.code == "PROFILE_NOT_EXECUTABLE" for issue in context.validate())


@pytest.mark.parametrize(
    ("task_kwargs", "message"),
    [
        ({"topic_id": "other"}, "TASK_TOPIC_MISMATCH"),
        ({"project_id": "other"}, "TASK_PROJECT_MISMATCH"),
    ],
)
def test_project_context_rejects_project_identity_mismatch(tmp_path, task_kwargs, message):
    store, _ = make_project(tmp_path)
    p = profile()
    profile_path = write_profile(tmp_path, p)
    _, task_dir = make_task(tmp_path, p, **task_kwargs)

    with pytest.raises(ValueError, match=message):
        ProjectContext.load(
            store=store,
            topic_id="topic",
            project_id="project",
            profile_path=profile_path,
            task_package_path=task_dir,
        )


@pytest.mark.parametrize(
    ("profile_kwargs", "message"),
    [
        ({"profile_id": "other"}, "PROFILE_ID_MISMATCH"),
        ({"profile_version": "2"}, "PROFILE_VERSION_MISMATCH"),
        ({"workflow_sha256": "b" * 64}, "WORKFLOW_HASH_MISMATCH"),
        ({"workflow_path": "other.json"}, "WORKFLOW_PATH_MISMATCH"),
    ],
)
def test_project_context_rejects_profile_mismatch(tmp_path, profile_kwargs, message):
    store, _ = make_project(tmp_path)
    task_profile = profile()
    profile_path = write_profile(tmp_path, profile(**profile_kwargs))
    _, task_dir = make_task(tmp_path, task_profile)

    with pytest.raises(ValueError, match=message):
        ProjectContext.load(
            store=store,
            topic_id="topic",
            project_id="project",
            profile_path=profile_path,
            task_package_path=task_dir,
        )


def test_project_context_rejects_missing_input_asset(tmp_path):
    store, _ = make_project(tmp_path, asset_ids=())
    p = profile()
    profile_path = write_profile(tmp_path, p)
    _, task_dir = make_task(tmp_path, p)

    with pytest.raises(ValueError, match="INPUT_ASSET_MISSING"):
        ProjectContext.load(
            store=store,
            topic_id="topic",
            project_id="project",
            profile_path=profile_path,
            task_package_path=task_dir,
        )
