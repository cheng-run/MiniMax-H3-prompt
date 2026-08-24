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
        }, brief, generation_id=generation_id, topic_id=topic_id, project_id=project_id)

    monkeypatch.setattr(pipeline, "run_pipeline_structured", fake_structured)
    root = tmp_path / "Assets"
    assert main([
        "create-video", "--root", str(root), "--topic", "雨夜街角收到旧信",
    ]) == 0
    output = capsys.readouterr().out
    assert "自动生成的剧本" in output
    assert "人物提示词" in output
    assert "视频剧情提示词" in output
    projects = list(root.glob("*/projects/project-001/generations/GEN001/generation.json"))
    assert len(projects) == 1
    assert "api_key" not in json.dumps(json.loads(projects[0].read_text(encoding="utf-8")))
