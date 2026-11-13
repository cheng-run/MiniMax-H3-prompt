"""#251：``scripts/accept_d6.py`` 的机械部分守卫。

不联网、不建模型、不起服务：这里只钉三件会被「脚本跑绿了但其实没验」吞掉的事——

1. ``--self-check`` 在桩模式与配置来源不符时必须判 FAIL 且退出码非零；
2. 配置来源的比对是纯函数，端点/模型/sha256 任一项不符都要点名；
3. 段提交目录的解析与报告组装不会把「缺段」静默当成「都齐了」，也不写令牌。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "accept_d6.py"
SPEC = importlib.util.spec_from_file_location("accept_d6", SCRIPT)
assert SPEC and SPEC.loader
accept_d6 = importlib.util.module_from_spec(SPEC)
# dataclass 定义时要按 ``__module__`` 回查 sys.modules；不登记会拿到 None。
sys.modules[SPEC.name] = accept_d6
SPEC.loader.exec_module(accept_d6)


def _self_check_args(tmp_path: Path, *extra: str):
    return accept_d6.parse_args([
        "--out-dir", str(tmp_path / "out"),
        "--self-check", *extra,
    ])


def test_self_check_fails_in_fake_mode(tmp_path, monkeypatch, capsys):
    """桩模式自检必须非零：真实验收不该在桩管线服务上「自检通过」。"""
    monkeypatch.setenv(accept_d6.FAKE_PIPELINE_ENV, "1")
    monkeypatch.setattr(accept_d6, "_probe_health", lambda _base: {"version": "t"})
    monkeypatch.setattr(accept_d6, "_ffmpeg_available", lambda: True)

    code = accept_d6.self_check(_self_check_args(tmp_path))

    assert code == 2
    assert "FAIL  真实模型模式" in capsys.readouterr().out


def test_self_check_fails_when_config_sha_differs(tmp_path, monkeypatch, capsys):
    """配置来源不匹配是硬失败：起点错了，后面每一步都只是白跑。"""
    monkeypatch.delenv(accept_d6.FAKE_PIPELINE_ENV, raising=False)
    monkeypatch.setattr(accept_d6, "_probe_health", lambda _base: {"version": "t"})
    monkeypatch.setattr(accept_d6, "_ffmpeg_available", lambda: True)

    code = accept_d6.self_check(
        _self_check_args(tmp_path, "--expect-config-sha256", "0" * 64))

    assert code == 2
    assert "FAIL  配置来源" in capsys.readouterr().out


def test_config_source_problems_names_every_mismatch():
    facts = {
        "config_path": "D:/repo/config/agent.yaml",
        "config_sha256": "a" * 64,
        "primary": {"model": "wanted-model", "base_url": "https://wanted.example/v1"},
    }
    problems = accept_d6.config_source_problems(
        facts,
        expected_path=Path("D:/repo/config/agent.yaml"),
        expected_sha256="b" * 64,
        expected_model="other-model",
        expected_base_url="https://other.example/v1",
    )

    joined = "\n".join(problems)
    assert len(problems) == 3
    assert "sha256" in joined
    assert "wanted-model" in joined and "other-model" in joined
    assert "wanted.example" in joined and "other.example" in joined


def test_config_source_problems_accepts_a_match():
    facts = {
        "config_path": "D:/repo/config/agent.yaml",
        "config_sha256": "c" * 64,
        "primary": {"model": "m", "base_url": "https://e/v1"},
    }

    assert accept_d6.config_source_problems(
        facts,
        expected_path=Path("D:/repo/config/agent.yaml"),
        expected_sha256="c" * 64,
        expected_model="m",
        expected_base_url="https://e/v1",
    ) == []


def test_health_config_problems_flags_another_service(tmp_path):
    config_path = str(tmp_path / "config" / "agent.yaml")
    problems = accept_d6.health_config_problems(
        config_path,
        {"config_path": str(tmp_path / "elsewhere" / "agent.yaml"),
         "missing_keys": ["DASHSCOPE_API_KEY"]},
    )

    joined = "\n".join(problems)
    assert "config_path" in joined
    assert "DASHSCOPE_API_KEY" in joined


def test_collect_segment_videos_maps_two_digit_names(tmp_path):
    for name in ("segment-01.mp4", "segment-02.mp4"):
        (tmp_path / name).write_bytes(b"mp4")
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")

    videos = accept_d6.collect_segment_videos(tmp_path)

    assert sorted(videos) == [1, 2]
    assert videos[1].name == "segment-01.mp4"


def test_collect_segment_videos_rejects_a_misspelled_number(tmp_path):
    """``segment-1.mp4`` 静默忽略会让报告说「缺第 1 段」而不是「名字写错了」。"""
    (tmp_path / "segment-1.mp4").write_bytes(b"mp4")

    with pytest.raises(accept_d6.AcceptanceError, match="segment-1.mp4"):
        accept_d6.collect_segment_videos(tmp_path)


def test_segment_rows_carry_planned_seconds_and_status():
    detail = {
        "segments": [
            {"index": 2, "start_s": 9, "end_s": 18, "status": "waiting_video",
             "prompt_artifact_id": "S/GEN001/segments/shot-02.md",
             "video_asset_id": ""},
            {"index": 1, "start_s": 0, "end_s": 9, "status": "passed",
             "prompt_artifact_id": "S/GEN001/segments/shot-01.md",
             "video_asset_id": "S/GEN001/ab"},
        ],
    }

    rows = accept_d6.segment_rows(detail)

    assert [row["index"] for row in rows] == [1, 2]
    assert rows[0]["planned_seconds"] == 9
    assert rows[1]["status"] == "waiting_video"


def test_pending_segment_item_returns_the_current_segment():
    detail = {
        "action_items": [
            {"item_id": "a", "kind": "stage1_review", "status": "resolved"},
            {"item_id": "b", "kind": "segment_video", "status": "pending",
             "segment_index": 2},
        ],
    }

    item = accept_d6.pending_segment_item(detail)

    assert item is not None
    assert item["segment_index"] == 2
    assert accept_d6.pending_segment_item({"action_items": []}) is None


def test_latest_job_failure_reads_the_last_failed_event(tmp_path):
    session_dir = tmp_path / "S" / "GEN002"
    session_dir.mkdir(parents=True)
    (session_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(entry) for entry in [
            {"type": "job.failed", "job_id": "j1",
             "payload": {"code": "runtime_error", "detail": "第一次"}},
            {"type": "job.succeeded", "job_id": "j2", "payload": {}},
            {"type": "job.failed", "job_id": "j3",
             "payload": {"code": "segment_assembly_refused", "detail": "缺第 2 段"}},
        ]) + "\n",
        encoding="utf-8",
    )

    failure = accept_d6.latest_job_failure(tmp_path, "S/GEN002")

    assert failure == {"code": "segment_assembly_refused", "detail": "缺第 2 段"}


def test_fake_text_marker_is_rejected():
    """外部起的桩服务从协议上观测不到，只能靠产物正文里的桩标记兜住。"""
    accept_d6._reject_fake_text("真实的 H3 提示词正文", "第 1 段提示词")

    with pytest.raises(accept_d6.AcceptanceError, match="桩管线"):
        accept_d6._reject_fake_text("# 分场剧本（桩管线第 1 稿）", "阶段 1 剧本")


def test_render_report_lists_segments_and_never_prints_a_token():
    facts = {
        "command": "accept_d6.py --prepare",
        "phase": "prepare",
        "base_url": "http://127.0.0.1:8000",
        "sessions_root": "D:/repo/output/sessions",
        "session_id": "雨夜/GEN001",
        "plot": "雨夜旧信",
        "variant": "T2VA",
        "duration": 18,
        "started_at": "2026-11-12T00:00:00+00:00",
        "finished_at": "2026-11-12T00:10:00+00:00",
        "server_version": "0.1.0",
        "pipeline": "real（真实 LLM）",
        "config_source": {
            "config_path": "D:/repo/config/agent.yaml",
            "config_sha256": "d" * 64,
            "primary": {"provider": "minimax", "model": "m",
                        "base_url": "https://e/v1"},
            "vision": {"model": "v", "base_url": "https://e/v1"},
        },
        "segments": [
            {"index": 1, "start_s": 0, "end_s": 9, "planned_seconds": 9,
             "status": "passed", "prompt_path": "prompts/segment-01.md",
             "gate_issues": [{"severity": "warning",
                              "code": "segment_video_duration_unknown_tier",
                              "message": "7–15s 档暂无样本"}],
             "submit_ms": 12000, "validate_ms": 34000},
            {"index": 2, "start_s": 9, "end_s": 18, "planned_seconds": 9,
             "status": "waiting_video", "prompt_path": "prompts/segment-02.md",
             "gate_issues": [], "submit_ms": None, "validate_ms": None},
        ],
        "final_film": {"artifact_id": "雨夜/GEN001/final.mp4", "bytes": 123,
                       "range_status": 206},
        "screenshots": [],
        "token": "should-never-be-printed",
    }

    text = accept_d6.render_report(facts, steps=[], failures=[], out_dir=Path("out"))

    assert "segment-01.md" in text and "segment-02.md" in text
    assert "9" in text and "7–15s" in text
    assert "未跑到 60 秒上限" in text
    assert "should-never-be-printed" not in text


def test_default_variant_is_the_frame_chain():
    """真实验收走 FL2VA：T2VA 的第 1 段提示词会引用不存在的 Picture 1。"""
    assert accept_d6.DEFAULT_VARIANT == "FL2VA"


def test_frame_image_argument_must_point_at_a_real_photo(tmp_path):
    """显式指定的首帧图不存在＝硬失败，不能静默改成「没图也能跑」。"""
    args = accept_d6.parse_args([
        "--out-dir", str(tmp_path / "out"), "--prepare",
        "--frame-image", str(tmp_path / "missing.png"),
    ])

    with pytest.raises(accept_d6.AcceptanceError) as raised:
        accept_d6._resolve_frame_image(args)

    assert "missing.png" in str(raised.value)


def test_accept_d4_finder_survives_module_registration():
    """回归：动态载入 accept_d4 前必须登记 sys.modules，否则 @dataclass 炸 AttributeError。"""
    finder = accept_d6._accept_d4_finder()

    assert finder is None or callable(finder)


def test_resume_is_exclusive_with_prepare_and_submit(capsys):
    """续跑是一个独立模式：与 --prepare / --submit-dir 同时给必须当场拒绝。

    续跑存在的理由：阶段 1 的真实调用可能要十几分钟，换模型重试时不能重跑它。
    """
    for extra in (["--prepare"], ["--submit-dir", "x"]):
        code = accept_d6.main(["--resume", "话题/GEN001", *extra])
        assert code == accept_d6.EXIT_FAILED
        assert "只能选一种模式" in capsys.readouterr().out


def test_resume_never_starts_stage1_again():
    """续跑分支不得再起一次阶段 1：服务端会用 409 拦下，白跑一轮真实等待。

    2026-11-13 实测：续跑成功后残留了一行重复的 ``report.run("起阶段 1…")``，
    阶段 2 已经跑完却又去起任务，整轮以 409 收场。
    """
    import inspect

    source = inspect.getsource(accept_d6.run_prepare)

    assert source.count('report.run("起阶段 1 并等待审查待办"') == 1
    assert source.count('report.run("建 18 秒会话"') == 1
