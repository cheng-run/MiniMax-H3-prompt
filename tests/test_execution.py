"""执行闭环编排层测试：操作卡 / 输出导入 / 验收 / 闸门 / Profile 升级。"""
import hashlib
import json
from pathlib import Path

import pytest

from minimax_h3_prompt import execution
from minimax_h3_prompt.execution import (
    apply_review,
    build_execution_card,
    check_executability,
    import_generation,
    promote_profile,
    render_card,
)
from minimax_h3_prompt.project_models import AssetRecord, AssetRegistry, Shot, ShotPlan
from minimax_h3_prompt.project_store import ProjectStore
from minimax_h3_prompt.task_package import AssetInput, TaskPackage
from minimax_h3_prompt.workflow_profiles import WorkflowOutput, WorkflowProfile, load_profile


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ui_workflow() -> dict:
    """一个可被 validate_profile 通过的极简 UI 工作流（187/133/168 + 114/177）。"""
    return {
        "last_node_id": 168,
        "last_link_id": 1,
        "nodes": [
            {"id": 187, "type": "PrimitiveStringMultiline",
             "inputs": [{"name": "value", "link": None}], "outputs": [{"name": "STRING", "links": []}], "mode": 0},
            {"id": 114, "type": "LoadImage",
             "inputs": [{"name": "image", "link": None}], "outputs": [{"name": "IMAGE", "links": []}], "mode": 0},
            {"id": 177, "type": "LoadImage",
             "inputs": [{"name": "image", "link": None}], "outputs": [{"name": "IMAGE", "links": []}], "mode": 0},
            {"id": 133, "type": "MiniMaxH3ImageToVideo",
             "inputs": [{"name": "prompt", "link": None}, {"name": "first_frame", "link": None},
                        {"name": "last_frame", "link": None}],
             "outputs": [{"name": "IMAGES", "links": [1]}], "mode": 0},
            {"id": 168, "type": "VHS_VideoCombine",
             "inputs": [{"name": "images", "link": 1}], "outputs": [{"name": "video", "links": []}], "mode": 0},
        ],
        "links": [[1, 133, 0, 168, 0, "IMAGES"]],
        "version": 0.4,
    }


def make_profile(workflow_sha256: str, **overrides) -> WorkflowProfile:
    data = {
        "profile_id": "h3_fl2va_v2",
        "display_name": "H3 FL2VA",
        "purpose": "test",
        "model_family": "MiniMax-H3",
        "input_mode": "fl2va",
        "workflow_path": "fl2va.json",
        "workflow_sha256": workflow_sha256,
        "profile_version": "2",
        "status": "candidate",
        "evidence_level": "runtime_pending",
        "workflow_format": "ui",
        "prompt_node_id": "187",
        "prompt_node_type": "PrimitiveStringMultiline",
        "prompt_input": "value",
        "generation_node_id": "133",
        "generation_node_type": "MiniMaxH3ImageToVideo",
        "slots": [
            {"slot_id": "first_frame", "node_id": "114", "input_name": "image", "semantic": "首帧", "required": False, "order": 1, "confirmed": False, "connection": "114 → 133.first_frame"},
            {"slot_id": "last_frame", "node_id": "177", "input_name": "image", "semantic": "尾帧", "required": False, "order": 2, "confirmed": False, "connection": "177 → 133.last_frame"},
        ],
        "outputs": [{"node_id": "168", "node_type": "VHS_VideoCombine", "output_type": "video", "prefix": "", "format": "video/h264-mp4"}],
        "manual_steps": ["将提示词输入到节点 187 的 value", "在节点 168 确认 MP4 输出"],
        "risks": ["尚未真实执行，不能标记为 verified 或 approved"],
    }
    data.update(overrides)
    return WorkflowProfile.from_dict(data)


def make_env(tmp_path, monkeypatch):
    """把 execution 的 profile 目录 / workflow 根指到临时目录，返回项目工厂所需上下文。"""
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir(parents=True, exist_ok=True)
    workflow_root = tmp_path / "workflows"
    workflow_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(execution, "PROFILES_DIR", profiles_dir)
    monkeypatch.setattr(execution, "WORKFLOW_ROOT", workflow_root)
    store = ProjectStore(tmp_path / "Assets")
    return store, profiles_dir, workflow_root


def make_project(store, profiles_dir, workflow_root):
    """构造一个带 SH001 镜头 + 任务包 + candidate Profile 的真实项目树。"""
    store.init_project("topic", "project", "Title")

    assets = tuple(AssetRecord(asset_id, "asset", asset_id) for asset_id in ("C01", "S01", "P01"))
    store.update_registry("topic", "project", AssetRegistry("topic", "project", assets), overwrite=True)

    wf_path = workflow_root / "fl2va.json"
    wf_path.write_text(json.dumps(ui_workflow()), encoding="utf-8")
    digest = _sha256(wf_path)

    profile = make_profile(digest)
    (profiles_dir / "h3_fl2va_v2.json").write_text(json.dumps(profile.to_dict(), ensure_ascii=False), encoding="utf-8")

    task = TaskPackage.from_profile(
        "SH001-G001", "video_clip", profile, "A cinematic rainy night shot.",
        project_id="project", topic_id="topic",
        inputs=(
            AssetInput("C01", "planned://C01", picture=1),
            AssetInput("S01", "planned://S01", picture=2),
            AssetInput("P01", "planned://P01", picture=3),
        ),
        expected_output_type="video", expected_width=1344, expected_height=768,
        expected_frames=56, expected_fps=24.0, output_prefix="rainy-night-001-SH001-G001",
    )
    task_dir = profiles_dir.parent / "tasks" / "SH001-G001"
    task.write(task_dir)

    shot = Shot(
        shot_id="SH001", shot_number=1, duration_seconds=5.0,
        start_state="start", action="action", end_state="end",
        subject_asset_ids=("C01",), input_asset_ids=("S01", "P01"),
        workflow_profile_id="h3_fl2va_v2", workflow_profile_version="2",
        workflow_sha256=digest, generation_id="SH001-G001",
        task_package_path=str(task_dir),
        continuity_constraints=("保持 C01 造型",),
    )
    document = store.load_project("topic", "project")
    plan = document.shot_plan
    updated_plan = ShotPlan(plan.shot_plan_id, plan.project_id, plan.variant, plan.duration_seconds, (shot,))
    store.update_shot_plan("topic", "project", updated_plan, overwrite=True)
    return {"workflow_sha256": digest, "task_dir": task_dir}


def test_build_execution_card_assembles_prompt_and_nodes(tmp_path, monkeypatch):
    store, profiles_dir, workflow_root = make_env(tmp_path, monkeypatch)
    ctx = make_project(store, profiles_dir, workflow_root)

    card = build_execution_card(store, "topic", "project", "SH001", workflow_root=workflow_root)

    assert card["shot_id"] == "SH001"
    assert card["generation_id"] == "SH001-G001"
    assert card["prompt"] == "A cinematic rainy night shot."
    assert "fl2va.json" in card["workflow_path"]
    assert card["workflow_sha256"] == ctx["workflow_sha256"]
    assert card["prompt_node"]["node_id"] == "187"
    assert card["prompt_node"]["input"] == "value"
    assert [slot["node_id"] for slot in card["input_slots"]] == ["114", "177"]
    assert card["generation_node"]["node_id"] == "133"
    assert card["output_node"]["node_id"] == "168"
    assert card["expected"]["width"] == 1344
    assert card["expected"]["fps"] == 24.0
    assert len(card["blockers"]) == 3  # C01/S01/P01 均为 planned:// 占位


def test_render_card_contains_steps_and_prompt(tmp_path, monkeypatch):
    store, profiles_dir, workflow_root = make_env(tmp_path, monkeypatch)
    make_project(store, profiles_dir, workflow_root)

    markdown = render_card(build_execution_card(store, "topic", "project", "SH001", workflow_root=workflow_root))

    assert "SH001" in markdown
    assert "节点 187" in markdown
    assert "A cinematic rainy night shot." in markdown
    assert "节点 168" in markdown
    assert "INPUT_PLACEHOLDER" in markdown


def test_import_generation_marks_executed_and_records_shot(tmp_path, monkeypatch):
    store, profiles_dir, workflow_root = make_env(tmp_path, monkeypatch)
    make_project(store, profiles_dir, workflow_root)

    source = tmp_path / "output" / "SH001-G001.mp4"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"fake-video-bytes")

    result = import_generation(store, "topic", "project", "SH001-G001", source)

    assert result["task_status"] == "executed"
    # ① task.json 已写回 executed
    assert TaskPackage.load(ctx_task_dir(store)).status == "executed"
    # ② 输出已复制到 inbox 并生成 source.json
    inbox = tmp_path / "Assets" / "inbox"
    copies = list(inbox.rglob("SH001-G001.mp4"))
    assert len(copies) == 1
    assert (copies[0].parent / "source.json").is_file()
    # ③ 镜头记录了 OUTPUT_IMPORTED
    document = store.load_project("topic", "project")
    shot = document.shot_plan.shots[0]
    assert any(rec.code == "OUTPUT_IMPORTED" for rec in shot.review_records)


def ctx_task_dir(store) -> Path:
    document = store.load_project("topic", "project")
    return Path(document.shot_plan.shots[0].task_package_path)


def test_import_generation_rejects_non_output_source(tmp_path, monkeypatch):
    store, profiles_dir, workflow_root = make_env(tmp_path, monkeypatch)
    make_project(store, profiles_dir, workflow_root)

    source = tmp_path / "elsewhere" / "clip.mp4"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"x")

    with pytest.raises(ValueError, match="output"):
        import_generation(store, "topic", "project", "SH001-G001", source)


def test_apply_review_planned_asset_to_approved(tmp_path, monkeypatch):
    store, profiles_dir, workflow_root = make_env(tmp_path, monkeypatch)
    make_project(store, profiles_dir, workflow_root)

    result = apply_review(store, "topic", "project", "C01", "visual", "approved", reviewer="me")

    assert result["status"] == "approved"
    document = store.load_project("topic", "project")
    asset = document.registry.get("C01")
    assert asset.status == "approved"
    assert any(rec.kind == "visual" and rec.outcome == "approved" for rec in asset.review_records)


def test_apply_review_shot_approves(tmp_path, monkeypatch):
    store, profiles_dir, workflow_root = make_env(tmp_path, monkeypatch)
    make_project(store, profiles_dir, workflow_root)

    result = apply_review(store, "topic", "project", "SH001", "visual", "approved", reviewer="me")

    assert result["entity_type"] == "shot"
    document = store.load_project("topic", "project")
    assert document.shot_plan.shots[0].status == "approved"


def test_apply_review_rejects_invalid_kind(tmp_path, monkeypatch):
    store, profiles_dir, workflow_root = make_env(tmp_path, monkeypatch)
    make_project(store, profiles_dir, workflow_root)

    with pytest.raises(ValueError, match="visual"):
        apply_review(store, "topic", "project", "C01", "static", "approved")


def test_check_executability_reports_gap(tmp_path, monkeypatch):
    store, profiles_dir, workflow_root = make_env(tmp_path, monkeypatch)
    make_project(store, profiles_dir, workflow_root)

    report = check_executability(store, "topic", "project", shot_id="SH001")

    assert report["shots"][0]["can_execute"] is False
    assert any(issue["code"] == "PROFILE_NOT_EXECUTABLE" for issue in report["shots"][0]["issues"])
    assert len(report["shots"][0]["input_gaps"]) == 3
    assert any(issue["severity"] == "warning" for issue in report["document_issues"])


def test_promote_profile_verified_requires_static_ok(tmp_path, monkeypatch):
    store, profiles_dir, workflow_root = make_env(tmp_path, monkeypatch)
    make_project(store, profiles_dir, workflow_root)

    result = promote_profile(store, "topic", "project", "h3_fl2va_v2", "verified")

    assert result["status"] == "verified"
    assert result["evidence_level"] == "static_verified"
    profile = load_profile(profiles_dir / "h3_fl2va_v2.json")
    assert profile.status == "verified"
    assert any(rec["type"] == "static_verified" for rec in profile.verification_records)


def test_promote_profile_verified_allows_known_mode_risk(tmp_path, monkeypatch):
    store, profiles_dir, workflow_root = make_env(tmp_path, monkeypatch)
    make_project(store, profiles_dir, workflow_root)

    # 把 114/177 改为 mode=4，模拟真实工作流的 bypass 分支（已知静态风险）
    wf_path = workflow_root / "fl2va.json"
    raw = json.loads(wf_path.read_text(encoding="utf-8"))
    for node in raw["nodes"]:
        if node["id"] in (114, 177):
            node["mode"] = 4
    wf_path.write_text(json.dumps(raw), encoding="utf-8")
    # 用新 sha 重写 profile，避免 WORKFLOW_HASH_MISMATCH 干扰
    digest = _sha256(wf_path)
    profile = make_profile(digest)
    (profiles_dir / "h3_fl2va_v2.json").write_text(json.dumps(profile.to_dict(), ensure_ascii=False), encoding="utf-8")

    result = promote_profile(store, "topic", "project", "h3_fl2va_v2", "verified")

    assert result["status"] == "verified"
    # 已知风险被记录但未阻塞升级
    assert any(
        "NODE_DISABLED_OR_SPECIAL_MODE" in str(record.get("errors"))
        for record in result["verification_records"]
    )


def test_promote_profile_approved_requires_evidence(tmp_path, monkeypatch):
    store, profiles_dir, workflow_root = make_env(tmp_path, monkeypatch)
    make_project(store, profiles_dir, workflow_root)

    with pytest.raises(ValueError, match="executed"):
        promote_profile(store, "topic", "project", "h3_fl2va_v2", "approved")


def test_promote_profile_approved_after_execution_and_review(tmp_path, monkeypatch):
    store, profiles_dir, workflow_root = make_env(tmp_path, monkeypatch)
    make_project(store, profiles_dir, workflow_root)

    # ① 人工执行回传：标记 executed + 导入输出
    source = tmp_path / "output" / "SH001-G001.mp4"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"fake-video-bytes")
    import_generation(store, "topic", "project", "SH001-G001", source)
    # ② 视觉验收镜头
    apply_review(store, "topic", "project", "SH001", "visual", "approved", reviewer="me")

    result = promote_profile(store, "topic", "project", "h3_fl2va_v2", "approved",
                             reviewer="me", note="首尾帧验证通过")

    assert result["status"] == "approved"
    assert result["evidence_level"] == "visual_approved"
    profile = load_profile(profiles_dir / "h3_fl2va_v2.json")
    assert profile.status == "approved"
    assert any(rec["type"] == "visual_approved" for rec in profile.verification_records)
