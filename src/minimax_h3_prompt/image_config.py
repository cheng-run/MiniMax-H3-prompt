"""生图（资产图）模型三档分配：读项目根 ``config/image.yaml``（issue #87 / 父 spec #84 D8）。

三档 = 人物／道具／场景，各选一个生图模型（``z_image`` / ``qwen`` / ``moss``）。选中值同时
决定该档出词请求要追加哪一份纪律块（``prompts/image_prompt_disciplines/<模型>.md``）。
默认分配来自 spec #84 D8 的外部调研结论：人物参考与转身板 Qwen 最强、道具与场景 Z-Image
够用且许可最宽松。

**为什么进配置而不写死在节点里**：换模型是这台机器的部署现实（用户经常换本地模型），
写死意味着换一次要改代码。读取口与 ``workflow_config`` / ``library_config`` 同一口径：
缺文件／缺键回落默认（没写不构成走不下去），**写了的非法值大声报**——把模型名拼错却
静默回默认，用户会以为改了、实际一直按旧模型的口径出词，且不报错。

**许可前提是交付红线**：``qwen`` 是 Qwen Research License，**仅限非商用**；商用前把
``character`` 切到 ``z_image`` 或 ``moss``。这条写进默认配置文件的注释里（spec #84 要求）。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

# 项目根目录：src/minimax_h3_prompt/image_config.py -> [0]=包, [1]=src, [2]=项目根。
# 不 import config.PROJECT_ROOT：那会连带实例化模型配置（读 agent.yaml + .env），
# 而本模块只关心这三档分配。
PROJECT_ROOT = Path(__file__).resolve().parents[2]
IMAGE_CONFIG_PATH = PROJECT_ROOT / "config" / "image.yaml"

# 三档资产的键。与 ``generation.KIND_LABELS`` 的键**同一组**（值是那边的中文标签，
# 由 ``nodes`` 逆映射取键），不另立第二套命名。
IMAGE_KINDS = ("character", "prop", "scene")

# 可选模型。值同时是纪律文件名（``prompts/image_prompt_disciplines/<值>.md``）——
# 清单只有这一处定义，纪律读取口与本模块共用，避免「配置认它、纪律文件没有」。
IMAGE_MODEL_KEYS = ("z_image", "qwen", "moss")

# 展示名：终端与产物里说人话用（#88 的出词横幅也取它）。
IMAGE_MODEL_LABELS: Mapping[str, str] = {
    "z_image": "Z-Image Turbo",
    "qwen": "Qwen-Image 2.1",
    "moss": "MOSS-Image",
}

# spec #84 D8 的默认分配：人物 Qwen、道具与场景 Z-Image。
DEFAULT_IMAGE_MODELS: Mapping[str, str] = {
    "character": "qwen",
    "prop": "z_image",
    "scene": "z_image",
}

# 「不带模型信息的调用走哪一档」。issue #87 之前只有 Z-Image 一个生图模型，历史调用点
# （AI 改词等）沿用那次的行为。**它属于模型词表，与 ``IMAGE_MODEL_KEYS`` 同住一处**——
# 让 tools 层另立一份就是第二个家，默认档改名时会有一处静默指向不存在的纪律文件。
DEFAULT_IMAGE_MODEL = "z_image"


@dataclass(frozen=True)
class ImageSettings:
    """一份生效的三档分配：资产类 → 生图模型键。"""

    models: Mapping[str, str]

    def model_for(self, kind: str) -> str:
        """取某一档的模型；未知档报错（与 ``IMAGE_KINDS`` 同一口径）。"""
        if kind not in IMAGE_KINDS:
            raise ValueError(
                f"资产类型无效：{kind!r}（只认 {', '.join(IMAGE_KINDS)}）")
        return self.models[kind]


def image_model_label(model: str) -> str:
    """模型的展示名；未知模型报错——拿不到标签说明值域已经错了，别拿原始键凑合。"""
    try:
        return IMAGE_MODEL_LABELS[model]
    except KeyError:
        raise ValueError(
            f"生图模型无效：{model!r}（只认 {', '.join(IMAGE_MODEL_KEYS)}）") from None


def load_image_settings(path: str | Path | None = None) -> ImageSettings:
    """读一份三档分配；``path`` 为空时读生效路径 ``IMAGE_CONFIG_PATH``。

    生效路径**在调用时**从模块变量取（不缓存、不在 import 时求值）：测试要能把它换到
    临时文件上验证「改配置即改分配」，缓存或提前求值都会让那条守卫变成假的。
    """
    config_path = Path(path) if path is not None else IMAGE_CONFIG_PATH
    raw: Any = {}
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"生图模型配置必须是映射：{config_path}")

    # 拼错的档名（如 characters）若静默略过，用户会以为改了、实际还是默认——所以要报。
    unknown = set(raw) - set(IMAGE_KINDS)
    if unknown:
        raise ValueError(
            f"生图模型配置里有未知字段：{', '.join(sorted(unknown))}"
            f"（只认 {', '.join(IMAGE_KINDS)}）：{config_path}")

    models: dict[str, str] = {}
    for kind in IMAGE_KINDS:
        value = raw.get(kind)
        if value is None:
            models[kind] = DEFAULT_IMAGE_MODELS[kind]
            continue
        if not isinstance(value, str) or not value.strip():
            # 写了就必须是非空字符串：`character:` 留空是「本想改却漏了值」，
            # 静默回默认会把笔误藏起来（同 workflow.yaml 的纪律）。
            raise ValueError(
                f"生图模型配置 {kind} 必须是非空字符串：{value!r}（{config_path}）")
        model = value.strip()
        if model not in IMAGE_MODEL_KEYS:
            raise ValueError(
                f"生图模型配置 {kind} 的值无效：{model!r}"
                f"（只认 {', '.join(IMAGE_MODEL_KEYS)}）：{config_path}")
        models[kind] = model
    return ImageSettings(models)


def image_model_for(kind: str) -> str:
    """某一档当前生效的模型键（每次读盘，见 ``load_image_settings`` 的时点说明）。"""
    return load_image_settings().model_for(kind)


__all__ = [
    "DEFAULT_IMAGE_MODEL", "DEFAULT_IMAGE_MODELS", "IMAGE_CONFIG_PATH", "IMAGE_KINDS",
    "IMAGE_MODEL_KEYS", "IMAGE_MODEL_LABELS", "ImageSettings", "image_model_for",
    "image_model_label", "load_image_settings",
]
