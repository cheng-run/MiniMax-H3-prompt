"""#365：交回的关键帧图尺寸判据（只提示、不阻断）与 CLI 阶段 2 的接线。

判据与 ``segment_validation`` 的语义提示同族：不符时给出一句含**两边数值**的提示，
相符时一声不响（空串），读不出宽高时给「量不出来」的说明——**不判失败、不当成不符**。
调用方没有任何分支能因它把帧退回：图上照样入库、读图、阶段 2 照常推进。
"""
from __future__ import annotations

from types import SimpleNamespace

from PIL import Image

from minimax_h3_prompt import session_store
from minimax_h3_prompt.brief_parser import Brief
from minimax_h3_prompt.director import aspect_flow
from minimax_h3_prompt.segment_validation import frame_canvas_warning_text
from minimax_h3_prompt.tools import frame_auditor
from minimax_h3_prompt.ui import wizard

FRAME_DESC = "深夜便利店内，店员独自站在收银台后，前景地砖上蹲着一只橘猫。"


def _png(path, size: tuple[int, int]):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (40, 60, 100)).save(path, format="PNG")
    return path


# ---------------------------------------------------------------------------
# 纯函数缝：三种结论
# ---------------------------------------------------------------------------

def test_mismatch_names_both_sides(tmp_path):
    image = _png(tmp_path / "first.png", (320, 240))

    note = frame_canvas_warning_text(image, (640, 1280), label="首帧")

    assert note, "尺寸不符却没有提示"
    assert "320×240" in note and "640×1280" in note, "提示里必须给出两边数值"


def test_matching_size_is_silent(tmp_path):
    image = _png(tmp_path / "first.png", (640, 1280))

    assert frame_canvas_warning_text(image, (640, 1280)) == ""


def test_unreadable_image_is_measured_not_mismatched(tmp_path):
    """损坏 / 非图片：量不出来就说量不出来，不能伪装成尺寸不符。"""
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"\x89PNG\r\n\x1a\nnot-really-a-png")

    note = frame_canvas_warning_text(broken, (640, 1280))

    assert note, "读不出宽高时没有任何说明"
    assert "量不出" in note
    assert "不符" not in note, "读不出宽高被当成了尺寸不符"


# ---------------------------------------------------------------------------
# CLI 缝：阶段 2 收帧图时真的打出来，且流程照常推进
# ---------------------------------------------------------------------------

def _drive_phase2(tmp_path, monkeypatch, image, frame_canvas_fn=None):
    brief = Brief(mode="base", variant="I2VA", duration=8.0, style="写实", plot="便利店橘猫")
    generation_dir = tmp_path / "GEN001"
    session_store.save_session(
        generation_dir, brief, {"shot_table": "[Shot 1] 深夜空店内……"},
        status=session_store.STATUS_AWAITING_FRAMES,
    )
    session = session_store.load_session(generation_dir)
    assert session is not None

    monkeypatch.setattr(wizard, "_collect_frame_paths", lambda variant: (str(image), ""))
    monkeypatch.setattr(
        frame_auditor, "audit_frame_images",
        lambda refs, variant, **kwargs: [
            frame_auditor.FrameAudit(picture=1, role="first", path=str(image),
                                     description=FRAME_DESC)
        ],
    )
    # 期望画布由配置推导；这里钉成字面量，用例不随 config/image.yaml 的 frame: 档漂。
    monkeypatch.setattr(
        aspect_flow, "frame_canvas",
        frame_canvas_fn or (lambda canvases=None: (640, 1280)))
    monkeypatch.setattr(wizard, "_load_or_make_summary", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        wizard, "run_stage2",
        lambda state, brief, config, **kwargs: (dict(state), "PROMPT"),
    )
    config = SimpleNamespace(vision_model=SimpleNamespace(model="stub-vision"))
    return wizard._phase2_collect_and_finish(config, session), generation_dir


def test_cli_prints_the_hint_and_keeps_going(tmp_path, monkeypatch, capsys):
    result, generation_dir = _drive_phase2(
        tmp_path, monkeypatch, _png(tmp_path / "first.png", (320, 240)))

    out = capsys.readouterr().out
    assert "320×240" in out and "640×1280" in out, "终端里没有交回图的尺寸提示"
    # 只提示不阻断：帧照常收下、阶段 2 照常跑完。
    assert result == 0
    assert (generation_dir / "video-prompt.md").is_file(), "阶段 2 没有照常收尾"


def test_cli_is_quiet_when_the_size_matches(tmp_path, monkeypatch, capsys):
    result, _ = _drive_phase2(
        tmp_path, monkeypatch, _png(tmp_path / "first.png", (640, 1280)))

    out = capsys.readouterr().out
    assert "关键帧尺寸提示" not in out, "尺寸相符时不许制造噪声"
    assert result == 0


def test_cli_survives_an_unreadable_canvas_config(tmp_path, monkeypatch, capsys):
    """取期望画布时抛错只该一声不响，不该把阶段 2 带停（#363 复审 A3）。

    提示是附言：``config/image.yaml`` 写坏（``ConfigError``）时主流程照常跑，
    否则一条附言算不出来就会让整条生成停在收帧这一步。
    """
    from minimax_h3_prompt.errors import ConfigError

    def _boom(_canvases=None):
        raise ConfigError("image.yaml 读不了")

    result, generation_dir = _drive_phase2(
        tmp_path, monkeypatch, _png(tmp_path / "first.png", (320, 240)),
        frame_canvas_fn=_boom)

    out = capsys.readouterr().out
    assert result == 0, "尺寸提示算不出来时阶段 2 被带停了"
    assert (generation_dir / "video-prompt.md").is_file()
    assert "关键帧尺寸提示" not in out
    assert "尺寸核对跳过" in out, "跳过也要说一声，否则用户以为核过了"


def test_shared_hint_helper_degrades_instead_of_raising(tmp_path):
    """共享收口自己对坏输入降级：CLI 与工作台只调它，不必各写一份 try/except。"""
    from minimax_h3_prompt.segment_validation import frame_canvas_hints_for

    broken = tmp_path / "broken.png"
    broken.write_bytes(b"\x89PNG\r\n\x1a\nnope")

    assert frame_canvas_hints_for({"first": broken}, (640, 1280)).startswith(
        "关键帧尺寸提示")
    # 期望画布为 None（调用方取失败）＝没有可核的期望值，一声不响。
    assert frame_canvas_hints_for({"first": broken}, None) == ""
    assert frame_canvas_hints_for({}, (640, 1280)) == ""
    # 不符与相符各一条，同一次调用里逐帧分开。
    ok = _png(tmp_path / "ok.png", (640, 1280))
    bad = _png(tmp_path / "bad.png", (320, 240))
    lines = frame_canvas_hints_for({"first": ok, "last": bad}, (640, 1280)).splitlines()
    assert len(lines) == 1 and "尾帧" in lines[0]
