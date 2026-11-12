"""#236 D5-1 打包 spike 的机械部分守卫。

不建窗口、不跑构建：这里只钉两件「不实测就不知道对不对」的事——
spike 程序的参数/页面路径解析，以及构建脚本交给 PyInstaller 的参数形态
（输出目录不能叫 ``dist``、本地页面必须作为数据文件打进去、可选 GUI 后端要排除）。
真实窗口与 EXE 的实证在 ``docs/research/2026-11-12-exe-packaging-spike.md`` 里。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SPIKE = Path(__file__).resolve().parents[1] / "scripts" / "spike_exe.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # 模块级 dataclass/函数按 __module__ 回查 sys.modules，不登记会拿到 None。
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


spike_exe = _load(SPIKE, "spike_exe")


def test_parse_args_defaults_keep_the_window_self_closing():
    """无人值守：默认必须自己关窗，并且默认不开调试端口。"""
    args = spike_exe.parse_args([])
    assert args.auto_close_seconds > 0
    assert args.debug_port is None


def test_parse_args_reads_auto_close_and_debug_port():
    args = spike_exe.parse_args(["--auto-close-seconds", "3", "--debug-port", "9333"])
    assert args.auto_close_seconds == 3
    assert args.debug_port == 9333


def test_page_dir_prefers_the_frozen_bundle_root(tmp_path):
    """冻结后本地页面在 ``sys._MEIPASS``，源码运行在脚本旁边——两处都要指对。"""
    source_dir = tmp_path / "scripts"
    assert spike_exe.resolve_page_dir(None, source_dir) == source_dir

    bundle_dir = tmp_path / "_MEI123456"
    assert spike_exe.resolve_page_dir(str(bundle_dir), source_dir) == bundle_dir


def test_page_url_is_a_file_uri_pointing_at_the_local_page(tmp_path):
    url = spike_exe.page_url(tmp_path)
    assert url.startswith("file:///")
    assert url.endswith("/" + spike_exe.PAGE_NAME)


BUILD = Path(__file__).resolve().parents[1] / "scripts" / "build_spike_exe.py"
build_spike = _load(BUILD, "build_spike_exe")


def test_build_output_dir_is_not_named_dist():
    """spec §9：PyInstaller 输出目录必须改名，别和 Vite 的 ``frontend/dist`` 撞。"""
    args = build_spike.parse_args([])
    assert Path(build_spike.dist_dir(args)).name != "dist"
    assert Path(build_spike.dist_dir(args)).name == "spike"
    assert "build-exe" in Path(build_spike.dist_dir(args)).parts


def test_pyinstaller_args_shape(tmp_path):
    args = build_spike.parse_args(["--build-root", str(tmp_path)])
    cmd = build_spike.pyinstaller_args(args)

    assert any("PyInstaller" in str(part) for part in cmd) or "PyInstaller" in " ".join(cmd)
    assert "--onedir" in cmd
    # 本地页面作为数据文件进包，分隔符是 Windows 的 ``;``
    add_data = [cmd[i + 1] for i, part in enumerate(cmd) if part == "--add-data"]
    assert any(entry.endswith(f"{spike_exe.PAGE_NAME};.") for entry in add_data), add_data
    # 没装的 GUI 后端不进包（spec §9）
    excluded = {cmd[i + 1] for i, part in enumerate(cmd) if part == "--exclude-module"}
    assert {"PyQt5", "PyQt6", "PySide2", "PySide6", "gtk", "gi"} <= excluded
    # 冻结后 importlib.metadata 要能读到版本号——dist-info 不显式收集就「未安装」
    copied = {cmd[i + 1] for i, part in enumerate(cmd) if part == "--copy-metadata"}
    assert {"pywebview", "pythonnet", "clr-loader"} <= copied, copied
    assert cmd[-1].endswith("spike_exe.py")


def test_expected_exe_path_lands_in_the_dist_dir(tmp_path):
    args = build_spike.parse_args(["--build-root", str(tmp_path), "--name", "H3Spike"])
    exe = Path(build_spike.expected_exe(args))
    assert exe.name == "H3Spike.exe"
    # onedir 形态：EXE 在 <dist>/<name>/ 下，同级还有一个 _internal/
    assert exe.parent == Path(build_spike.dist_dir(args)).resolve() / "H3Spike"
