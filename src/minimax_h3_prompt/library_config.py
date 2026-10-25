"""素材库根目录配置：读项目根 ``config/library.yaml``（issue #85 / spec #84 D1）。

素材不再复制进生成目录的一次性 ``assets/``，而是进**长期素材库**：根目录可配置
（默认 ``D:\\素材库``），子目录按视频主题摘要分。根目录进配置的理由与
``workflow_config`` 同款——它是**机器环境的落点**，不是代码常量：换机器、换盘只改
yaml，改配置即生效（读取口在**调用时**读盘，不缓存、不在 import 时求值，否则
「改配置即改边界」的测试会变成假的）。

缺文件/缺字段回落默认；**写了的非法值大声报**（同 workflow.yaml 纪律）：把库根
拼错却静默回默认，等于用户以为改了、实际一直写进旧目录，而且不报错。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

# 项目根目录：src/minimax_h3_prompt/library_config.py -> [0]=包, [1]=src, [2]=项目根。
# 不 import config.PROJECT_ROOT：那会连带实例化模型配置（读 agent.yaml + .env），
# 而本模块只关心库根这一个路径。
PROJECT_ROOT = Path(__file__).resolve().parents[2]
LIBRARY_CONFIG_PATH = PROJECT_ROOT / "config" / "library.yaml"

# 默认库根（spec #84 D1）。仓库在 Windows 上跑，默认值就用用户机器的落点；
# 别的机器靠 config/library.yaml 覆盖，不改代码。
DEFAULT_LIBRARY_ROOT = Path(r"D:\素材库")


@dataclass(frozen=True)
class LibrarySettings:
    """一份生效的素材库配置：目前只有**根目录**。

    子目录（主题摘要、重跑的 ``-2``）由入库时按运行目录决定，不进配置——
    那是每次运行的产物，不是环境设置。
    """

    root: Path


def require_library_root(value: Any, *, label: str = "素材库根") -> Path:
    """库根的**唯一校验口**：绝对路径、不含 ``..``。

    相对路径会随进程 CWD 漂，同一本台账换个目录启动就找不到图；``..`` 逃逸会让
    撤下/换版操作到库外。三处入口（yaml 配置、台账 ``library_root``、``intake_assets``
    的显式参数）都调它——各写一份的结果本仓见过：其中一处漏挡 ``..``，坏值就从
    那条更宽松的路溜进去（issue #85 复审）。
    """
    if isinstance(value, Path):
        root = value
    elif isinstance(value, str):
        root = Path(value.strip())
    else:
        raise ValueError(f"{label} 必须是字符串或路径：{value!r}")
    if not root.is_absolute():
        raise ValueError(f"{label} 必须是绝对路径：{value!r}")
    if ".." in root.parts:
        raise ValueError(f"{label} 不许含 ..：{value!r}")
    return root


def load_library_settings(path: str | Path | None = None) -> LibrarySettings:
    """读一份素材库配置；``path`` 为空时读生效路径 ``LIBRARY_CONFIG_PATH``。

    生效路径**在调用时**从模块变量取（不缓存、不在 import 时求值）：测试要能把它
    换到临时文件上验证「改配置即改落点」，缓存或提前求值都会让那条守卫变成假的。
    """
    config_path = Path(path) if path is not None else LIBRARY_CONFIG_PATH
    raw: Any = {}
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"素材库配置必须是映射：{config_path}")

    # 拼错的键（如 rooot）若静默略过，用户会以为换了库、实际还写在旧目录——所以要报
    unknown = set(raw) - {"root"}
    if unknown:
        raise ValueError(
            f"素材库配置里有未知字段：{', '.join(sorted(unknown))}（只认 root）")

    root_raw = raw.get("root")
    if root_raw is None or (isinstance(root_raw, str) and not root_raw.strip()):
        # 缺键或空值 ＝ 没写，回落默认；写了的错值走下面报错
        return LibrarySettings(DEFAULT_LIBRARY_ROOT)
    return LibrarySettings(require_library_root(root_raw, label="library.root"))


def library_settings() -> LibrarySettings:
    """生效配置（每次读盘，见 ``load_library_settings`` 的路径时点说明）。"""
    return load_library_settings()


__all__ = [
    "DEFAULT_LIBRARY_ROOT", "LIBRARY_CONFIG_PATH", "LibrarySettings",
    "load_library_settings", "library_settings", "require_library_root",
]
