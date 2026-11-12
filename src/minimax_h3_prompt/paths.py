"""唯一路径解析模块（#237 / spec #235 §6）：只读资源 vs 可写数据。

**两类根必须分开**（spec §6 的裁定）：

- **只读资源**（前端构建产物、内置配置模板、``prompts`` / ``knowledge`` 等随包进 EXE
  的文件）：冻结态在 PyInstaller 的解包目录 ``sys._MEIPASS``，开发态是仓库根。
- **可写数据**（会话、产物、日志、配置）：冻结态是 **EXE 同级的 ``data/``**，开发态是
  仓库根（历史布局，既有测试与用户目录一品不变）。

为什么要有这一个模块：冻结后包安装位置换成临时解包目录，``api/spa.py`` 与
``config.py`` 那几处 ``Path(__file__).parents[N]`` 会指到不存在的位置（spec §6 的表）。
散装推导因此全部收口到这里，``tests/test_path_root_guard.py`` 用结构守卫咬住「不许
再冒出第二处根路径推导」。

**为什么冻结态会话根是 ``data/sessions`` 而不是 ``data/output/sessions``**：spec §4 的
交付目录树把 ``sessions`` 与 ``output`` 并列为 ``data/`` 下的同级目录（前者是会话仓库，
后者是产物与阶段落盘）；开发态的 ``output/sessions`` 是历史布局，跟着冻结态改会动到
用户机器上已有的会话目录。两条口径都写在 :func:`sessions_root` 里，别以为哪边是笔误。

**本模块必须轻量**：不 import ``config``（那会连带实例化模型配置、读 YAML 与 ``.env``），
也不读任何文件。``image_config`` / ``library_config`` / ``workflow_config`` 原先各复制
一份 ``parents[2]`` 正是为了避开这条重依赖。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# 开发态仓库根：``src/minimax_h3_prompt/paths.py`` -> parents[0]=包, [1]=src, [2]=仓库根。
# 这是**全仓唯一**允许写 ``parents[2]`` 的地方（结构守卫按包内深度判定）。
_DEV_REPO_ROOT = Path(__file__).resolve().parents[2]

# 冻结态可写数据根在 EXE 同级的目录名（spec §4 目录树）。
DATA_DIR_NAME = "data"

# 冻结态用户配置目录名：``%APPDATA%\MiniMax-H3-prompt\``（spec #235 §4/§7 的密钥落点）。
# 大小写与 spec 原文一致，别改成包名 ``minimax_h3_prompt``——那是 Python 包标识。
APP_DIR_NAME = "MiniMax-H3-prompt"
ENV_FILE_NAME = ".env"

# 开发态默认会话根（历史布局）。与冻结态的 ``sessions`` 并列解释见模块 docstring。
DEV_SESSIONS_RELATIVE = Path("output") / "sessions"
FROZEN_SESSIONS_RELATIVE = Path("sessions")

# ``--data-root`` 的覆盖值（``None`` = 按形态取默认根）。模块级是因为它必须在
# ``import config`` 之前就生效，而 consumer 是模块级单例（见 :func:`set_data_root`）。
_data_root_override: Path | None = None


def is_frozen() -> bool:
    """是否运行在冻结形态（PyInstaller 打包的 EXE 里）。

    PyInstaller 在启动时给 ``sys.frozen`` 赋值；开发态该属性不存在。每次调用都现读，
    不用模块级常量——否则用例没法模拟冻结态（``monkeypatch.setattr(sys, "frozen", True)``）。
    """
    return bool(getattr(sys, "frozen", False))


def resource_root() -> Path:
    """只读资源根：冻结态 = 解包目录 ``sys._MEIPASS``，开发态 = 仓库根。"""
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if not meipass:
            raise RuntimeError(
                "冻结形态下读不到 sys._MEIPASS：PyInstaller 没把它交给本次进程，"
                "只读资源（前端 dist、内置模板）无法定位。")
        return Path(meipass)
    return _DEV_REPO_ROOT


def data_root() -> Path:
    """可写数据根：冻结态 = EXE 同级的 ``data/``，开发态 = 仓库根。

    ``--data-root`` 覆盖优先（:func:`set_data_root`）。返回绝对路径：会话标识是
    ``sessions_root`` 之下的相对路径，根不是绝对的会让 ``_resolve`` 的越界判断失真。
    """
    if _data_root_override is not None:
        return _data_root_override
    if not is_frozen():
        return _DEV_REPO_ROOT
    return Path(sys.executable).resolve().parent / DATA_DIR_NAME


def set_data_root(path: str | Path | None) -> None:
    """设置 / 清除数据根覆盖（``--data-root``，spec §6「验收与调试用」）。

    **必须在 import ``config`` 之前调用**：``config = Config()`` 是模块级单例，import 时
    一口气算完 ``CONFIG_PATH`` / ``sessions_root`` / ``output_path``（``config.py``），
    迟到的覆盖不会回头修改已经算好的值。启动入口因此在任何业务 import 之前解析 argv。
    """
    global _data_root_override
    if path is None or not str(path).strip():
        _data_root_override = None
        return
    _data_root_override = Path(path).expanduser().resolve()


def resource_path(*parts: str) -> Path:
    """只读资源根下的路径（前端 dist、内置模板、``prompts`` / ``knowledge``）。"""
    return resource_root().joinpath(*parts)


def frontend_dist_dir() -> Path:
    """前端构建产物目录。

    冻结态要求打包脚本把 ``frontend/dist`` 收进解包目录的 ``frontend/dist``
    （PyInstaller ``--add-data frontend/dist;frontend/dist``，spec §9）；打包流水线在
    D5-6，本票只保证解析口一致。
    """
    return resource_path("frontend", "dist")


def bundled_config_dir() -> Path:
    """随包发布的内置配置模板目录（首次启动复制到 ``data/config/`` 的来源）。"""
    return resource_path("config")


def bundled_config_path(name: str) -> Path:
    """内置配置模板文件（``agent.yaml`` / ``image.yaml`` / …）。"""
    return bundled_config_dir() / name


def config_dir() -> Path:
    """生效配置目录：可写数据根下的 ``config/``（开发态即仓库的 ``config/``）。"""
    return data_root() / "config"


def config_path() -> Path:
    """生效的模型配置（``agent.yaml``）。冻结态在 ``data/config/``，首次启动由 D5-3 复制模板。"""
    return config_dir() / "agent.yaml"


def env_file_path() -> Path:
    """密钥文件（``.env``）：冻结态 = ``%APPDATA%\\MiniMax-H3-prompt\\.env``，开发态 = 仓库根 ``.env``。

    **为什么冻结态不落 ``data/``**（spec #235 §4）：作品目录会被用户整个拷走或分享，
    密钥跟着走等于随作品泄露。开发态保持仓库根 ``.env`` 是**刻意的**——本机开发与
    既有用例都按历史位置读，改成 APPDATA 会让每台开发机凭空多一份要手工同步的副本
    （spec #235 §14「开发形态保持现状」）。

    **不受 ``--data-root`` 影响**：数据根挪的是作品与配置，密钥属于「这台机器的人」，
    不跟着某个数据目录走。
    """
    if not is_frozen():
        return _DEV_REPO_ROOT / ENV_FILE_NAME
    appdata = os.environ.get("APPDATA", "").strip()
    # APPDATA 缺失（服务账号、被精简的环境）时回退 Windows 的常规漫游目录；
    # 绝不回退数据根——那正好是把密钥放回作品目录里。
    base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    return base / APP_DIR_NAME / ENV_FILE_NAME


def image_config_path() -> Path:
    """生图模型分配配置（``image.yaml``）。"""
    return config_dir() / "image.yaml"


def workflow_config_path() -> Path:
    """工作流槽位配置（``workflow.yaml``）。"""
    return config_dir() / "workflow.yaml"


def library_config_path() -> Path:
    """素材库根配置（``library.yaml``）。"""
    return config_dir() / "library.yaml"


def data_path(relative: str | Path) -> Path:
    """数据根下的路径。给配置里那些**相对仓库根**写的值用（``output/stages`` 等）。"""
    return data_root() / Path(relative)


def output_dir() -> Path:
    """产物与阶段落盘目录（spec §4 的 ``data/output``）。"""
    return data_root() / "output"


def logs_dir() -> Path:
    """运行日志目录（spec §4 的 ``data/logs``）。

    本票只把路径暴露出来；按日滚动与 FileHandler 在 D5-4（#239），别在这里提前接线。
    """
    return data_root() / "logs"



def sessions_root(configured: str | Path | None = None) -> Path:
    """会话根。

    - 冻结态：恒为 ``<data_root>/sessions``（spec §4 目录树）。``agent.yaml`` 里的
      ``defaults.sessions_root`` 是**开发态历史布局**（``output/sessions``），冻结态不跟随；
      要把数据挪别处请用 ``--data-root``（spec §6「数据根可被覆盖」）。
    - 开发态：``configured`` 非空时按数据根的相对路径解析，为空时 ``output/sessions``。

    为什么不在冻结态也认 YAML 里的相对值：那是仓库内路径的写法，冻结后又没有仓库，
    跟着它走会得到 ``data/output/sessions``——与 spec §4 并列的 ``data/sessions`` 冲突。
    """
    if is_frozen():
        return data_root() / FROZEN_SESSIONS_RELATIVE
    if configured is None or not str(configured).strip():
        return data_root() / DEV_SESSIONS_RELATIVE
    return data_root() / Path(configured)
