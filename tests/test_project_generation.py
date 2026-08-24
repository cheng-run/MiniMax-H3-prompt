"""面向最终用户的主题单入口测试。"""
import json

from minimax_h3_prompt.main import main


def test_create_video_cli_automatically_initializes_and_persists(tmp_path, monkeypatch, capsys):
    from minimax_h3_prompt.generation import result_from_state
    from minimax_h3_prompt.graph import pipeline

    def fake_structured(brief, config, *, generation_id, topic_id, project_id):
        return result_from_state({
            "script": "自动生成的剧本", "final_prompt": "视频提示词",
            "character_design": "人物提示词", "prop_design": "道具提示词",
            "background_design": "场景提示词",
            "fl2va_prompt_bundle": {
                "scene_anchor": "rainy night street corner",
                "first": {
                    "zimage": {"positive_prompt": "A person stands at a rainy night street corner."},
                    "flux2": {"positive_prompt": "Flux.2 view of a person at the same rainy night street corner."},
                },
                "last": {
                    "zimage": {"positive_prompt": "The same person lowers an envelope at the same rainy night street corner."},
                    "flux2": {"positive_prompt": "Flux.2 view of the same person lowering an envelope at the same street corner."},
                },
                "continuity_constraints": ["Keep the same person, envelope, street corner, rain, and lighting."],
            },
        }, brief, generation_id=generation_id, topic_id=topic_id, project_id=project_id)

    monkeypatch.setattr(pipeline, "run_pipeline_structured", fake_structured)
    root = tmp_path / "Assets"
    assert main([
        "create-video", "--root", str(root), "--topic", "雨夜街角收到旧信",
    ]) == 0
    output = capsys.readouterr().out
    assert "自动生成的剧本" in output
    assert "FL2VA 首帧提示词" in output
    assert "人物提示词" not in output
    projects = list(root.glob("*/projects/project-001/generations/GEN001/generation.json"))
    assert len(projects) == 1
    generation_dir = projects[0].parent
    assert (generation_dir / "first-frame-prompt.md").exists()
    assert (generation_dir / "last-frame-prompt.md").exists()
    assert (generation_dir / "fl2va-prompt.md").exists()
    assert not (generation_dir / "character-prompt.md").exists()
    assert not (generation_dir / "prop-prompt.md").exists()
    assert not (generation_dir / "scene-prompt.md").exists()
