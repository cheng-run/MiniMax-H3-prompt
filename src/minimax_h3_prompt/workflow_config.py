"""工作流配置：登记工作流名与三型参考槽位数（issue #65 / ADR 0017）。

与 ``config/agent.yaml``（模型侧）**分开**：两者生命周期不同——换模型不该误改
槽位，换工作流才改这里。槽位数是**工作流节点的物理端口数**（``ref_image_0…N-1``），
不是模型能力（ADR 0017 实测：模型接受任意长度 ``minimax_refs``，9 只是旧节点的
UI 上限）。故它跟着工作流走：**先有对应端口的工作流，再改这里的数字**。

缺文件/缺字段一律回落默认值（图 24 / 视频 3 / 音频 3）：用户没写配置文件不构成
流程走不下去，与注入卡「不造假端口、把话说破」同口径。**写了的非法值则大声报**
——静默回默认会把笔误藏起来，而接出不存在端口正是 ADR 0017 记录的那类事故。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from .asset_ledger import KIND_AUDIO, KIND_PICTURE, KIND_VIDEO, KINDS

# 项目根目录：src/minimax_h3_prompt/workflow_config.py -> parents[0]=包, [1]=src, [2]=根。
# 不 import config.PROJECT_ROOT：那会连带实例化模型配置（读 agent.yaml + .env），
# 而本模块的测试只需要槽位数——两本账不互相拖累。
PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_CONFIG_PATH = PROJECT_ROOT / "config" / "workflow.yaml"

# V3 是「24 图槽」工作流（票面 #65）；V2 保留在本机作退路，不再被登记。
DEFAULT_REF_WORKFLOW_NAME = "59-(多Lora改)MiniMax-H3 ref2va 多参生视频 V3（24图槽）"
DEFAULT_SLOT_COUNTS: Mapping[str, int] = {
    KIND_PICTURE: 24,
    KIND_VIDEO: 3,
    KIND_AUDIO: 3,
}


@dataclass(frozen=True)
class WorkflowSettings:
    """一份生效的工作流登记：名字 + 三型槽位数（槽位数＝物理端口数）。"""

    ref_workflow_name: str
    slot_counts: Mapping[str, int]

    def slot_count(self, kind: str) -> int:
        """取某一型的端口数；未知类型报错（与 ``KINDS`` 同一口径）。"""
        try:
            return self.slot_counts[kind]
        except KeyError:
            raise ValueError(
                f"参考素材类型无效：{kind!r}（只认 {', '.join(KINDS)}）") from None


def _positive_int(value: Any, key: str) -> int:
    """槽位数只收正整数；bool 是 int 的子类（YAML 的 ``yes/no``），必须单独挡掉。"""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(
            f"slots.{key} 必须是正整数：{value!r}（槽位数＝工作流端口个数）")
    return value


def load_workflow_settings(path: str | Path | None = None) -> WorkflowSettings:
    """读一份工作流配置；``path`` 为空时读生效路径 ``WORKFLOW_CONFIG_PATH``。

    生效路径**在调用时**从模块变量取（不缓存、不在 import 时求值）：测试要能把它
    换到临时文件上验证「改配置即改边界」，缓存或提前求值都会让那条守卫变成假的。
    """
    config_path = Path(path) if path is not None else WORKFLOW_CONFIG_PATH
    raw: Any = {}
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"工作流配置必须是映射：{config_path}")

    name = raw.get("ref_workflow_name")
    if name is None or (isinstance(name, str) and not name.strip()):
        name = DEFAULT_REF_WORKFLOW_NAME
    elif not isinstance(name, str):
        raise ValueError(f"ref_workflow_name 必须是字符串：{name!r}")
    else:
        name = name.strip()

    slots_raw = raw.get("slots")
    if slots_raw is None:
        slots_raw = {}
    if not isinstance(slots_raw, dict):
        # 注意 `slots: []` 是「写了但写错」，不是「没写」——前者要报，后者才回落默认
        raise ValueError(f"slots 必须是映射：{slots_raw!r}")
    unknown = set(slots_raw) - set(KINDS)
    if unknown:
        # 拼错的键（如 pictures）若静默略过，用户会以为自己改了边界、实际用的默认值
        raise ValueError(
            f"slots 里有未知类型：{', '.join(sorted(unknown))}"
            f"（只认 {', '.join(KINDS)}）")
    counts = {
        kind: (_positive_int(slots_raw[kind], kind) if kind in slots_raw
               else DEFAULT_SLOT_COUNTS[kind])
        for kind in KINDS
    }
    return WorkflowSettings(name, counts)


def workflow_settings() -> WorkflowSettings:
    """生效配置（每次读盘，见 ``load_workflow_settings`` 的路径时点说明）。"""
    return load_workflow_settings()
