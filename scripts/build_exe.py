"""D5-6（#241）打包流水线：一条命令「前端构建 → PyInstaller 打包 → 产出 zip」。

为什么输出目录叫 ``build-exe/`` 而不是 ``dist/``：Vite 的前端产物就在
``frontend/dist``，PyInstaller 默认的 ``dist`` 会与之同名互相覆盖（spec #235 §9）。

用法（仓库根，Windows）：

    python scripts/build_exe.py              # 干净重建
    python scripts/build_exe.py --no-clean   # 复用 PyInstaller 缓存

产物：``build-exe/MiniMax-H3-prompt/``（onedir）与 ``build-exe/MiniMax-H3-prompt.zip``。
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"
SCRIPTS_DIR = REPO_ROOT / "scripts"

# 包内资源清单的唯一来源在包内 ``selfcheck``（冻结自检也读它）。脚本自己再抄一份
# 就会「打包收了、自检不查」或反过来（#243 两轴复审）。``scripts/`` 不是包，直接
# 跑脚本时把仓库 ``src`` 补进导入路径，别依赖调用者已经设好 ``PYTHONPATH``。
_SRC = REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from minimax_h3_prompt.selfcheck import PACKAGE_DATA_DIRS  # noqa: E402  先补路径再导入

# 交付目录名与 EXE 名同源（spec §4 的目录树）。
DIST_NAME = "MiniMax-H3-prompt"
PRODUCT_NAME = "MiniMax H3 Prompt"
BUILD_DIR_NAME = "build-exe"

# PyInstaller 的入口。**独立三行脚本**而不是直接指 ``src/.../desktop.py``：
# ``--self-check`` 这类「打包与 CI 专用」的开关不该长进业务外壳（#240）的公开
# 命令行里，入口脚本是这类关注点的唯一落点。
ENTRY_SCRIPT = SCRIPTS_DIR / "exe_entry.py"

# 包内非 py 资源只有上面 ``PACKAGE_DATA_DIRS`` 那几处（33 个 md + 2 个 txt）；模块靠
# ``Path(__file__).parent`` 定位它们，所以数据必须落在**同样的相对路径**，不能拍平。
# 清单本身不在这里定义（见文件顶部的 import）。

# 没装的 GUI 后端：pywebview 会在运行时探测它们，PyInstaller 的钩子却可能把整棵
# 依赖树拖进来（spec §9「可选后端显式 excludes」）。
EXCLUDED_MODULES = ("PyQt5", "PyQt6", "PySide2", "PySide6", "gtk", "gi", "cefpython3")

# ``from .api import __main__ as entry`` 这种「包里的 __main__ 子模块」PyInstaller
# 的分析器不会自动收（它把 ``__main__`` 当特殊名）——冻结后启动即
# ``ImportError: cannot import name '__main__'``（2026-11-12 首次实跑抓到）。
HIDDEN_IMPORTS: tuple[str, ...] = (
    "minimax_h3_prompt.api.__main__",
)

# 冻结后 ``importlib.metadata`` 要读的 dist-info。第一项是本包自己：
# ``api/app.py::_resolve_version`` 只认 ``minimax-h3-prompt`` 的元数据，漏了 EXE 起不来。
METADATA_PACKAGES: tuple[str, ...] = (
    "minimax-h3-prompt", "pywebview", "pythonnet", "clr-loader",
)

_VERSION_RE = re.compile(r"^\d+(?:\.\d+){1,3}$")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="build_exe",
        description="一条命令产出可分发目录与 zip：前端构建 → PyInstaller → 压缩。",
    )
    parser.add_argument(
        "--build-root",
        default=str(REPO_ROOT / BUILD_DIR_NAME),
        help=f"构建根目录，产物落 <build-root>/{DIST_NAME}/",
    )
    parser.add_argument(
        "--no-clean",
        dest="clean",
        action="store_false",
        help="不清 PyInstaller 缓存（默认清，保证可复现）",
    )
    parser.add_argument(
        "--skip-frontend",
        action="store_true",
        help="跳过前端构建（仅在 dist 已存在时调试用；CI 与发布不要用）",
    )
    return parser.parse_args(argv)


def dist_dir(args: argparse.Namespace) -> str:
    """onedir 交付目录：``<build-root>/MiniMax-H3-prompt``。

    绝不能叫 ``dist``——Vite 的产物就在 ``frontend/dist``，同名会互相覆盖（spec §9）。
    """
    return str(Path(args.build_root) / DIST_NAME)


def expected_exe(args: argparse.Namespace) -> str:
    return str((Path(dist_dir(args)) / f"{DIST_NAME}.exe").resolve())


def zip_path(args: argparse.Namespace) -> str:
    """与目录同名的 zip（``build-exe/MiniMax-H3-prompt.zip``）。"""
    return str((Path(args.build_root) / f"{DIST_NAME}.zip").resolve())


def _add_data_arg(source: Path, destination: str) -> str:
    """PyInstaller 的 ``SRC;DEST`` 形态；分隔符取平台的 ``os.pathsep``。

    源路径用**绝对路径**：``--add-data`` 的相对路径是相对 ``--specpath`` 那个目录
    解析的，不是相对 cwd——写成相对路径会得到 ``build-exe/spec/frontend/dist``
    （2026-11-12 首次构建实测 ERROR）。
    """
    return f"{source}{os.pathsep}{destination}"


def pyinstaller_args(
    args: argparse.Namespace, *, version_file: Path, ffmpeg_path: Path,
) -> list[str]:
    """拼出交给 PyInstaller 的完整命令。纯函数：不碰文件系统、不跑构建。"""
    build_root = Path(args.build_root)
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--onedir",
        "--name", DIST_NAME,
        "--distpath", str(build_root),
        "--workpath", str(build_root / "work"),
        "--specpath", str(build_root / "spec"),
        # 真实交付形态：不带控制台窗口（spike 的 --console 只是取证需要）。
        "--windowed",
        "--version-file", str(version_file),
        # 前端构建产物：冻结后由 paths.frontend_dist_dir() 在 _MEIPASS 下按同一相对
        # 路径找到它；漏了就是 503「请先构建前端」。
        "--add-data", _add_data_arg(REPO_ROOT / "frontend" / "dist", "frontend/dist"),
        # 四个配置模板：首次启动由 bootstrap 复制进 data/config/。
        "--add-data", _add_data_arg(REPO_ROOT / "config", "config"),
        # 包内资源：模块按 ``Path(__file__).parent / "prompts"`` 定位，因此落点必须
        # 与包内相对路径一致。
        *[
            part
            for name in PACKAGE_DATA_DIRS
            for part in (
                "--add-data",
                _add_data_arg(REPO_ROOT / "src" / "minimax_h3_prompt" / name,
                              f"minimax_h3_prompt/{name}"),
            )
        ],
        # 自己的 dist-info 必须在包里（``_resolve_version``），其余三个是 pywebview
        # 在冻结后自报版本用的。
        *[part for package in METADATA_PACKAGES
          for part in ("--copy-metadata", package)],
        # ffmpeg：imageio_ffmpeg 运行时按 ``__file__`` 同级 ``binaries/`` 找可执行文件，
        # 所以落到包内同相对路径（``tools/seam_audit.py::find_ffmpeg`` 依赖它）。
        "--add-binary",
        f"{ffmpeg_path}{os.pathsep}imageio_ffmpeg/binaries",
        # hidden imports：每一条都由冻结后实跑报错驱动，不凭猜堆一串。
        *[part for module in HIDDEN_IMPORTS for part in ("--hidden-import", module)],
    ]
    for module in EXCLUDED_MODULES:
        cmd += ["--exclude-module", module]
    if args.clean:
        cmd.append("--clean")
    cmd.append(str(ENTRY_SCRIPT))
    return cmd


def read_version(pyproject: Path = PYPROJECT) -> str:
    """从 ``pyproject.toml`` 读版本号。

    只认这一份真源：版本再抄一处（脚本常量、EXE 名里的字面量），改 pyproject 时
    另一处会静默留在旧值，EXE 属性与诊断包各说各话。
    """
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    version = str(data["project"]["version"])
    if not _VERSION_RE.match(version):
        raise ValueError(f"pyproject.toml 的版本号读不懂：{version!r}（要形如 0.1.0）")
    return version


def version_tuple(version: str, *, length: int = 4) -> tuple[int, ...]:
    """``0.1.0`` → ``(0, 1, 0, 0)``：Windows 版本资源固定四段，缺的补零。"""
    parts = [int(part) for part in version.split(".")]
    if len(parts) > length:
        raise ValueError(f"版本号超过 {length} 段：{version!r}")
    return tuple(parts + [0] * (length - len(parts)))


def version_file_text(version: str) -> str:
    """PyInstaller ``--version-file`` 的内容（Windows 版本资源）。

    ``FileVersion`` / ``ProductVersion`` 用点分串，``filevers`` / ``prodvers`` 用四段
    整数元组——两边都写，文件属性里才既好看又可机器比较。
    """
    nums = version_tuple(version)
    return f"""# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={nums!r},
    prodvers={nums!r},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040904B0',
        [StringStruct('CompanyName', '{PRODUCT_NAME}'),
         StringStruct('FileDescription', '{PRODUCT_NAME}'),
         StringStruct('FileVersion', '{version}'),
         StringStruct('InternalName', '{DIST_NAME}'),
         StringStruct('OriginalFilename', '{DIST_NAME}.exe'),
         StringStruct('ProductName', '{PRODUCT_NAME}'),
         StringStruct('ProductVersion', '{version}')])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""


# --------------------------------------------------------------- 交付目录里的 README

def readme_text(version: str) -> str:
    """交付目录里的 ``README.txt``：首次运行唯一的人类说明书。

    三件事必须写全（spec #235 §4）：**SmartScreen 的点击路径**（不签名必然遇到）、
    **密钥文件在哪、长什么样**（``%APPDATA%`` 下、不随作品目录被拷走）、
    **实际端口在哪看**（窗口标题与当天的日志——随机端口，用户不可能猜）。
    """
    return f"""MiniMax H3 Prompt {version} — 首次运行说明

1) 解压与启动
   把整个压缩包解压到一个可写的位置（例如桌面），保持目录结构完整，双击
   {DIST_NAME}.exe。不要只把 exe 单独拷出来——_internal/ 是它的运行时。

2) Windows 首次运行提示（SmartScreen）
   本程序没有做代码签名，Windows 第一次会提示「Windows 已保护你的电脑」。
   点「更多信息」→「仍要运行」即可。之后不会再提示。

3) 配置密钥
   在 %APPDATA%\\MiniMax-H3-prompt\\ 目录下新建 .env 文件（完整路径：
   %APPDATA%\\MiniMax-H3-prompt\\.env），按实际使用的模型服务写一行或几行，例如：
       DASHSCOPE_API_KEY=sk-你的密钥
       DEEPSEEK_API_KEY=sk-你的密钥
       OPENAI_API_KEY=sk-你的密钥
       ANTHROPIC_API_KEY=sk-你的密钥
   密钥只从该文件读取，不会写进程序目录；拷贝作品目录给别人不会带走密钥。

4) 端口在哪看
   程序会随机挑一个空闲端口。打开的窗口标题里带着「端口 12345」；当天的日志在
   data\\logs\\ 下，启动行同样记录地址与端口。需要固定端口时用命令行：
       {DIST_NAME}.exe --port 8000

5) 数据目录
   程序运行时会在 exe 同级创建 data\\：
       data\\config\\  配置（首次启动自动生成 agent.yaml 等四份）
       data\\sessions\\ 会话
       data\\output\\  产物
       data\\logs\\    日志
   备份或迁移时整个 data\\ 目录一起拷走即可。

6) 依赖
   界面使用系统自带的 Microsoft Edge WebView2 运行时。Windows 11 一般已预装；
   若提示缺少，请到 https://developer.microsoft.com/microsoft-edge/webview2/
   下载「Evergreen Standalone Installer」安装后重试。

自检（排障用）：命令行运行 {DIST_NAME}.exe --self-check，会在 --data-root 指定的
目录（默认临时目录）下的 selfcheck.log 写出冻结环境、ffmpeg 与桩管线阶段 1/2 的检查结果。
"""


def write_readme(dist: Path, version: str) -> Path:
    """把 README.txt 写进交付目录（不是 EXE 内部）；返回文件路径。"""
    path = Path(dist) / "README.txt"
    path.write_text(readme_text(version), encoding="utf-8")
    return path


# --------------------------------------------------------------------- 打包与压缩

# 运行时才建的数据目录与密钥绝不进 zip：解压即用，但不带走别人的会话与 key。
ZIP_EXCLUDED_TOP_LEVEL = frozenset({"data", "logs", "output"})
ZIP_EXCLUDED_NAMES = frozenset({".env"})


def zip_entries(dist: Path) -> list[tuple[Path, str]]:
    """``(源文件, 包内路径)`` 列表；顶层保留 ``MiniMax-H3-prompt/``，解压即得目录。"""
    dist = Path(dist)
    entries: list[tuple[Path, str]] = []
    for path in sorted(dist.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(dist)
        if relative.parts[0] in ZIP_EXCLUDED_TOP_LEVEL:
            continue
        if relative.name in ZIP_EXCLUDED_NAMES:
            continue
        entries.append((path, f"{dist.name}/{relative.as_posix()}"))
    return entries


def create_zip(dist: Path, archive: Path) -> Path:
    """压缩交付目录；已存在的同名 zip 先删，避免旧成员混进新包。"""
    archive = Path(archive)
    archive.parent.mkdir(parents=True, exist_ok=True)
    if archive.exists():
        archive.unlink()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for source, arcname in zip_entries(dist):
            zf.write(source, arcname)
    return archive


# ------------------------------------------------------------------- 构建各步骤

class BuildError(RuntimeError):
    """构建流水线的可预期失败（文案直接给人看，不裸抛 traceback）。"""


def frontend_build_command() -> list[str]:
    """前端构建命令：走仓库已装的 vite，不联网。

    用 ``npx --no-install`` 而不是 ``pnpm``：本机与 CI 的 pnpm 来源不同（本机没有
    全局 pnpm），而 ``node_modules/.bin/vite`` 是两边装完依赖后都有的那一个入口。
    """
    npx = shutil.which("npx")
    if npx is None:
        raise BuildError("找不到 npx：请先安装 Node.js，并在 frontend/ 下安装依赖。")
    return [npx, "--no-install", "vite", "build"]


def build_frontend(run=subprocess.run) -> Path:
    """在 ``frontend/`` 里跑生产构建；产物必须落在 ``frontend/dist``。"""
    cmd = frontend_build_command()
    print("[build] 前端构建：" + " ".join(cmd))
    result = run(cmd, cwd=str(REPO_ROOT / "frontend"))
    if result.returncode != 0:
        raise BuildError(f"前端构建失败（退出码 {result.returncode}）")
    dist = REPO_ROOT / "frontend" / "dist"
    if not (dist / "index.html").is_file():
        raise BuildError(f"前端构建成功但 {dist / 'index.html'} 不存在")
    return dist


def resolve_ffmpeg() -> Path:
    """imageio-ffmpeg 自带的 ffmpeg 可执行文件（不是 PATH 上那个）。"""
    import imageio_ffmpeg

    path = Path(imageio_ffmpeg.get_ffmpeg_exe())
    if not path.is_file():
        raise BuildError(f"imageio-ffmpeg 报告的 ffmpeg 不存在：{path}")
    return path


def prepare_version_file(build_root: Path, version: str) -> Path:
    path = Path(build_root) / "version_info.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(version_file_text(version), encoding="utf-8")
    return path


def verify_bundle(dist: Path, ffmpeg_path: Path) -> None:
    """PyInstaller 成功 ≠ 资源齐了：显式收集的每一项都点名核对。

    这几项漏了都不会让构建失败（只会让冻结程序白屏、起不来或找不到 ffmpeg），
    所以在构建阶段就把「少东西」变成红。
    """
    dist = Path(dist)
    internal = dist / "_internal"
    required = {
        "EXE": dist / f"{DIST_NAME}.exe",
        "前端 index.html": internal / "frontend" / "dist" / "index.html",
        "配置模板 agent.yaml": internal / "config" / "agent.yaml",
        # 从常量派生而不是逐个手写：新增一个资源目录时这里自动跟着核对（#243 复审）。
        **{
            f"包内资源 {name}": internal / "minimax_h3_prompt" / name
            for name in PACKAGE_DATA_DIRS
        },
        "ffmpeg": internal / "imageio_ffmpeg" / "binaries" / Path(ffmpeg_path).name,
    }
    missing = [name for name, path in required.items() if not path.exists()]
    if missing:
        raise BuildError("打包结果缺少：" + "、".join(missing))


def _force_utf8_stdio() -> None:
    """让构建日志在 GBK 控制台也不乱码（与 ``api.__main__`` 同一条纪律）。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):  # pragma: no cover - 非常规流
            pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdio()
    args = parse_args(argv)
    build_root = Path(args.build_root)
    started = time.monotonic()
    try:
        version = read_version()
        print(f"[build] 版本：{version}（来自 pyproject.toml）")
        if not args.skip_frontend:
            build_frontend()
        version_file = prepare_version_file(build_root, version)
        ffmpeg_path = resolve_ffmpeg()
        cmd = pyinstaller_args(args, version_file=version_file, ffmpeg_path=ffmpeg_path)
        print("[build] PyInstaller 命令：")
        print("  " + " ".join(cmd))
        result = subprocess.run(cmd, cwd=str(REPO_ROOT))
        if result.returncode != 0:
            raise BuildError(f"PyInstaller 失败（退出码 {result.returncode}）")
        dist = Path(dist_dir(args))
        verify_bundle(dist, ffmpeg_path)
        write_readme(dist, version)
        archive = create_zip(dist, Path(zip_path(args)))
    except BuildError as exc:
        print(f"[build] FAIL：{exc}")
        return 1
    except Exception as exc:  # noqa: BLE001 - 构建脚本用退出码收场，不裸抛
        print(f"[build] FAIL：{exc}")
        return 1
    elapsed = time.monotonic() - started
    print(f"[build] PASS 耗时 {elapsed:.1f}s")
    print(f"[build] 目录：{dist}")
    print(f"[build] zip ：{archive}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

