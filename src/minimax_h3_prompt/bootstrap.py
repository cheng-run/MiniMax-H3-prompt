"""首次启动的配置骨架：把内置模板复制进可写配置目录（D5-3 / #238，spec #235 §7）。

**为什么需要它**：冻结形态第一次启动时 ``data/config/`` 是空的，而 ``config = Config()``
是 import 期单例、读不到 ``agent.yaml`` 会直接抛错——用户双击 EXE 只能看到白屏或一段
traceback。所以在任何业务 import 之前先把骨架补上。

**时机是硬约束**：``Config()`` 在 import 时就把 YAML 读完了，补文件必须发生在
``from .app import create_app`` 之前（见 ``api/__main__.py`` 的 ``start``）。

**两条不变量**：

- **只补缺**：目标文件已存在就一个字节都不动——用户改过的配置不能被模板盖回去；
- **可以反复跑**：开发态配置目录就是仓库 ``config/``（目标都已在），因此是空操作。
"""
from __future__ import annotations

import shutil
from pathlib import Path

from . import paths

# 随包发布的四份配置（spec #235 §4 的目录树只画了 ``agent.yaml``，另外三份是同一层
# 语义：``image.yaml`` 生图档、``workflow.yaml`` 工作流槽位、``library.yaml`` 素材库根）。
# 清单住在这里而不是散在各读取口：首次启动一次补齐，别让用户先踩到「缺 image.yaml」
# 才发现少了一份。
CONFIG_TEMPLATE_NAMES: tuple[str, ...] = (
    "agent.yaml",
    "image.yaml",
    "workflow.yaml",
    "library.yaml",
)


class ConfigBootstrapError(RuntimeError):
    """内置模板缺失或写盘失败：启动入口据此给可操作提示并以失败码退出。"""


def ensure_config_files() -> list[Path]:
    """把缺失的配置模板复制到 :func:`paths.config_dir`，返回本次新写入的文件。

    已存在的文件跳过（不覆盖）；一份都不缺时返回空列表且**不建目录**——开发态因此
    连副作用都没有。模板缺失或写盘失败抛 :class:`ConfigBootstrapError`，文案点名
    「缺哪个文件 / 要写到哪」，不裸抛 ``OSError``。
    """
    target_dir = paths.config_dir()
    pending: list[tuple[Path, Path]] = []
    for name in CONFIG_TEMPLATE_NAMES:
        target = target_dir / name
        if target.exists():
            continue
        source = paths.bundled_config_path(name)
        if not source.is_file():
            # 打包漏收模板是最可能的成因（PyInstaller 的 --add-data），所以文案直接
            # 指向「从哪来的、该去哪」，而不是只说「文件不存在」。
            raise ConfigBootstrapError(
                f"内置配置模板缺失：{source} 不存在，无法在 {target_dir} 生成 {name}。"
                "这通常意味着打包时没把 config/ 收进发行目录；开发态请确认仓库 config/ 完整。")
        pending.append((source, target))

    if not pending:
        return []

    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ConfigBootstrapError(
            f"无法创建配置目录 {target_dir}：{exc}。请检查该目录是否可写"
            "（程序目录被放在只读位置时也会这样），或换一个可写目录再启动。") from exc

    created: list[Path] = []
    for source, target in pending:
        try:
            shutil.copyfile(source, target)
        except OSError as exc:
            raise ConfigBootstrapError(
                f"复制配置模板失败：{source} → {target}：{exc}。"
                f"请确认 {target_dir} 可写后重启程序。") from exc
        created.append(target)
    return created
