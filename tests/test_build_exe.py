"""D5-6（#241）打包流水线的机械守卫。

不跑构建、不联网：这里钉住「漏了不报错、只会静默少东西」的那几处形状——
包内资源与前端产物必须显式 ``--add-data``、自己的 dist-info 必须随包（否则
``_resolve_version`` 起不来）、输出目录不能叫 ``dist``、ffmpeg 二进制要显式收、
版本号只能从 ``pyproject.toml`` 取一份。

真实构建、冻结自检与体积证据在 PR/票面报告里，不在用例里跑（PyInstaller 打
langchain 全家桶要几分钟）。
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BUILD = REPO_ROOT / "scripts" / "build_exe.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # 模块级 dataclass/函数按 __module__ 回查 sys.modules，不登记会拿到 None。
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


build_exe = _load(BUILD, "build_exe")


def test_version_comes_from_pyproject_and_reaches_the_version_file(tmp_path):
    """版本号只有 ``pyproject.toml`` 一份真源；version file 与它同值（#241 AC）。"""
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    expected = pyproject["project"]["version"]

    assert build_exe.read_version() == expected

    text = build_exe.version_file_text(expected)
    assert f"filevers={build_exe.version_tuple(expected)!r}" in text
    assert f"StringStruct('FileVersion', '{expected}')" in text
    assert f"StringStruct('ProductVersion', '{expected}')" in text


def test_pyinstaller_args_shape(tmp_path):
    """漏了不报错、只会静默少东西的那几处，逐条钉住（spec #235 §9）。"""
    args = build_exe.parse_args(["--build-root", str(tmp_path / "build-exe")])

    dist = Path(build_exe.dist_dir(args))
    assert dist.name != "dist", "PyInstaller 输出目录不能和 frontend/dist 同名"
    assert dist.name == build_exe.DIST_NAME
    assert dist.parent.name == "build-exe"

    version_file = tmp_path / "version_info.txt"
    version_file.write_text("dummy", encoding="utf-8")
    ffmpeg = tmp_path / "ffmpeg.exe"
    cmd = build_exe.pyinstaller_args(args, version_file=version_file, ffmpeg_path=ffmpeg)

    assert "--onedir" in cmd
    assert "--windowed" in cmd and "--console" not in cmd
    assert "--version-file" in cmd
    assert cmd[cmd.index("--version-file") + 1] == str(version_file)

    add_data = [cmd[i + 1] for i, part in enumerate(cmd) if part == "--add-data"]
    sep = os.pathsep
    for source, dest in (
        ("frontend/dist", "frontend/dist"),
        ("config", "config"),
        ("src/minimax_h3_prompt/prompts", "minimax_h3_prompt/prompts"),
        ("src/minimax_h3_prompt/knowledge", "minimax_h3_prompt/knowledge"),
        ("src/minimax_h3_prompt/references", "minimax_h3_prompt/references"),
    ):
        # 源必须是绝对路径：--add-data 的相对路径相对 --specpath 解析（实测踩过）。
        assert f"{REPO_ROOT / source}{sep}{dest}" in add_data, (source, add_data)

    copy_metadata = [cmd[i + 1] for i, part in enumerate(cmd) if part == "--copy-metadata"]
    # 自己的 dist-info 是关键：``_resolve_version`` 读它，漏了冻结后直接起不来。
    for package in ("minimax-h3-prompt", "pywebview", "pythonnet", "clr-loader"):
        assert package in copy_metadata, (package, copy_metadata)

    add_binary = [cmd[i + 1] for i, part in enumerate(cmd) if part == "--add-binary"]
    assert f"{ffmpeg}{sep}imageio_ffmpeg/binaries" in add_binary, add_binary

    excluded = {cmd[i + 1] for i, part in enumerate(cmd) if part == "--exclude-module"}
    assert {"PyQt5", "PyQt6", "PySide2", "PySide6", "gtk", "gi", "cefpython3"} <= excluded

    # ``from .api import __main__`` 不会被自动收集，冻结后启动即 ImportError。
    hidden = [cmd[i + 1] for i, part in enumerate(cmd) if part == "--hidden-import"]
    assert "minimax_h3_prompt.api.__main__" in hidden, hidden

    assert str(build_exe.ENTRY_SCRIPT) == cmd[-1]
    assert build_exe.ENTRY_SCRIPT.name == "exe_entry.py"


def test_package_data_dirs_come_from_the_package_constant():
    """资源目录清单只有一份（``selfcheck.PACKAGE_DATA_DIRS``）：打包与冻结自检同源。

    #243 复审抓出 ``build_exe.PACKAGE_DATA_DIRS`` 与 ``selfcheck.RESOURCE_DIRS`` 是两份
    同形字面量——新增一个资源目录时，只改一处就会出现「打包收了、自检不查」。
    """
    from minimax_h3_prompt import selfcheck

    assert build_exe.PACKAGE_DATA_DIRS is selfcheck.PACKAGE_DATA_DIRS


def test_verify_bundle_checks_every_package_data_dir(tmp_path, monkeypatch):
    """``verify_bundle`` 从常量派生核对项：新增一个资源目录必须被点名核对。"""
    dist = tmp_path / "MiniMax-H3-prompt"
    internal = dist / "_internal"
    dist.mkdir()
    (dist / f"{build_exe.DIST_NAME}.exe").write_bytes(b"exe")
    for relative in ("frontend/dist", "config"):
        (internal / relative).mkdir(parents=True)
    (internal / "frontend" / "dist" / "index.html").write_text("<html></html>", encoding="utf-8")
    (internal / "config" / "agent.yaml").write_text("", encoding="utf-8")
    (internal / "imageio_ffmpeg" / "binaries").mkdir(parents=True)
    (internal / "imageio_ffmpeg" / "binaries" / "ffmpeg.exe").write_bytes(b"ff")
    for name in build_exe.PACKAGE_DATA_DIRS:
        (internal / "minimax_h3_prompt" / name).mkdir(parents=True)

    # 现有清单齐全时通过（否则下面的失败无法归因到新增项）。
    build_exe.verify_bundle(dist, tmp_path / "ffmpeg.exe")

    monkeypatch.setattr(
        build_exe, "PACKAGE_DATA_DIRS", (*build_exe.PACKAGE_DATA_DIRS, "new-assets"))
    with pytest.raises(build_exe.BuildError) as excinfo:
        build_exe.verify_bundle(dist, tmp_path / "ffmpeg.exe")

    assert "new-assets" in str(excinfo.value)


def test_readme_tells_the_user_where_the_key_and_port_live():
    """README 是交付目录里唯一的人类入口：SmartScreen、密钥落点、端口三件事必须写全。"""
    text = build_exe.readme_text("0.1.0")

    assert "0.1.0" in text
    # SmartScreen 的具体点击路径（spec #4：不签名，首次运行会被拦）。
    assert "SmartScreen" in text
    assert "更多信息" in text and "仍要运行" in text
    # 密钥不在 data/ 下，避免作品目录被拷走时带上 key（spec #4/§7）。
    assert r"%APPDATA%\MiniMax-H3-prompt\.env" in text
    assert "DASHSCOPE_API_KEY" in text
    # 实际端口在窗口标题与日志里看（spec #5.2 的三处可得）。
    assert "窗口标题" in text
    assert "data" in text and "logs" in text


def test_zip_entries_skip_runtime_state_and_keep_the_top_folder(tmp_path):
    """zip 解压即得一个目录；运行时才建的数据目录与密钥绝不进包。"""
    dist = tmp_path / "MiniMax-H3-prompt"
    (dist / "_internal").mkdir(parents=True)
    (dist / "MiniMax-H3-prompt.exe").write_bytes(b"exe")
    (dist / "_internal" / "python314.dll").write_bytes(b"dll")
    (dist / "README.txt").write_text("hi", encoding="utf-8")
    for junk in ("data/config/agent.yaml", "logs/run.log", "output/x.bin", ".env"):
        target = dist / junk
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("secret", encoding="utf-8")

    entries = build_exe.zip_entries(dist)
    names = [arcname for _source, arcname in entries]

    assert "MiniMax-H3-prompt/MiniMax-H3-prompt.exe" in names
    assert "MiniMax-H3-prompt/_internal/python314.dll" in names
    assert "MiniMax-H3-prompt/README.txt" in names
    assert not any(
        name.split("/", 1)[1].split("/", 1)[0] in {"data", "logs", "output"}
        for name in names
    ), names
    assert not any(name.endswith("/.env") for name in names), names


def test_create_zip_writes_the_archive(tmp_path):
    dist = tmp_path / "MiniMax-H3-prompt"
    dist.mkdir()
    (dist / "MiniMax-H3-prompt.exe").write_bytes(b"exe")
    archive = tmp_path / "MiniMax-H3-prompt.zip"

    build_exe.create_zip(dist, archive)

    import zipfile

    with zipfile.ZipFile(archive) as zf:
        assert zf.namelist() == ["MiniMax-H3-prompt/MiniMax-H3-prompt.exe"]
