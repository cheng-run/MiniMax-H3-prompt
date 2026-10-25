"""工作流配置：登记工作流名与三型参考槽位数（issue #65 / ADR 0017）。

与 ``config/agent.yaml``（模型侧）**分开**：两者生命周期不同——换模型不该误改
槽位，换工作流才改这里。槽位数是**工作流节点的物理端口数**（``ref_image_0…N-1``），
不是模型能力（ADR 0017 实测：模型接受任意长度 ``minimax_refs``）。故它跟着工作流走：
**先有对应端口的工作流，再改这里的数字**。

登记值是 **9 图**（issue #86 / spec #84 D5），依据是官方的单次请求上限「≤9 图 +
≤3 视频 + ≤3 音频、总文件 ≤12」，不是节点 UI 的偶然限制；24 槽 V3 已随 issue #90
从仓库退役（本机节点包与工作流保留，但不再登记）。

缺文件/缺字段一律回落默认值（图 9 / 视频 3 / 音频 3）：用户没写配置文件不构成
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

# 登记工作流是 V2 的 **9 图槽**版本（issue #86 / spec #84 D5）：9 是官方 API 的单次请求
# 上限（≤9 图 + ≤3 视频 + ≤3 音频、总文件 ≤12），不是节点 UI 上限。24 槽 V3 从一开始
# 就不该是默认路径——24 张全喂在 16GB 卡上实测 OOM（2026-10-22，见 ADR 0017）。
DEFAULT_REF_WORKFLOW_NAME = "59-(多Lora改)MiniMax-H3 ref2va 多参生视频 V2"
DEFAULT_SLOT_COUNTS: Mapping[str, int] = {
    KIND_PICTURE: 9,
    KIND_VIDEO: 3,
    KIND_AUDIO: 3,
}

# 每个执行段默认喂几张参考图（issue #75 / ADR 0017）：24 张全喂时 Qwen3-VL 序列约
# 5.4 万 token，16GB 卡只剩朴素注意力 kernel，`einsum` 要 232 GiB（2026-10-22 实测
# OOM）。9 是社区调度器（`SceneDispatcher2`）与旧节点 UI 上限同款的每段值——作为
# **每段默认值**合理，作为**全局容量**才是错的（ADR 0017 裁定）。写进配置文件后
# 换工作流/换显存只改这里。
DEFAULT_REFS_PER_SEGMENT = 9


@dataclass(frozen=True)
class WorkflowSettings:
    """一份生效的工作流登记：名字 + 三型槽位数＋每段参考图上限（issue #75）。

    ``refs_per_segment`` 与槽位数是两个**不同**的边界：槽位数是工作流端口数（全局
    容量），每段上限是单次生成真正喂几张图（显存/序列长度决定）。ADR 0017 的原话是
    「9 作为每段默认值仍然合理，作为全局容量才是错的」——两个数因此各占一个字段。
    """

    ref_workflow_name: str
    slot_counts: Mapping[str, int]
    refs_per_segment: int = DEFAULT_REFS_PER_SEGMENT

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
    per_segment = raw.get("refs_per_segment")
    if per_segment is None:
        refs_per_segment = DEFAULT_REFS_PER_SEGMENT
    else:
        # 写了就必须是正整数：`0`/`false`/`"9"` 都会被 _positive_int 挡下——
        # 静默回默认会把「本想限制却打错字」藏起来（同 slots 的纪律）。
        refs_per_segment = _positive_int(per_segment, "refs_per_segment")
    return WorkflowSettings(name, counts, refs_per_segment)


def workflow_settings() -> WorkflowSettings:
    """生效配置（每次读盘，见 ``load_workflow_settings`` 的路径时点说明）。"""
    return load_workflow_settings()
