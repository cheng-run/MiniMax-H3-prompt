"""剧本与四类提示词生成结果测试。"""
import json

import pytest

from minimax_h3_prompt.generation import GenerationResult, PromptArtifact, result_from_state
from minimax_h3_prompt.brief_parser import Brief
from minimax_h3_prompt.project_store import ProjectStore


def make_result(generation_id="GEN001"):
    brief = Brief(plot="雨夜街角", duration=5, style="cinematic", language="Chinese")
    return result_from_state(
        {
            "script": "第一场：雨夜街角。",
            "final_prompt": "video prompt",
            "character_design": "character prompt",
            "prop_design": "prop prompt",
            "background_design": "scene prompt",
        },
        brief,
        generation_id=generation_id,
        topic_id="topic",
        project_id="project",
    )


def test_generation_result_maps_four_prompt_kinds_and_roundtrips():
    result = make_result()
    restored = GenerationResult.from_dict(result.to_dict())
    assert restored == result
    assert result.script == "第一场：雨夜街角。"
    assert {item.kind for item in result.artifacts} == {"video", "character", "prop", "scene"}
    assert result.artifact("video").content == "video prompt"


def test_generation_result_rejects_missing_output_or_bad_hash():
    with pytest.raises(ValueError, match="缺少"):
        result_from_state(
            {"script": "剧本", "final_prompt": "视频"},
            Brief(plot="主题"), generation_id="GEN001", topic_id="topic", project_id="project",
        )
    artifact = PromptArtifact("a", "video", "正文", "final_prompt")
    with pytest.raises(ValueError, match="sha256"):
        PromptArtifact("a", "video", "正文", "final_prompt", sha256="0" * 64)
    assert len(artifact.sha256) == 64


def test_project_store_persists_generation_and_is_backward_compatible(tmp_path):
    store = ProjectStore(tmp_path / "Assets")
    store.init_project("topic", "project", "Title")
    result = make_result()
    directory = store.save_generation_result("topic", "project", result)
    assert (directory / "script.md").read_text(encoding="utf-8").strip() == result.script
    assert store.load_generation_result("topic", "project", "GEN001") == result
    listed = store.list_generation_results("topic", "project")
    assert listed[0]["generation_id"] == "GEN001"
    shown = store.show_generation("topic", "project", "GEN001", kind="video", raw=True)
    assert shown["artifacts"] == {"video": "video prompt"}
    assert "api_key" not in json.dumps(result.to_dict())


def test_project_store_rejects_changed_generation_without_overwrite(tmp_path):
    store = ProjectStore(tmp_path / "Assets")
    store.init_project("topic", "project", "Title")
    store.save_generation_result("topic", "project", make_result())
    changed = make_result()
    changed = GenerationResult(
        **{**changed.__dict__, "script": "不同剧本"}
    )
    with pytest.raises(FileExistsError):
        store.save_generation_result("topic", "project", changed)
