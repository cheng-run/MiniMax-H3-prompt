"""#227 复审修复：``scripts/accept_d4.py`` 的机械部分守卫。

不启服务、不跑模型、不开浏览器：这里只钉两件在复审里被实测打穿的事——
``--self-check`` 的桩模式必须让退出码非零，以及完成态帧证据从会话文件读真字段。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "accept_d4.py"
SPEC = importlib.util.spec_from_file_location("accept_d4", SCRIPT)
assert SPEC and SPEC.loader
accept_d4 = importlib.util.module_from_spec(SPEC)
# dataclass 定义时要按 ``__module__`` 回查 sys.modules；不登记会拿到 None。
sys.modules[SPEC.name] = accept_d4
SPEC.loader.exec_module(accept_d4)


def _self_check_args(tmp_path: Path, *extra: str):
    image = tmp_path / "photo.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
    return accept_d4.parse_args([
        "--image", str(image), "--out-dir", str(tmp_path / "out"), *extra])


def test_self_check_fails_in_fake_mode(tmp_path, monkeypatch, capsys):
    """桩模式自检必须非零：真实验收不该在桩模式上「自检通过」。"""
    # 其余检查全部放行，只让「真实模型模式」成为唯一失败项。
    monkeypatch.setattr(accept_d4, "_health", lambda _client, _base: {"version": "t"})
    monkeypatch.setattr(
        accept_d4.importlib.util, "find_spec", lambda _name: object())
    args = _self_check_args(tmp_path, "--fake")

    code = accept_d4.self_check(args)

    assert code == 2
    assert "FAIL  真实模型模式" in capsys.readouterr().out


def test_frame_read_evidence_reads_descriptions_and_targets(tmp_path):
    session_id = "雨夜旧信-test/GEN001"
    directory = tmp_path / session_id
    (directory / "assets").mkdir(parents=True)
    target = directory / "assets" / "frame.png"
    target.write_bytes(b"png")
    state = {
        "stage_state": {
            "fl2va_frame_descriptions": [
                {"role": "first", "description": "首帧：雨夜街口撑伞"},
                {"role": "last", "description": "尾帧：收伞回望"},
            ],
            "frame_images": [{"role": "first", "target": str(target)}],
        },
    }
    (directory / accept_d4.SESSION_SNAPSHOT).write_text(
        json.dumps(state, ensure_ascii=False), encoding="utf-8")

    evidence = accept_d4._frame_read_evidence(tmp_path, session_id)

    assert evidence["descriptions"]["first"].startswith("首帧")
    assert evidence["descriptions"]["last"].startswith("尾帧")
    assert evidence["frame_images"] == [str(target)]


def test_frame_read_evidence_rejects_an_empty_description(tmp_path):
    session_id = "S/GEN001"
    directory = tmp_path / session_id
    directory.mkdir(parents=True)
    state = {
        "stage_state": {
            "fl2va_frame_descriptions": [
                {"role": "first", "description": "有描述"},
                {"role": "last", "description": "  "},
            ],
            "frame_images": [],
        },
    }
    (directory / accept_d4.SESSION_SNAPSHOT).write_text(
        json.dumps(state, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(accept_d4.AcceptanceError):
        accept_d4._frame_read_evidence(tmp_path, session_id)
