"""两阶段向导流程测试：会话持久化、缺帧降级矩阵、读图注入、帧图入库（全部 mock，不打真实 API）。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from minimax_h3_prompt.brief_parser import Brief, RefItem
from minimax_h3_prompt.execution import copy_frame_image, store_root_default
from minimax_h3_prompt.session_store import (
    STATUS_AWAITING_FRAMES,
    STATUS_COMPLETED,
    find_awaiting_sessions,
    load_session,
    save_session,
)
from minimax_h3_prompt.tools.frame_auditor import (
    audit_frame_images,
    required_frames,
)


# ---------------------------------------------------------------------------
# required_frames / resolve_effective_variant 降级矩阵
# ---------------------------------------------------------------------------

def test_required_frames_by_variant():
    assert required_frames("FL2VA") == ((1, "first"), (2, "last"))
    assert required_frames("I2VA") == ((1, "first"),)
    assert required_frames("L2VA") == ((1, "last"),)
    assert required_frames("T2VA") == ()
    assert required_frames("unknown") == ()


def test_resolve_effective_variant_matrix():
    from minimax_h3_prompt.ui.wizard import resolve_effective_variant

    assert resolve_effective_variant(True, True) == "FL2VA"
    assert resolve_effective_variant(True, False) == "I2VA"
    assert resolve_effective_variant(False, True) == "L2VA"
    assert resolve_effective_variant(False, False) == "T2VA"


# ---------------------------------------------------------------------------
# session_store 持久化与恢复
# ---------------------------------------------------------------------------

def _stage_state() -> dict:
    return {
        "script": "剧本",
        "fl2va_prompt_bundle": {
            "scene_anchor": "medieval tavern interior",
            "first": {"zimage": {"positive_prompt": "Three adventurers inside a medieval tavern interior."}},
            "last": {"zimage": {"positive_prompt": "The same adventurers raise mugs inside the tavern."}},
            "continuity_constraints": ["Keep the same characters and props."],
        },
        "final_prompt": "",
    }


def test_session_roundtrip(tmp_path):
    brief = Brief(variant="FL2VA", duration=5, plot="在酒馆喝酒")
    directory = tmp_path / "GEN001"
    save_session(directory, brief, _stage_state(), status=STATUS_AWAITING_FRAMES)

    session = load_session(directory)
    assert session is not None
    assert session.awaiting_frames
    assert session.brief.plot == "在酒馆喝酒"
    assert session.brief.variant == "FL2VA"
    assert session.stage_state["script"] == "剧本"
    assert session.stage_state["fl2va_prompt_bundle"]["scene_anchor"] == "medieval tavern interior"


def test_find_awaiting_sessions_scans_assets_root(tmp_path):
    brief = Brief(variant="FL2VA", duration=5, plot="酒馆")
    gen_dir = tmp_path / "酒馆-ab12cd34" / "projects" / "project-001" / "generations" / "GEN001"
    save_session(gen_dir, brief, _stage_state(), status=STATUS_AWAITING_FRAMES)
    # completed 的不应出现
    done_dir = tmp_path / "其他-ee55ff66" / "projects" / "project-001" / "generations" / "GEN001"
    save_session(done_dir, brief, _stage_state(), status=STATUS_COMPLETED)

    sessions = find_awaiting_sessions(tmp_path)
    assert len(sessions) == 1
    assert sessions[0].directory == gen_dir


def test_load_session_missing_or_corrupt_returns_none(tmp_path):
    assert load_session(tmp_path) is None
    (tmp_path / "session-state.json").write_text("{broken", encoding="utf-8")
    assert load_session(tmp_path) is None


def test_session_preserves_refs_with_paths(tmp_path):
    brief = Brief(variant="FL2VA", duration=5, plot="酒馆")
    brief.refs.append(RefItem(picture=1, name="首帧", description="", path=r"D:\pics\first.png"))
    brief.refs.append(RefItem(picture=2, name="尾帧", description="木桌", path=r"D:\pics\last.png"))
    directory = tmp_path / "GEN001"
    save_session(directory, brief, {}, status=STATUS_AWAITING_FRAMES)
    restored = load_session(directory)
    assert [r.path for r in restored.brief.refs] == [r"D:\pics\first.png", r"D:\pics\last.png"]
    assert restored.brief.refs[1].description == "木桌"


# ---------------------------------------------------------------------------
# frame_auditor 读图（mock describe）
# ---------------------------------------------------------------------------

def _make_png(path):
    Image.new("RGB", (64, 32), color=(120, 40, 40)).save(path)


def test_audit_frame_images_reads_required_roles(tmp_path):
    first_png = tmp_path / "first.png"
    last_png = tmp_path / "last.png"
    _make_png(first_png)
    _make_png(last_png)
    captured = []

    def fake_describe(path, instruction):
        captured.append((str(path), "第一帧" in instruction))
        return f"描述：{Path(path).name}"

    refs = [
        RefItem(picture=1, name="首帧", description="", path=str(first_png)),
        RefItem(picture=2, name="尾帧", description="", path=str(last_png)),
    ]
    audits = audit_frame_images(refs, "FL2VA", describe=fake_describe)
    assert [a.role for a in audits] == ["first", "last"]
    assert audits[0].description == "描述：first.png"
    # 首帧的指令应包含「第一帧」字样
    assert captured[0][1] is True
    assert captured[1][1] is False


def test_audit_skips_frames_without_path(tmp_path):
    refs = [RefItem(picture=1, name="首帧", description="", path="")]
    assert audit_frame_images(refs, "FL2VA", describe=lambda p, i: "x") == []


def test_audit_single_frame_failure_degrades(tmp_path):
    first_png = tmp_path / "first.png"
    _make_png(first_png)
    calls = {"n": 0}

    def flaky_describe(path, instruction):
        calls["n"] += 1
        raise RuntimeError("vision api down")

    refs = [RefItem(picture=1, name="首帧", description="", path=str(first_png))]
    assert audit_frame_images(refs, "I2VA", describe=flaky_describe) == []


def test_audit_missing_file_raises(tmp_path):
    refs = [RefItem(picture=1, name="首帧", description="", path=str(tmp_path / "nope.png"))]
    with pytest.raises(FileNotFoundError):
        audit_frame_images(refs, "I2VA", describe=lambda p, i: "x")


# ---------------------------------------------------------------------------
# copy_frame_image 帧图入库
# ---------------------------------------------------------------------------

def test_copy_frame_image_copies_and_traces(tmp_path):
    src = tmp_path / "src"
    out_root = tmp_path / "assets"
    src.mkdir()
    first_png = src / "gen_00001_.png"
    last_png = src / "gen_00002_.png"
    _make_png(first_png)
    _make_png(last_png)

    record = copy_frame_image(
        first_png, topic_id="酒馆-test", generation_id="GEN001",
        role="first", assets_root=out_root,
    )
    assert record["role"] == "first"
    target = out_root / "inbox" / "酒馆-test" / "GEN001" / "frames" / "first.png"
    assert target.is_file()

    copy_frame_image(
        last_png, topic_id="酒馆-test", generation_id="GEN001",
        role="last", assets_root=out_root,
    )
    source = json.loads((out_root / "inbox" / "酒馆-test" / "GEN001" / "frames" / "source.json").read_text(encoding="utf-8"))
    roles = {item["role"] for item in source["frames"]}
    assert roles == {"first", "last"}
    assert all(len(item["sha256"]) == 64 for item in source["frames"])


def test_copy_frame_image_same_hash_skips_recopy(tmp_path):
    src = tmp_path / "a.png"
    _make_png(src)
    out_root = tmp_path / "assets"
    kwargs = dict(topic_id="t", generation_id="GEN001", role="first", assets_root=out_root)
    first_record = copy_frame_image(src, **kwargs)
    target = out_root / "inbox" / "t" / "GEN001" / "frames" / "first.png"
    first_mtime = target.stat().st_mtime_ns
    second_record = copy_frame_image(src, **kwargs)
    assert target.stat().st_mtime_ns == first_mtime  # 同哈希未重写
    assert first_record["sha256"] == second_record["sha256"]


def test_copy_frame_image_rejects_bad_role_and_missing_file(tmp_path):
    with pytest.raises(ValueError):
        copy_frame_image(tmp_path, topic_id="t", generation_id="GEN001", role="middle", assets_root=tmp_path)
    with pytest.raises(FileNotFoundError):
        copy_frame_image(tmp_path / "none.png", topic_id="t", generation_id="GEN001", role="first", assets_root=tmp_path)


# ---------------------------------------------------------------------------
# nodes._fl2va_frame_context 真实描述优先
# ---------------------------------------------------------------------------

def test_frame_context_prefers_real_descriptions():
    from minimax_h3_prompt.graph.nodes import _fl2va_frame_context

    state = {
        "fl2va_prompt_bundle": {
            "scene_anchor": "medieval tavern interior",
            "first": {"zimage": {"positive_prompt": "planned first frame"}},
            "last": {"zimage": {"positive_prompt": "planned last frame"}},
        },
        "fl2va_frame_descriptions": [
            {"picture": 1, "role": "first", "path": "a.png", "description": "真实首帧画面：木桌酒杯"},
            {"picture": 2, "role": "last", "path": "b.png", "description": "真实尾帧画面：举杯庆祝"},
        ],
    }
    text = _fl2va_frame_context(state)
    assert "视频第一帧实际画面：真实首帧画面：木桌酒杯" in text
    assert "视频最后一帧实际画面：真实尾帧画面：举杯庆祝" in text
    assert "以它为准" in text


def test_frame_context_falls_back_to_planned_prompts():
    from minimax_h3_prompt.graph.nodes import _fl2va_frame_context

    state = {
        "fl2va_prompt_bundle": {
            "scene_anchor": "medieval tavern interior",
            "first": [{"model_family": "zimage", "positive_prompt": "planned first frame"}],
            "last": [{"model_family": "zimage", "positive_prompt": "planned last frame"}],
        },
    }
    text = _fl2va_frame_context(state)
    assert "planned first frame" in text
    assert "planned last frame" in text


def test_frame_context_i2va_only_first_real_description():
    from minimax_h3_prompt.graph.nodes import _fl2va_frame_context

    state = {
        "fl2va_prompt_bundle": {},
        "fl2va_frame_descriptions": [
            {"picture": 1, "role": "first", "path": "a.png", "description": "雨夜街角行人"},
        ],
    }
    text = _fl2va_frame_context(state)
    assert "雨夜街角行人" in text


# ---------------------------------------------------------------------------
# topic_slug 命名
# ---------------------------------------------------------------------------

def test_topic_slug_readable_and_unique():
    from minimax_h3_prompt.project_generation import topic_slug

    slug = topic_slug("雨夜旧信")
    assert slug.startswith("雨夜旧信-")
    assert slug == topic_slug("雨夜旧信")
    assert slug != topic_slug("雨夜旧信二")
    assert "/" not in slug and "\\" not in slug


def test_topic_slug_handles_symbols():
    from minimax_h3_prompt.project_generation import topic_slug

    slug = topic_slug("!!!///###")
    assert slug.startswith("topic-")
