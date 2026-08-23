"""Workflow Profile 与任务包协议测试。"""
from pathlib import Path

import pytest

from minimax_h3_prompt.task_package import (
    AssetInput,
    TaskPackage,
    import_output,
    inspect_asset,
    resolve_input_path,
)
from minimax_h3_prompt.workflow_profiles import (
    WorkflowOutput,
    WorkflowProfile,
    WorkflowSlot,
    inspect_workflow,
    validate_profile,
)


def profile(**kwargs):
    return WorkflowProfile(
        profile_id="demo",
        display_name="Demo",
        purpose="test",
        model_family="test",
        input_mode="t2i",
        workflow_path="demo.json",
        **kwargs,
    )


def test_profile_roundtrip_and_status_guard():
    p = profile(
        slots=(WorkflowSlot("image", "1", "image", required=True),),
        outputs=(WorkflowOutput("2", "SaveImage", "image"),),
    )
    assert WorkflowProfile.from_dict(p.to_dict()) == p
    with pytest.raises(ValueError, match="visual_approved"):
        p.with_status("approved")
    verified = p.with_status("verified", "static_verified")
    assert verified.status == "verified"


def test_record_verification_appends_and_guards():
    p = profile()

    recorded = p.record_verification({"type": "static_verified", "errors": []})
    assert len(recorded.verification_records) == 1
    assert recorded.status == "candidate"  # 只追加记录，不升级状态

    upgraded = p.record_verification(
        {"type": "static_verified", "errors": []}, status="verified", evidence_level="static_verified"
    )
    assert upgraded.status == "verified"
    assert upgraded.evidence_level == "static_verified"
    assert len(upgraded.verification_records) == 1

    with pytest.raises(ValueError, match="visual_approved"):
        p.record_verification({"type": "x"}, status="approved")


def test_inspect_ui_workflow_and_validate(tmp_path):
    path = tmp_path / "workflow.json"
    path.write_text(
        '{"nodes":[{"id":1,"type":"CLIPTextEncode","inputs":[{"name":"text","link":null}],"outputs":[{"name":"CONDITIONING","links":[1]}]},'
        '{"id":2,"type":"SaveImage","inputs":[{"name":"images","link":1}],"outputs":[]}],'
        '"links":[[1,1,0,2,0,"IMAGE"]]}',
        encoding="utf-8",
    )
    inspection = inspect_workflow(path)
    assert inspection.workflow_format == "ui"
    p = profile(
        workflow_sha256=inspection.sha256,
        workflow_format="ui",
        prompt_node_id="1",
        prompt_node_type="CLIPTextEncode",
        prompt_input="text",
        outputs=(WorkflowOutput("2", "SaveImage", "image"),),
    )
    assert validate_profile(p, inspection) == []


def test_validate_ui_links_and_reachability(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text(
        '{"nodes":[{"id":1,"type":"Source","inputs":[],"outputs":[{"name":"IMAGE","links":[9]}]},'
        '{"id":2,"type":"SaveImage","inputs":[{"name":"images","link":9}],"outputs":[]}],'
        '"links":[[9,1,1,2,0,"IMAGE"],[10,99,0,2,0,"IMAGE"]]}',
        encoding="utf-8",
    )
    inspection = inspect_workflow(path)
    assert any("LINK_SOURCE_MISSING" in error for error in validate_profile(profile(workflow_format="ui"), inspection))


def test_inspection_collects_dependencies_and_disabled_nodes(tmp_path):
    path = tmp_path / "deps.json"
    path.write_text(
        '{"nodes":[{"id":1,"type":"CustomSampler","mode":2,"inputs":[],"outputs":[]}],'
        '"definitions":{"subgraphs":[{"id":"sg1"}]},'
        '"extra":{"model_name":"demo.safetensors"}}',
        encoding="utf-8",
    )
    inspection = inspect_workflow(path)
    assert inspection.subgraphs == ("sg1",)
    assert inspection.model_references == ("demo.safetensors",)
    assert inspection.custom_node_types == ("CustomSampler",)
    assert inspection.disabled_nodes == ("1",)
    errors = validate_profile(profile(workflow_format="ui"), inspection, strict_dependencies=True)
    assert any("SUBGRAPH_PRESENT" in error for error in errors)
    assert any("MODEL_DEPENDENCIES_DECLARED" in error for error in errors)
    assert any("CUSTOM_NODES_DECLARED" in error for error in errors)


    path = tmp_path / "api.json"
    path.write_text(
        '{"1":{"class_type":"Source","inputs":{}},'
        '"2":{"class_type":"SaveVideo","inputs":{"video":["1",0]}}}',
        encoding="utf-8",
    )
    inspection = inspect_workflow(path)
    assert inspection.link_edges() == (("1", "2"),)

    path = tmp_path / "api.json"
    path.write_text('{"1":{"class_type":"CLIPTextEncode","inputs":{"text":"${prompt}"}}}', encoding="utf-8")
    inspection = inspect_workflow(path)
    assert inspection.workflow_format == "api"
    assert inspection.node("1").inputs == ("text",)


def test_profile_path_binding(tmp_path):
    path = tmp_path / "workflow.json"
    path.write_text('{"1":{"class_type":"SaveVideo","inputs":{}}}', encoding="utf-8")
    inspection = inspect_workflow(path)
    p = profile(
        workflow_format="api",
        workflow_sha256=inspection.sha256,
        absolute_workflow_path=str(tmp_path / "other.json"),
    )
    assert any("WORKFLOW_PATH_MISMATCH" in error for error in validate_profile(p, inspection))


    p = profile(workflow_sha256="a" * 64)
    task = TaskPackage.from_profile(
        "G001", "image", p, "一张测试图",
        inputs=(AssetInput("C01-v001", "D:/input.png", "image"),),
    )
    path = task.write(tmp_path)
    data = __import__("json").loads(path.read_text(encoding="utf-8"))
    assert data["generation_id"] == "G001"
    assert len(data["prompt_sha256"]) == 64
    assert (tmp_path / "prompt.md").read_text(encoding="utf-8") == "一张测试图"


def test_resolve_mnt_path_on_windows():
    resolved = resolve_input_path("/mnt/d/Comfyui/output/a.png")
    assert str(resolved).lower().startswith("d:\\")


def test_import_output_copies_and_records_source(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    source = output / "result.txt"
    source.write_text("result", encoding="utf-8")
    p = profile(workflow_sha256="b" * 64)
    task = TaskPackage.from_profile("G001", "video", p, "prompt")
    inbox = tmp_path / "inbox"
    imported = import_output(source, task, inbox)
    assert Path(imported.target_path).read_text(encoding="utf-8") == "result"
    assert (inbox / "source.json").exists()
    assert imported.sha256 == inspect_asset(source)["sha256"]


def test_import_rejects_unassociated_output(tmp_path):
    source = tmp_path / "result.txt"
    source.write_text("result", encoding="utf-8")
    p = profile(workflow_sha256="b" * 64)
    task = TaskPackage.from_profile("G001", "video", p, "prompt")
    with pytest.raises(ValueError, match="output"):
        import_output(source, task, tmp_path / "inbox")


PROFILE_DIR = Path(__file__).parents[1] / "docs" / "profiles"
PROFILE_IDS = (
    "zimage_t2i_v1", "zimage_i2i_v1", "zimage_control_image_v1",
    "flux2_t2i_v1", "flux2_single_edit_v1", "flux2_dual_edit_v1",
    "h3_fl2va_v2", "h3_ref2va_v2", "h3_ref2va_upscale_v1",
)


# 已通过真实 ComfyUI 运行与静态核验升级的 Profile；其余首批 Profile 仍保持 candidate/runtime_pending
VERIFIED_PROFILE_IDS = {"h3_fl2va_v2"}


@pytest.mark.parametrize("profile_id", PROFILE_IDS)
def test_first_batch_profiles_are_readable_and_candidate(profile_id):
    from minimax_h3_prompt.workflow_profiles import load_profile

    loaded = load_profile(PROFILE_DIR / f"{profile_id}.json")
    assert loaded.profile_id == profile_id
    assert loaded.workflow_format == "ui"
    assert loaded.workflow_sha256 and len(loaded.workflow_sha256) == 64
    if profile_id in VERIFIED_PROFILE_IDS:
        assert loaded.status == "verified"
        assert loaded.evidence_level == "static_verified"
        assert loaded.verification_records
    else:
        assert loaded.status == "candidate"
        assert loaded.evidence_level == "runtime_pending"
    with pytest.raises(ValueError, match="visual_approved"):
        loaded.with_status("approved")


@pytest.mark.parametrize("profile_id", PROFILE_IDS)
def test_first_batch_profiles_match_real_workflow_contract(profile_id):
    from minimax_h3_prompt.workflow_profiles import load_profile

    loaded = load_profile(PROFILE_DIR / f"{profile_id}.json")
    workflow_root = Path(r"D:\Comfyui\ComfyUI\user\default\workflows")
    inspection = inspect_workflow(workflow_root / loaded.workflow_path.replace("/", chr(92)))
    errors = validate_profile(loaded, inspection)
    known_risk_codes = {
        "NODE_DISABLED_OR_SPECIAL_MODE",
        "LINK_TARGET_MISSING",
        "OUTPUT_LINK_MISSING",
    }
    unexpected = [error for error in errors if not any(code in error for code in known_risk_codes)]
    assert unexpected == []
    if errors:
        assert all(any(code in error for code in known_risk_codes) for error in errors)
