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


# ---------------------------------------------------------------------------
# 交互控制流：stdin 排水 / 阶段 2 门禁 / 跳过二次确认（全部 mock，不碰真实键盘）
# ---------------------------------------------------------------------------

import builtins  # noqa: E402
import sys  # noqa: E402
from types import SimpleNamespace  # noqa: E402

import minimax_h3_prompt.ui.wizard as wizard_module  # noqa: E402


def _scripted_inputs(monkeypatch, prompts_seen, answers):
    """把 builtins.input 换成脚本应答器；记录每个提问并按序作答。"""
    queue = iter(answers)

    def fake_input(prompt=""):
        prompts_seen.append(prompt)
        try:
            return next(queue)
        except StopIteration:
            raise AssertionError(f"输入序列耗尽，多出的提问：{prompt!r}")

    monkeypatch.setattr(builtins, "input", fake_input)


def _fake_config(monkeypatch, tmp_path):
    from minimax_h3_prompt.config import Config

    config = Config()
    monkeypatch.setattr(config, "assets_root", str(tmp_path))
    return config


def _fake_tty(monkeypatch):
    """让向导以为在交互终端运行；同时用假 msvcrt 保证不碰真实键盘。"""
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(kbhit=lambda: False, getwch=lambda: "\r"))


def _stage1_state():
    state = _stage_state()
    state.setdefault("fl2va_frame_descriptions", [])
    return state


def _patch_run_stage1(monkeypatch):
    calls = []

    def fake_run_stage1(brief, config, *, on_node=None):
        calls.append(True)
        if on_node is not None:
            on_node("producer")
        return _stage1_state(), object(), object()

    monkeypatch.setattr(wizard_module, "run_stage1", fake_run_stage1)
    return calls


def _patch_run_stage2(monkeypatch):
    calls = []

    def fake_run_stage2(state, brief, config, **kwargs):
        calls.append({"variant": brief.variant})
        return dict(state), "# 最终视频提示词"

    monkeypatch.setattr(wizard_module, "run_stage2", fake_run_stage2)
    return calls


def test_drain_stdin_noop_when_not_tty(monkeypatch):
    """非 tty（pytest/管道）下排水必须为 noop——绝不触碰键盘缓冲。"""
    consumed = []
    fake_msvcrt = SimpleNamespace(
        kbhit=lambda: True,
        getwch=lambda: consumed.append("k") or "\r",
    )
    monkeypatch.setattr(wizard_module.sys, "stdin", SimpleNamespace(isatty=lambda: False))
    monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)
    wizard_module._drain_stdin()
    assert consumed == []


def test_drain_stdin_consumes_buffered_keys(monkeypatch):
    """tty 下排空缓冲按键后停止。"""
    buffered = ["\r", "\n", "x"]
    fake_msvcrt = SimpleNamespace(
        kbhit=lambda: bool(buffered),
        getwch=buffered.pop,
    )
    monkeypatch.setattr(wizard_module.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)
    wizard_module._drain_stdin()
    assert buffered == []


def test_drain_stdin_survives_missing_msvcrt(monkeypatch):
    """msvcrt 导入失败（POSIX）时走 termios 兜底且不抛异常。"""
    monkeypatch.setattr(wizard_module.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setitem(sys.modules, "msvcrt", None)  # None ⇒ import 抛 ImportError
    monkeypatch.setitem(sys.modules, "termios", None)  # 同样缺失时静默放弃
    wizard_module._drain_stdin()  # 不应抛异常


def test_ask_optional_path_requires_double_confirm_to_skip(monkeypatch, capsys):
    seen: list[str] = []
    # 路径为空 → 警告 → 确认跳过输 y ⇒ 返回空串
    _scripted_inputs(monkeypatch, seen, ["", "y"])
    assert wizard_module._ask_optional_path("首帧") == ""
    assert "[警告]" in capsys.readouterr().out
    assert any("回车=跳过" in p for p in seen)

    # 路径为空 → 确认跳过输 n ⇒ 重新询问，第二次给真实路径
    seen.clear()
    _scripted_inputs(monkeypatch, seen, ["", "n", r"D:\frames\first.png"])
    assert wizard_module._ask_optional_path("首帧") == r"D:\frames\first.png"
    assert sum(1 for p in seen if "回车=跳过" in p) == 2


def test_full_wizard_gate_default_exits_before_stage2(monkeypatch, tmp_path, capsys):
    """阶段 1 完成 → 门禁处直接回车（默认否）⇒ 退出码 0 且不进阶段 2。"""
    _fake_tty(monkeypatch)
    config = _fake_config(monkeypatch, tmp_path)
    stage1_calls = _patch_run_stage1(monkeypatch)
    stage2_calls = _patch_run_stage2(monkeypatch)
    seen: list[str] = []
    _scripted_inputs(monkeypatch, seen, ["酒馆短剧", "", "", "", ""])  # 主题、时长、风格、审阅默认、门禁默认

    code = wizard_module.run_wizard(config)

    assert code == 0
    assert stage1_calls == [True]
    assert stage2_calls == []
    joined = "\n".join(seen)
    assert "对生图提示词有修改意见？ [y/N]: " in joined
    assert "是否立即继续阶段 2" in joined
    assert not any("首帧图片路径" in p for p in seen), "不应出现阶段 2 的路径提问"


def test_full_wizard_gate_yes_runs_phase2_with_skip_confirms(monkeypatch, tmp_path):
    """门禁答 y 进阶段 2；首尾帧双跳过需二次确认，最终降级 T2VA 并写出提示词。"""
    _fake_tty(monkeypatch)
    config = _fake_config(monkeypatch, tmp_path)
    _patch_run_stage1(monkeypatch)
    stage2_calls = _patch_run_stage2(monkeypatch)
    seen: list[str] = []
    # 主题、时长、风格、审阅默认、门禁 y、首帧空+确认 y、尾帧空+确认 y
    _scripted_inputs(monkeypatch, seen, ["酒馆短剧", "", "", "", "y", "", "y", "", "y"])

    code = wizard_module.run_wizard(config)

    assert code == 0
    assert len(stage2_calls) == 1
    assert stage2_calls[0]["variant"] == "T2VA"
    video_files = list(tmp_path.rglob("video-prompt.md"))
    assert len(video_files) == 1
    assert "# 最终视频提示词" in video_files[0].read_text(encoding="utf-8")
    joined = "\n".join(seen)
    assert "确认跳过首帧？" in joined and "确认跳过尾帧？" in joined


def test_regenerate_error_unsubscribes_progress_listener(monkeypatch, tmp_path):
    """重生成中途抛异常也必须解除进度订阅（try/finally 对称）。"""
    from minimax_h3_prompt.observability import reporter
    from minimax_h3_prompt.ui.progress import TextProgress

    _fake_tty(monkeypatch)
    config = _fake_config(monkeypatch, tmp_path)
    _patch_run_stage1(monkeypatch)

    def boom(state):
        raise RuntimeError("模型故障")

    import minimax_h3_prompt.graph.nodes as nodes_mod

    monkeypatch.setattr(
        nodes_mod, "make_nodes",
        lambda *a, **k: {"fl2va_frame_prompts": boom},
    )

    seen: list[str] = []
    _scripted_inputs(monkeypatch, seen, ["酒馆短剧", "", "", "y", "1", "更多火光"])

    before = list(reporter._subscribers)
    with pytest.raises(RuntimeError):
        wizard_module.run_wizard(config)
    after = list(reporter._subscribers)

    assert after == before, "异常退出后 reporter 不得残留向导订阅"


def test_text_progress_lines_with_stripped_role_prefix(capsys):
    """TextProgress 输出开始/完成行：带耗时与累计时间，且剥掉 role_ 前缀。"""
    from minimax_h3_prompt.ui.progress import TextProgress

    tick = iter([100.0, 142.0])
    listener = TextProgress(clock=lambda: next(tick))

    listener({"type": "agent_start", "role": "role_producer"})
    listener({"type": "agent_done", "role": "role_producer", "duration": 38.2})
    listener({"type": "other_event"})  # 无关事件被忽略

    out = capsys.readouterr().out
    assert "▶ producer 正在处理……" in out
    assert "✓ producer 完成（38.2s | 累计 0:42）" in out
    assert "role_" not in out
