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


def test_inspect_ui_workflow_and_validate(tmp_path):
    path = tmp_path / "workflow.json"
    path.write_text(
        '{"nodes":[{"id":1,"type":"CLIPTextEncode","inputs":[{"name":"text"}],"outputs":[]},'
        '{"id":2,"type":"SaveImage","inputs":[],"outputs":[]}],"links":[]}',
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


def test_inspect_api_workflow(tmp_path):
    path = tmp_path / "api.json"
    path.write_text('{"1":{"class_type":"CLIPTextEncode","inputs":{"text":"${prompt}"}}}', encoding="utf-8")
    inspection = inspect_workflow(path)
    assert inspection.workflow_format == "api"
    assert inspection.node("1").inputs == ("text",)


def test_task_package_roundtrip_and_prompt_hash(tmp_path):
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
