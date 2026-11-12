"""D5-1 打包 spike 的构建脚本（#236）：一条命令产出 ``build-exe/spike/H3Spike.exe``。

为什么输出目录叫 ``build-exe/spike`` 而不是 ``dist``：Vite 的前端产物就在
``frontend/dist``，PyInstaller 默认的 ``dist`` 会与之同名互相覆盖（spec §9）。

用法（在仓库根目录跑，Windows）：

    python scripts/build_spike_exe.py            # 干净重建 onedir 产物
    python scripts/build_spike_exe.py --no-clean # 复用上次的 build 缓存

产物：``build-exe/spike/H3Spike/H3Spike.exe``（onedir），antivirus 拦截时放行路径见调研笔记。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"

# 页面文件名与窗口程序共用同一个常量（`spike_exe.PAGE_NAME`），不各写一份字面量。
sys.path.insert(0, str(SCRIPTS_DIR))
from spike_exe import PAGE_NAME  # noqa: E402  （脚本目录要在 sys.path 上才导得到）

ENTRY_SCRIPT = SCRIPTS_DIR / "spike_exe.py"

# 没装的 GUI 后端：pywebview 会在运行时探测它们，PyInstaller 的钩子却可能把整棵
# 依赖树拖进来（spec §9「可选后端显式 excludes」）。
EXCLUDED_MODULES = ("PyQt5", "PyQt6", "PySide2", "PySide6", "gtk", "gi", "cefpython3")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="build_spike_exe",
        description="用 PyInstaller 把 spike 打成 onedir 目录（输出到 build-exe/spike）。",
    )
    parser.add_argument(
        "--build-root",
        default=str(REPO_ROOT / "build-exe"),
        help="构建根目录，产物落 <build-root>/spike/",
    )
    parser.add_argument("--name", default="H3Spike", help="EXE 名（不含 .exe）")
    parser.add_argument(
        "--no-clean",
        dest="clean",
        action="store_false",
        help="不清 PyInstaller 缓存（默认清，保证可复现）",
    )
    parser.add_argument(
        "--windowed",
        action="store_true",
        help="产出不带控制台的窗口程序（默认带控制台，spike 要看运行日志）",
    )
    return parser.parse_args(argv)


def dist_dir(args: argparse.Namespace) -> str:
    return str(Path(args.build_root) / "spike")


def expected_exe(args: argparse.Namespace) -> str:
    """onedir 形态下 EXE 在 ``<dist>/<name>/<name>.exe``。"""
    return str((Path(dist_dir(args)) / args.name / f"{args.name}.exe").resolve())


def pyinstaller_args(args: argparse.Namespace) -> list[str]:
    build_root = Path(args.build_root)
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--onedir",
        "--name",
        args.name,
        "--distpath",
        dist_dir(args),
        "--workpath",
        str(build_root / "work"),
        "--specpath",
        str(build_root / "spec"),
        # 本地页面作为数据文件进包，落在解包根（spike_exe.resolve_page_dir 指向那里）
        "--add-data",
        f"{SCRIPTS_DIR / PAGE_NAME};.",
        # PyInstaller 默认不带 dist-info：冻结后 importlib.metadata 读不到版本号，
        # spike 会自报「未安装」，证据就断了（2026-11-12 首次构建实测）。
        "--copy-metadata",
        "pywebview",
        "--copy-metadata",
        "pythonnet",
        "--copy-metadata",
        "clr-loader",
        *("--console" if not args.windowed else "--windowed",),
    ]
    for module in EXCLUDED_MODULES:
        cmd += ["--exclude-module", module]
    if args.clean:
        cmd.append("--clean")
    cmd.append(str(ENTRY_SCRIPT))
    return cmd


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cmd = pyinstaller_args(args)
    print("[build] 命令：")
    print("  " + " ".join(cmd))
    started = time.monotonic()
    result = subprocess.run(cmd, cwd=str(REPO_ROOT))
    elapsed = time.monotonic() - started
    if result.returncode != 0:
        print(f"[build] FAIL（退出码 {result.returncode}），耗时 {elapsed:.1f}s")
        return result.returncode
    exe = Path(expected_exe(args))
    if not exe.exists():
        print(f"[build] FAIL：PyInstaller 成功但没产出 {exe}")
        return 1
    print(f"[build] PASS 耗时 {elapsed:.1f}s")
    print(f"[build] EXE: {exe}")
    print(f"[build] 目录: {exe.parent}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
