"""项目级剧本与提示词生成结果模型、持久化和人工查看适配。"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, NamedTuple

from .asset_proofread import proofread_descriptions
from .brief_parser import Brief, CanvasMap, RefItem, is_ref_mode
from .tools.theme_guard import (
    _CONFLICTING_OUTDOOR_TERMS,
    contains_unqualified_conflict as _contains_unqualified_conflict,
    scene_requirement_groups,
)
from .workflow_config import workflow_settings

PromptKind = Literal["script", "video", "character", "prop", "scene"]
_PROMPT_KINDS = ("script", "video", "character", "prop", "scene")
_GENERATION_ID = re.compile(r"^GEN\d{3}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

# 多参（ref2va）路线的登记工作流名。**唯一可写来源是 config/workflow.yaml**
# （issue #41 AC6 → issue #65 改为配置派生）：产物/菜单/注入卡的指称都要对得上
# 配置里那一行（2026-10-25 起是 V2 的 9 图槽版本，issue #86），两处各写一份就会漂成
# 两个名字（用户按名字进 ComfyUI 找不到工作流）。这里只是给既有 import 面留的只读
# 别名；文档引文与它的一致性由守卫测试 ``tests/test_ref_route_prompts.py`` 钉住；
# 旧称「工作流 63」已废弃。
# 别名**不在 import 时求值**（复审 S4）：模块级赋值会把名字钉死在首次加载那一刻，
# 换一份 workflow.yaml 后菜单/登记行仍指旧工作流而不报错。PEP 562 的模块
# ``__getattr__`` 让每次取用都现读配置——别名保留，语义与注入卡同源。

# 兼容别名的名字（``__all__`` 与 ``__getattr__`` 共用，避免两处各写一份字面量）。
_REF_WORKFLOW_ALIAS = "REF_WORKFLOW_NAME"


def __getattr__(name: str):
    """PEP 562：取 ``REF_WORKFLOW_NAME`` 时现读生效配置（复审 S4）。"""
    if name == _REF_WORKFLOW_ALIAS:
        return workflow_settings().ref_workflow_name
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class ImageProfile(NamedTuple):
    """生图 profile 默认表的一行。

    用具名字段而不是裸元组：这张表现在有多个消费者（渲染层、「推荐尺寸」、帧节点的画布、
    ``FL2VAFramePrompt`` 的字段默认值），按下标取值时「第 3 位是宽还是高」只写在注释里，
    错位了不会有任何东西报红（issue #33 复审：写测试时确实被这个下标绊过一次）。
    """

    profile_id: str
    prompt_node_id: str
    width: int
    height: int
    status: str


# 生图 profile 默认表：模型 → 该模型在本仓的默认工作流参数。
# 它以前只在渲染层被读（给人看「推荐尺寸」）；帧节点的请求现在也从这里取画布
# （``image_canvas_size``，issue #33）——两处各写一份数就会漂，所以是**公开的单一来源**。
# Flux.2 行已删（issue #48，ADR 0013 全链退役）：选择器、【画布】块、帧产物都随这张表
# 自动收缩，家族清单不另抄第二份。旧产物/载荷里的 flux2 键走**弃读**（见
# ``RETIRED_IMAGE_FAMILIES``），不做静默兼容。
IMAGE_PROFILE_DEFAULTS: dict[str, ImageProfile] = {
    "zimage": ImageProfile("zimage_t2i_v1", "10", 640, 1280, "candidate"),
}
# 已退役的生图模型家族（issue #48）：**只用于读旧数据时的弃读**——旧产物/载荷里
# 的 flux2 键/行照常载入但不产出、不渲染（修订基线同款，见 `user_revisions`）；
# 活输入（brief 画布键、直接构造）则照旧报「未知生图模型/生图模型类型无效」，两者不混。
RETIRED_IMAGE_FAMILIES = frozenset({"flux2"})


def is_retired_image_family(name: object) -> bool:
    """该家族名是否点名了已退役家族——三处弃读口共用的**唯一**谓词（issue #48）。

    缺省/空值按现行默认家族 zimage 处理：只有**显式点名**退役家族才算弃读对象，
    没写家族的旧数据不是弃读对象（它走各自读法的默认逻辑）。判定默认值只住这里，
    三处各写一份「or zimage」就是漂的开始。
    """
    return str(name or "zimage") in RETIRED_IMAGE_FAMILIES
# 本仓支持的帧生图模型家族。**从上面那张表派生**：家族清单另写一份的话，将来加第三个
# 家族时【画布】块会静默少一行（issue #33 复审）。
_FRAME_MODELS = tuple(IMAGE_PROFILE_DEFAULTS)

# 生图模型键 → 给人看的显示名。渲染层与帧节点的【画布】块都用它，免得同一张对照表
# 在三个地方各写一遍（issue #33 复审）。
IMAGE_FAMILY_LABELS = {"zimage": "Z-Image"}
# 表外家族的兜底档。**它产不出对象**——真正拦下这种输入的是 ``FL2VAFramePrompt.__post_init__``
# 的「生图模型类型无效」（实测 `from_dict({"model_family": "sdxl"})` 直接 ValueError）。
# 之所以还留着这一档：抛错要来自字段校验那一处，而不是 from_dict 里的 AttributeError。
_EMPTY_IMAGE_PROFILE = ImageProfile("", "", 0, 0, "candidate")


def image_canvas_size(
    model_family: str,
    canvases: CanvasMap | None = None,
) -> tuple[int, int]:
    """该生图模型的生效画布（宽, 高）：用户选定优先，未选回落 profile 默认表。

    构图按画布走：同一条构图指令在不同画幅上语义不同，所以帧节点的请求要把画布一并
    送进模型（issue #33）。**形状与数字只住在这一个读取口后面**：2026-10-16 起尺寸由
    用户选定（issue #42，选比例→选尺寸→不符可自定义），``canvases`` 是会话里用户的选择
    （家族 → (宽, 高)）；没选的家族（含旧会话、--brief 无画布键）回落默认表——兜底
    让改动前的行为逐字保留。
    """
    # 家族合法性先于选定值：表外家族带一个选定值也照旧报错——报错要来自「这个家族
    # 根本不存在」，而不是静默接受一个没有 profile/渲染行的幽灵家族。
    profile = IMAGE_PROFILE_DEFAULTS.get(model_family)
    if profile is None:
        raise ValueError(f"未知生图模型：{model_family}")
    if canvases and model_family in canvases:
        width, height = canvases[model_family]
        return int(width), int(height)
    return profile.width, profile.height


# 构图词分套的画幅族边界（issue #42，Q21(c) 裁定「族内就近归类，族外降通用」）。
# r = 高/宽。划分的依据是**两套构图词的措辞前提**，不是拍脑袋的数字：
# - 正方套说「横向留白受限」→ 只在宽≤高时成立 → 近方族下界到 1（r ≥ 1）；
# - 竖幅套说「纵向分层、主体占满一列」→ 要明显高于宽 → 4:5（r=1.25）起才成立
#   （起草裁定原话「4:5 的构图语义确实接近竖幅」，故 1.25 归近竖族）；
# - 超过 1:2 的极端竖（9:21≈2.33）与极端横（21:9）是「极端」，一切横幅是「横幅」：
#   二者都是族外，降为与画幅无关的通用构图词（起草裁定「横幅硬塞任一套都会说错话」）。
# 1:2（r=2.0）必须留在近竖族：它是 zimage 的原生画布。
_SQUARE_FAMILY_MAX = 1.25  # 近方族上界（不含）：1 ≤ r < 1.25
_PORTRAIT_FAMILY_MAX = 2.0  # 近竖族上界（含）：1.25 ≤ r ≤ 2.0；再往外是极端

# 画幅族 → 构图词套（Q21(c) 的映射表本身）：近竖→竖幅套、近方→正方套、族外→通用。
# 分套绑**画幅族**、不绑模型身份——用户给 Z-Image 选了近方形比例，落笔就用正方套。
_COMPOSITION_SET_BY_FAMILY = {"近竖": "竖幅套", "近方": "正方套", "横幅": "通用", "极端": "通用"}


def canvas_family(width: int, height: int) -> str:
    """该画布的画幅族类：``近竖`` / ``近方`` / ``横幅`` / ``极端``（分套的依据）。

    这四类就是票面「近竖→竖幅套、近方→正方套（族内就近归类），横幅与极端比例
    （如 9:21）降通用构图词」的四个归类；显示行里括号中的形状标签就是它。
    """
    if width <= 0 or height <= 0:
        raise ValueError("画布尺寸必须为正")
    ratio = height / width
    if ratio > _PORTRAIT_FAMILY_MAX or ratio < 1 / _PORTRAIT_FAMILY_MAX:
        return "极端"
    if ratio < 1:
        return "横幅"
    if ratio < _SQUARE_FAMILY_MAX:
        return "近方"
    return "近竖"


def composition_set(width: int, height: int) -> str:
    """该画布的构图词套：``竖幅套`` / ``正方套`` / ``通用``（族外）。

    两套不互换；族外（横幅、极端比例）不硬塞任一套——「横幅画面配竖幅构图词」的
    说反话在分套这一层被堵死。
    """
    return _COMPOSITION_SET_BY_FAMILY[canvas_family(width, height)]


def canvas_display(width: int, height: int) -> str:
    """画布显示名与尺寸的**唯一**格式化口（issue #42 AC3）：``9:16 · 576 × 1024（近竖）``。

    显示名=约分后的比例（由宽高导出，不另存标签——标签和尺寸是两个来源就会漂）；
    括号里是**画幅族类**（不是朝向）：它才是构图词分套的依据，与行尾的分套指示同源，
    不会出现「（竖幅）；构图用正方套」这种自相矛盾的行。请求的【画布】块与渲染层的
    画布行都调这里，排版只写这一处。
    """
    from math import gcd

    divisor = gcd(width, height)
    name = f"{width // divisor}:{height // divisor}"
    return f"{name} · {width} × {height}（{canvas_family(width, height)}）"


class RatioPreset(NamedTuple):
    """画布选择器的比例档（issue #42「选比例→选尺寸」的第一步）。

    ``w_ratio / h_ratio`` 是比例的宽/高**份数**（不是像素——像素字段全仓叫 width/height，
    同名会把「9:16 的 9」读成 9 像素，复审点名的撞名）。``sizes`` 是该比例下的预设像素
    尺寸——**必须与 (w_ratio, h_ratio) 逐档约分一致**（tests/test_canvas.py 的表完整性
    守卫钉着）：菜单显示 9:16 档却给出 4:5 的尺寸，用户按显示选、拿到的东西对不上号。
    """

    name: str
    w_ratio: int
    h_ratio: int
    sizes: tuple[tuple[int, int], ...]


# 画布比例档：覆盖两个构图词族（近竖/近方）与常见横幅；档外比例（如 9:21）走
# 选择器的自定义入口。zimage 的原生画布（640×1280）在档内——用户不改主意时按原样
# 选得到，而不是「系统默认」。
CANVAS_RATIO_PRESETS: tuple[RatioPreset, ...] = (
    RatioPreset("1:2", 1, 2, ((640, 1280), (720, 1440))),
    RatioPreset("9:16", 9, 16, ((576, 1024), (720, 1280))),
    RatioPreset("2:3", 2, 3, ((768, 1152), (832, 1248))),
    RatioPreset("3:4", 3, 4, ((768, 1024), (960, 1280))),
    RatioPreset("4:5", 4, 5, ((1024, 1280), (832, 1040))),
    RatioPreset("1:1", 1, 1, ((1024, 1024), (1280, 1280))),
    RatioPreset("4:3", 4, 3, ((1024, 768), (1280, 960))),
    RatioPreset("16:9", 16, 9, ((1280, 720), (1920, 1080))),
)


def composition_hint(width: int, height: int) -> str:
    """请求行尾的构图分套指示：告诉模型这一行该用哪套构图词（issue #42 AC2）。

    族外的指示是**完整的降级指令**（用通用词、且明说不用两套）——只写「通用」两个字，
    模型仍可能按画幅直觉硬套竖幅/正方措辞，「横幅画面配竖幅构图词」的说反话就堵不死。
    """
    family = composition_set(width, height)
    if family == "通用":
        return "构图用与画幅无关的通用构图词（引导线/框中框/三分法等），不用竖幅/正方任一套"
    return f"构图用{family}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _require_text(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} 不能为空")


def _infer_frame_variant(first: tuple, last: tuple) -> str:
    """从关键帧存在性推断变体：双帧→FL2VA、仅首帧→I2VA、仅尾帧→L2VA、无帧→FL2VA（保持报缺失错误）。"""
    if first and last:
        return "FL2VA"
    if first:
        return "I2VA"
    if last:
        return "L2VA"
    return "FL2VA"


# 变体 → 该变体自己的关键帧。**只此一份**：非帧变体（T2VA／未知）不在表里，
# 它们是「没有关键帧」，不是「三选一的另一种」。
_FRAME_NAMES_BY_VARIANT: dict[str, tuple[str, ...]] = {
    "FL2VA": ("first", "last"),
    "I2VA": ("first",),
    "L2VA": ("last",),
}

# 关键帧槽位的全集，**从上面那张表推出来**（别手写第二份）：剥离要遍历它去找不该在的帧，
# 写死一份的话，将来多一个槽位就是**静默不剥**——正是本票要消灭的那类缺陷。
_ALL_FRAME_NAMES: tuple[str, ...] = tuple(
    dict.fromkeys(name for names in _FRAME_NAMES_BY_VARIANT.values() for name in names)
)


def variant_frame_names(variant: str) -> tuple[str, ...]:
    """该变体自己的关键帧名；非帧变体抛 KeyError（调用方只对帧变体用它）。

    公开读取口：帧节点要用它现拼那句「必须保留 first、last」的改写清单——同一份知识
    两处各写一张表，就是本仓反复出现的「规则在、接线不在」的温床（复审指出）。
    """
    return _FRAME_NAMES_BY_VARIANT[str(variant).upper()]


@dataclass(frozen=True)
class PromptArtifact:
    """一份供用户查看、复制的剧本或提示词文本；不是媒体资产。"""

    artifact_id: str
    kind: PromptKind
    content: str
    source_stage: str
    sha256: str = ""
    version: str = "v001"
    created_at: str = ""
    schema_version: str = "prompt_artifact.v1"

    def __post_init__(self) -> None:
        _require_text(self.artifact_id, "artifact_id")
        if self.kind not in _PROMPT_KINDS:
            raise ValueError(f"提示词产物类型无效：{self.kind}")
        _require_text(self.content, f"{self.kind} 内容")
        _require_text(self.source_stage, "source_stage")
        expected = _sha256(self.content)
        if self.sha256 and self.sha256 != expected:
            raise ValueError("提示词产物 sha256 与正文不一致")
        object.__setattr__(self, "sha256", expected)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> PromptArtifact:
        return cls(
            artifact_id=str(raw.get("artifact_id", "")),
            kind=str(raw.get("kind", "")),
            content=str(raw.get("content", "")),
            source_stage=str(raw.get("source_stage", "")),
            sha256=str(raw.get("sha256", "")),
            version=str(raw.get("version", "v001")),
            created_at=str(raw.get("created_at", "")),
            schema_version=str(raw.get("schema_version", "prompt_artifact.v1")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "kind": self.kind,
            "content": self.content,
            "source_stage": self.source_stage,
            "sha256": self.sha256,
            "version": self.version,
            "created_at": self.created_at,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class ImagePromptVariant:
    """一份资产的可直接复制生图提示词（issue #40：N 资产 × 单模型；出图模型按档，issue #87）。

    ``name`` 是资产语义名——素材台账／注入卡靠它对号，所以它是**产物的一部分**，
    不是渲染层的标题。``model_family`` 已随 Flux.2 版全链取消而移除：资产链只剩
    Z-Image（帧链的双模型也在 issue #48 / ADR 0013 一并退役，``FL2VAFramePrompt``
    现在同样只出 Z-Image 一版）。
    """

    name: str
    kind: Literal["character", "prop", "scene"]
    positive_prompt: str
    negative_prompt: str = ""
    profile_id: str = ""
    prompt_node_id: str = ""
    width: int = 0
    height: int = 0
    profile_status: str = "candidate"
    # 「操作说明」（instructions）已于 2026-10-24 删除：它只是双模型时代的搬运说明，
    # 现在抬头已印节点号；模型还会把正文抄进去（实测整段提示词被重复一次）。旧会话
    # 里残留的该键在读取处被忽略——字段删了，没有第二个入口能把它带回来。
    # 锁死清单（issue #53）：本张资产的少数身份特征，逐字摘自 positive_prompt。它是
    # 读图校对判偏差的唯一判据（清单外的出入属理想描述，一律忽略）。**默认空表**
    # 是给旧会话留的读口：节点侧（``nodes._normalize_locked_features``）对新产物要求非空。
    locked_features: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in {"character", "prop", "scene"}:
            raise ValueError(f"生图产物类型无效：{self.kind}")
        _require_text(self.positive_prompt, "positive_prompt")
        if self.width < 0 or self.height < 0:
            raise ValueError("生图尺寸不能为负数")

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ImagePromptVariant:
        return cls(
            name=str(raw.get("name", "")),
            kind=str(raw.get("kind", "")),
            positive_prompt=str(raw.get("positive_prompt", "")),
            negative_prompt=str(raw.get("negative_prompt", "")),
            profile_id=str(raw.get("profile_id", "")),
            prompt_node_id=str(raw.get("prompt_node_id", "")),
            width=int(raw.get("width", 0) or 0),
            height=int(raw.get("height", 0) or 0),
            profile_status=str(raw.get("profile_status", "candidate")),
            locked_features=tuple(str(x) for x in raw.get("locked_features") or ()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "positive_prompt": self.positive_prompt,
            "negative_prompt": self.negative_prompt,
            "profile_id": self.profile_id,
            "prompt_node_id": self.prompt_node_id,
            "width": self.width,
            "height": self.height,
            "profile_status": self.profile_status,
            "locked_features": list(self.locked_features),
        }


@dataclass(frozen=True)
class FL2VAFramePrompt:
    """一张 FL2VA 首帧或尾帧的静态生图提示词。"""

    frame: Literal["first", "last"]
    # 家族合法性由 ``__post_init__`` 对 ``_FRAME_MODELS`` 校验（单一来源在默认表），
    # 不在类型里再抄一份家族清单；Flux.2 已随 issue #48 退役。
    model_family: str
    positive_prompt: str
    negative_prompt: str = ""
    shot_id: str = "SH001"
    time_seconds: float = 0.0
    scene_anchor: str = ""
    subject_anchor: str = ""
    composition: str = ""
    lighting: str = ""
    profile_id: str = ""
    prompt_node_id: str = ""
    width: int = 0
    height: int = 0
    profile_status: str = "candidate"
    # 「操作说明」（instructions）已于 2026-10-24 删除，理由见 `ImagePromptVariant`。

    def __post_init__(self) -> None:
        if self.frame not in {"first", "last"}:
            raise ValueError(f"FL2VA 帧类型无效：{self.frame}")
        if self.model_family not in _FRAME_MODELS:
            raise ValueError(f"FL2VA 生图模型类型无效：{self.model_family}")
        _require_text(self.positive_prompt, "FL2VA positive_prompt")
        if self.time_seconds < 0:
            raise ValueError("FL2VA 帧时间不能为负数")
        if self.width < 0 or self.height < 0:
            raise ValueError("FL2VA 生图尺寸不能为负数")

    @classmethod
    def from_dict(
        cls, raw: dict[str, Any], canvases: CanvasMap | None = None,
    ) -> "FL2VAFramePrompt":
        """解析一行关键帧提示词；``canvases`` 给定时尺寸**无条件**取读取口（issue #42）。

        画布是请求下发的数据，不是模型/文件的自由发挥：给了 ``canvases``（哪怕是空
        dict＝回落默认表），raw 里带的 width/height 一律不认——模型幻觉尺寸、帧级
        回退形状残留、错形 state 都从这一层被挡住（复审实测：只在 bundle 层按家族
        子键覆盖时，帧级回退形状照样把 111×222 送进产物，与请求【画布】块漂开）。
        不传 ``canvases``＝信任 raw 里的既有值（旧会话重展示走这条，保历史原样）。
        """
        # 表外家族走空档——它只在下面构造时被 ``__post_init__`` 拦下（见常量处的说明），
        # 这一档取空值是为了让报错来自字段校验、而不是这里的 AttributeError。
        family = str(raw.get("model_family", "zimage"))
        profile = IMAGE_PROFILE_DEFAULTS.get(family, _EMPTY_IMAGE_PROFILE)
        if canvases is None:
            width = int(raw.get("width", profile.width) or profile.width)
            height = int(raw.get("height", profile.height) or profile.height)
        else:
            width, height = image_canvas_size(family, canvases)
        return cls(
            frame=str(raw.get("frame", "")),
            model_family=str(raw.get("model_family", "")),
            positive_prompt=str(raw.get("positive_prompt") or raw.get("prompt", "")),
            negative_prompt=str(raw.get("negative_prompt", "")),
            shot_id=str(raw.get("shot_id", "SH001")),
            time_seconds=float(raw.get("time_seconds", 0) or 0),
            scene_anchor=str(raw.get("scene_anchor", "")),
            subject_anchor=str(raw.get("subject_anchor", "")),
            composition=str(raw.get("composition", "")),
            lighting=str(raw.get("lighting", "")),
            profile_id=str(raw.get("profile_id", profile.profile_id)),
            prompt_node_id=str(raw.get("prompt_node_id", profile.prompt_node_id)),
            width=width,
            height=height,
            profile_status=str(raw.get("profile_status", profile.status)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "frame": self.frame,
            "model_family": self.model_family,
            "positive_prompt": self.positive_prompt,
            "negative_prompt": self.negative_prompt,
            "shot_id": self.shot_id,
            "time_seconds": self.time_seconds,
            "scene_anchor": self.scene_anchor,
            "subject_anchor": self.subject_anchor,
            "composition": self.composition,
            "lighting": self.lighting,
            "profile_id": self.profile_id,
            "prompt_node_id": self.prompt_node_id,
            "width": self.width,
            "height": self.height,
            "profile_status": self.profile_status,
        }


@dataclass(frozen=True)
class FL2VAPromptBundle:
    """同一段 FL2VA 视频的首帧/尾帧融合提示词集合。"""

    scene_anchor: str
    first: tuple[FL2VAFramePrompt, ...]
    last: tuple[FL2VAFramePrompt, ...]
    continuity_constraints: tuple[str, ...] = ()
    profile_id: str = "h3_fl2va_v2"
    prompt_node_id: str = "187"
    prompt_input: str = "value"
    first_frame_slot_id: str = "first_frame"
    last_frame_slot_id: str = "last_frame"
    required_scene_terms: tuple[tuple[str, ...], ...] = ()
    schema_version: str = "fl2va_prompt_bundle.v1"

    def __post_init__(self) -> None:
        _require_text(self.scene_anchor, "FL2VA scene_anchor")
        variant = _infer_frame_variant(self.first, self.last)
        if variant == "FL2VA" and (not self.first or not self.last):
            raise ValueError("FL2VA 必须同时包含首帧和尾帧提示词")
        for frame_name in variant_frame_names(variant):
            rows = getattr(self, frame_name)
            models = {row.model_family for row in rows}
            if len(models) != len(rows):
                raise ValueError(f"{variant} {frame_name} 帧不能重复同一生图模型")
            if any(row.frame != frame_name for row in rows):
                raise ValueError(f"{variant} {frame_name} 帧包含错误的 frame 标识")
            if any(not row.scene_anchor for row in rows):
                raise ValueError(f"{variant} {frame_name} 帧缺少场景锚点")
        if variant == "FL2VA" and self.profile_id != "h3_fl2va_v2":
            raise ValueError(f"FL2VA Profile 无效：{self.profile_id}")
        if variant == "FL2VA" and (not self.first_frame_slot_id or not self.last_frame_slot_id):
            raise ValueError("FL2VA 首尾帧输入槽不能为空")

    @classmethod
    def from_dict(
        cls, raw: dict[str, Any], canvases: CanvasMap | None = None,
    ) -> "FL2VAPromptBundle":
        """解析 bundle；``canvases`` 透传到每行的尺寸解析（帧级回退形状也走同一条）。"""
        def parse_frame(frame_name: str) -> tuple[FL2VAFramePrompt, ...]:
            value = raw.get(frame_name, [])
            if isinstance(value, dict):
                rows = []
                for model_family in _FRAME_MODELS:
                    item = value.get(model_family)
                    if isinstance(item, dict):
                        rows.append(FL2VAFramePrompt.from_dict({
                            **item, "frame": frame_name, "model_family": model_family,
                            "scene_anchor": str(item.get("scene_anchor") or raw.get("scene_anchor", "")),
                            "shot_id": str(item.get("shot_id") or value.get("shot_id", "SH001")),
                            "time_seconds": item.get("time_seconds", value.get("time_seconds", 0)),
                        }, canvases))
                # 帧级回退形状：模型没写家族子键、直接把提示词铺在帧上。显式点名
                # 已退役家族的（旧载荷）在这里弃读——家族子键那条路靠 `_FRAME_MODELS`
                # 遍历天然跳过 flux2，这条路得自己挡。
                if not rows and (value.get("positive_prompt") or value.get("prompt")) \
                        and not is_retired_image_family(value.get("model_family")):
                    rows.append(FL2VAFramePrompt.from_dict({
                        **value, "frame": frame_name, "model_family": value.get("model_family", "zimage"),
                        "scene_anchor": str(value.get("scene_anchor") or raw.get("scene_anchor", "")),
                    }, canvases))
                return tuple(rows)
            # 列表形（节点落盘的 to_dict()）：旧产物里的退役家族行**弃读**（issue #48，
            # ADR 0013）——历史数据不当报错也不续用；表外家族（如 sdxl）仍走构造处
            # 的「生图模型类型无效」，活数据不静默。
            return tuple(
                FL2VAFramePrompt.from_dict(item, canvases)
                for item in value
                if isinstance(item, dict)
                and not is_retired_image_family(item.get("model_family"))
            )

        first = parse_frame("first")
        last = parse_frame("last")
        variant = _infer_frame_variant(first, last)
        profile_default = "h3_fl2va_v2" if variant == "FL2VA" else ""
        return cls(
            scene_anchor=str(raw.get("scene_anchor", "")),
            first=first,
            last=last,
            continuity_constraints=tuple(str(x) for x in raw.get("continuity_constraints", [])),
            profile_id=str(raw.get("profile_id") or profile_default),
            prompt_node_id=str(raw.get("prompt_node_id", "187")),
            prompt_input=str(raw.get("prompt_input", "value")),
            first_frame_slot_id=str(raw.get("first_frame_slot_id", "first_frame")),
            last_frame_slot_id=str(raw.get("last_frame_slot_id", "last_frame")),
            required_scene_terms=tuple(
                tuple(str(term) for term in group)
                for group in raw.get("required_scene_terms", [])
            ),
            schema_version=str(raw.get("schema_version", "fl2va_prompt_bundle.v1")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "scene_anchor": self.scene_anchor,
            "first": [item.to_dict() for item in self.first],
            "last": [item.to_dict() for item in self.last],
            "continuity_constraints": list(self.continuity_constraints),
            "profile_id": self.profile_id,
            "prompt_node_id": self.prompt_node_id,
            "prompt_input": self.prompt_input,
            "first_frame_slot_id": self.first_frame_slot_id,
            "last_frame_slot_id": self.last_frame_slot_id,
            "required_scene_terms": [list(group) for group in self.required_scene_terms],
            "schema_version": self.schema_version,
        }


def validate_fl2va_bundle(
    bundle: FL2VAPromptBundle,
    *,
    duration: float | None = None,
    variant: str | None = None,
) -> list[str]:
    """返回该变体关键帧提示词的确定性问题，不调用模型或外部服务。"""
    variant = str(variant or _infer_frame_variant(bundle.first, bundle.last)).upper()
    issues: list[str] = []
    if variant == "FL2VA":
        if bundle.first_frame_slot_id != "first_frame" or bundle.last_frame_slot_id != "last_frame":
            issues.append("FL2VA_FRAME_SLOT_MISMATCH")
    if bundle.prompt_node_id != "187":
        issues.append("FL2VA_PROMPT_NODE_MISMATCH")
    if not bundle.continuity_constraints:
        issues.append("FL2VA_CONTINUITY_MISSING")
    # 地点词一律**覆盖式**（issue #37）：每组地点词被 scene_anchor 或**任一帧**覆盖即可。
    # 旧口径按组数分流（单组=逐帧严格、多组=覆盖式），其假设是「词表命中几组 = 主题有几个
    # 地点」——不成立：词表是从**整条**主题里子串匹配出来的，而一张关键帧只画主题里的
    # **一个**时刻。实测主题「云上天宫的女子 → 人间高楼室内练琵琶的女孩」只命中「室内」
    # 一组（来自后半句），而 I2VA / FL2VA 的**首帧画的是天宫**：画面里写不出「室内」，
    # 模型两轮都交不出，阶段 1 跑满 4 分 37 秒后整条抛错（output/issue37-evidence/）。
    # 锚点是**全片**共享的环境锚点、帧才是具体画面，覆盖式正对上这个分工。
    # 有意接受的代价：首帧酒馆、尾帧换到另一处这类「帧间换地点」这条不再拦。**没有兜底**
    # ——实测（2026-09-27 复审）「首帧酒馆 + 尾帧『among tall trees in a forest』」在这条与
    # 整片正文守卫（theme_fidelity_issues）下**双双放行**：后者只管「该地点在整片正文里
    # 出现过」，而这段话在开场就出现过；_CONFLICTING_OUTDOOR_TERMS 只认 forest clearing /
    # open field / wilderness / outdoor camp 四个词组，不含光秃的 forest。
    groups = [group for group in bundle.required_scene_terms if group]
    all_frame_text = " ".join(
        row.positive_prompt
        for frame_name in variant_frame_names(variant)
        for row in getattr(bundle, frame_name)
    ).lower()
    anchor_lower = bundle.scene_anchor.lower()
    for frame_name in variant_frame_names(variant):
        for row in getattr(bundle, frame_name):
            if row.scene_anchor != bundle.scene_anchor:
                issues.append(f"FL2VA_{frame_name.upper()}_ANCHOR_MISMATCH")
            if not row.positive_prompt.strip():
                issues.append(f"FL2VA_{frame_name.upper()}_PROMPT_EMPTY")
            if frame_name == "first" and row.time_seconds != 0:
                issues.append("FL2VA_FIRST_FRAME_TIME_MISMATCH")
            if duration is not None and frame_name == "last" and row.time_seconds > duration:
                issues.append("FL2VA_LAST_FRAME_TIME_EXCEEDS_DURATION")
            if groups:
                for term in _CONFLICTING_OUTDOOR_TERMS:
                    if _contains_unqualified_conflict(row.positive_prompt, term):
                        issues.append("FL2VA_SCENE_DRIFT")
    for group in groups:
        if any(term.lower() in anchor_lower for term in group) \
                or any(term.lower() in all_frame_text for term in group):
            continue
        issues.append("FL2VA_SCENE_GROUP_UNCOVERED")
    return list(dict.fromkeys(issues))


def strip_foreign_frames(raw: dict[str, Any], variant: str) -> tuple[dict[str, Any], tuple[str, ...]]:
    """剥掉不属于该变体的关键帧，返回 ``(剥好的 raw, 被剥掉的帧名)``。

    帧的归属由**变体**决定（变体是用户的选择，见 `resolve_effective_variant`），而模型
    偶尔会多写一张不属于它的帧：issue #38 现场是 I2VA 运行里多出 ``last``，#37 那次运行
    **两轮**都自己多写了它（当时在试图满足地点词校验）。多出来的那张无处可去——

    - `validate_fl2va_bundle` 只认该变体自己的帧（ADR 0009 裁定一），所以它**不被校验**；
    - 于是它会一路走到 `render_generation`，落成一份用户拿不到的提示词文件；
    - 还会被 `_fl2va_frame_context` 当成整片提示词的帧锚定（它对 I2VA 照样读 ``last``），
      让正文去收束到一张用户根本不会生成的图；`render_baseline_block`（改稿的「上一版产物」）
      与常识质检判官 `frame_common_sense_issues` 同样两帧都读。

    非帧变体（T2VA／未知）**原样返回**：帧节点对它们直接返回 ``{}``，本来就不存在帧
    bundle；替一份产不出来的输入发明读法，只会让旧 state 多一种解释。剥离只在
    **原始 dict** 上做（不是剥已建好的 bundle）：剥完再解析，`from_dict` 才会据实推出
    该变体自己的 profile 默认值——否则 I2VA 会顶着 FL2VA 的 ``h3_fl2va_v2``。

    「绝不静默失败」这条由**调用方**承担：帧节点在剥离处打告警（真实运行都经过它），
    本函数只做确定性的剥离、不打印，好让 `result_from_state` 这类纯转换路径也能复用它。
    """
    frame_names = _FRAME_NAMES_BY_VARIANT.get(str(variant).upper())
    if frame_names is None:
        return raw, ()
    dropped = tuple(
        name for name in _ALL_FRAME_NAMES
        if name not in frame_names and raw.get(name)
    )
    if not dropped:
        return raw, ()
    return {key: value for key, value in raw.items() if key not in dropped}, dropped


def fl2va_bundle_from_dict(raw: dict[str, Any], brief: Brief) -> FL2VAPromptBundle:
    """把模型 JSON 补齐为项目协议（只留该变体自己的帧），并执行主题地点约束校验。"""
    # 这一剥是**不变量**：任何调用方都绕不过它。被剥掉的帧名在这里不报告——真实运行
    # 都经过帧节点，告警由它打（见 `strip_foreign_frames`）。
    enriched, _dropped = strip_foreign_frames(dict(raw), brief.variant)
    enriched["required_scene_terms"] = [list(group) for group in scene_requirement_groups(brief.plot)]
    for frame_name, default_time in (("first", 0.0), ("last", brief.duration)):
        value = enriched.get(frame_name)
        if not isinstance(value, dict):
            continue
        frame_value = dict(value)
        frame_value.setdefault("time_seconds", default_time)
        for model_family in _FRAME_MODELS:
            item = frame_value.get(model_family)
            if isinstance(item, dict):
                item_value = dict(item)
                item_value.setdefault("time_seconds", default_time)
                frame_value[model_family] = item_value
        enriched[frame_name] = frame_value
    # 尺寸交给 from_dict 按 ``brief.canvases`` 权威解析（issue #42 AC1）：模型幻觉的
    # width/height、帧级回退形状的残留一律不认——画布是请求下发的数据，不是模型的
    # 自由发挥；不认它就成了「产物尺寸与请求【画布】块漂开」，AC3 的同源被破坏。
    bundle = FL2VAPromptBundle.from_dict(enriched, brief.canvases)
    if str(brief.variant).upper() == "FL2VA" and (not bundle.first or not bundle.last):
        raise ValueError("FL2VA 必须同时包含首帧和尾帧提示词")
    issues = validate_fl2va_bundle(bundle, duration=brief.duration, variant=brief.variant)
    if issues:
        raise ValueError("FL2VA 首尾帧提示词校验失败：" + ", ".join(issues))
    return bundle


@dataclass(frozen=True)
class GenerationResult:
    """一次完整管线生成的可恢复项目产物。"""

    generation_id: str
    topic_id: str
    project_id: str
    script: str
    artifacts: tuple[PromptArtifact, ...]
    mode: str
    variant: str
    duration: float
    style: str
    language: str
    brief_snapshot: dict[str, Any] = field(default_factory=dict)
    image_prompts: tuple[ImagePromptVariant, ...] = ()
    fl2va_prompt_bundle: FL2VAPromptBundle | None = None
    created_at: str = ""
    schema_version: str = "generation_result.v1"

    def __post_init__(self) -> None:
        if not _GENERATION_ID.fullmatch(self.generation_id):
            raise ValueError(f"generation_id 格式无效：{self.generation_id}")
        _require_text(self.topic_id, "topic_id")
        _require_text(self.project_id, "project_id")
        _require_text(self.script, "script")
        if self.duration <= 0:
            raise ValueError("duration 必须大于 0")
        kinds = [artifact.kind for artifact in self.artifacts]
        if self.variant.upper() == "FL2VA":
            if self.fl2va_prompt_bundle is None:
                raise ValueError("FL2VA GenerationResult 必须包含首尾帧提示词")
            if kinds != ["video"]:
                raise ValueError("FL2VA GenerationResult 只保存 video artifact；人物、道具、场景必须融合到首尾帧")
        else:
            missing = [kind for kind in _PROMPT_KINDS if kind != "script" and kind not in kinds]
            if "script" in kinds or missing:
                raise ValueError("GenerationResult 的 artifacts 只能保存四类提示词，且不能重复或缺失")
            if len(kinds) != len(set(kinds)):
                raise ValueError("GenerationResult 不允许重复提示词类型")
        for artifact in self.artifacts:
            if not _SHA256.fullmatch(artifact.sha256):
                raise ValueError("提示词产物必须带有效 SHA-256")
        if self.fl2va_prompt_bundle is not None:
            issues = validate_fl2va_bundle(self.fl2va_prompt_bundle, duration=self.duration)
            if issues:
                raise ValueError("FL2VA 首尾帧提示词校验失败：" + ", ".join(issues))

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> GenerationResult:
        return cls(
            generation_id=str(raw.get("generation_id", "")),
            topic_id=str(raw.get("topic_id", "")),
            project_id=str(raw.get("project_id", "")),
            script=str(raw.get("script", "")),
            artifacts=tuple(PromptArtifact.from_dict(x) for x in raw.get("artifacts", [])),
            mode=str(raw.get("mode", "base")),
            variant=str(raw.get("variant", "T2VA")),
            duration=float(raw.get("duration", 0)),
            style=str(raw.get("style", "")),
            language=str(raw.get("language", "")),
            brief_snapshot=dict(raw.get("brief_snapshot", {})),
            image_prompts=tuple(ImagePromptVariant.from_dict(x) for x in raw.get("image_prompts", [])),
            fl2va_prompt_bundle=(
                FL2VAPromptBundle.from_dict(raw["fl2va_prompt_bundle"])
                if isinstance(raw.get("fl2va_prompt_bundle"), dict)
                else None
            ),
            created_at=str(raw.get("created_at", "")),
            schema_version=str(raw.get("schema_version", "generation_result.v1")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "generation_id": self.generation_id,
            "topic_id": self.topic_id,
            "project_id": self.project_id,
            "script": self.script,
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
            "mode": self.mode,
            "variant": self.variant,
            "duration": self.duration,
            "style": self.style,
            "language": self.language,
            "brief_snapshot": self.brief_snapshot,
            "image_prompts": [item.to_dict() for item in self.image_prompts],
            "fl2va_prompt_bundle": (
                self.fl2va_prompt_bundle.to_dict()
                if self.fl2va_prompt_bundle is not None else None
            ),
            "created_at": self.created_at,
            "schema_version": self.schema_version,
        }

    def artifact(self, kind: str) -> PromptArtifact:
        if kind == "script":
            return PromptArtifact(
                f"{self.generation_id}-script", "script", self.script, "script",
                created_at=self.created_at,
            )
        for item in self.artifacts:
            if item.kind == kind:
                return item
        raise KeyError(f"生成结果中不存在提示词类型：{kind}")


def brief_snapshot(brief: Brief) -> dict[str, Any]:
    """只保留 Brief 的非秘密内容，不保存运行时配置或 API key。"""
    return {
        "mode": brief.mode,
        "variant": brief.variant,
        "duration": brief.duration,
        "style": brief.style,
        "language": brief.language,
        "plot": brief.plot,
        # 用户选定画布（issue #42 复审）：本快照也是「逐字段显式列出」的清单，
        # 漏登记的字段在 generation.json 里就没了——选定值是产物数据，必须留痕。
        "canvases": {family: [width, height] for family, (width, height) in brief.canvases.items()},
        # 视频输出三元组（issue #45）：比例档 + megapixels + 对齐倍数（注入卡同口径）。
        "video_output": (
            {"ratio": brief.video_output.ratio,
             "megapixels": brief.video_output.megapixels,
             "multiple": brief.video_output.multiple}
            if brief.video_output else None
        ),
        "refs": [
            {"picture": ref.picture, "name": ref.name, "description": ref.description}
            for ref in brief.refs
        ],
    }


def result_from_state(
    state: dict[str, Any], brief: Brief, *, generation_id: str, topic_id: str, project_id: str,
) -> GenerationResult:
    values = {
        "script": state.get("script", ""),
        "video": state.get("final_prompt", ""),
        "character": state.get("character_design", ""),
        "prop": state.get("prop_design", ""),
        "scene": state.get("background_design", ""),
    }
    # 资产图提示词只属于多参路线（issue #78 / 父 spec #77）：统一入口取代了 #40 回滚时
    # 在 FL2VA 分支里单写的那份「结果置空」特例——留着就是两处口径，新增出口必漏一处。
    image_prompts = asset_prompts_for(brief, state)
    if brief.variant.upper() == "FL2VA":
        required = {"script": values["script"], "video": values["video"]}
        missing = [kind for kind, content in required.items() if not str(content).strip()]
        if missing:
            raise ValueError("管线缺少结构化产物：" + ", ".join(missing))
        raw_bundle = state.get("fl2va_prompt_bundle")
        if not isinstance(raw_bundle, dict):
            raise ValueError("管线缺少 FL2VA 首尾帧提示词")
        image_bundle = fl2va_bundle_from_dict(raw_bundle, brief)
        artifacts = (
            PromptArtifact(
                artifact_id=f"{generation_id}-video",
                kind="video",
                content=str(values["video"]),
                source_stage="final_prompt",
                created_at=_now(),
            ),
        )
    else:
        missing = [kind for kind, content in values.items() if not str(content).strip()]
        if missing:
            raise ValueError("管线缺少结构化产物：" + ", ".join(missing))
        artifacts = tuple(
            PromptArtifact(
                artifact_id=f"{generation_id}-{kind}",
                kind=kind,
                content=str(values[kind]),
                source_stage={"video": "final_prompt", "character": "character_design", "prop": "prop_design", "scene": "background_design"}[kind],
                created_at=_now(),
            )
            for kind in ("video", "character", "prop", "scene")
        )
        # 多返回的关键帧在这条支路也要剥（issue #38）：它不经 `fl2va_bundle_from_dict`
        # ——那条函数顺带做地点校验与词表补齐，而这条支路为「读旧 state」特意保持宽松——
        # 所以剥的动作得各做一次。剥完自己的帧一张不剩时留给 `from_dict` 报错：
        # 那是「该变体连自己的帧都没有」，与「多了一张」不是一回事，不该被这次改动抹平。
        raw_bundle = state.get("fl2va_prompt_bundle")
        image_bundle = (
            FL2VAPromptBundle.from_dict(
                strip_foreign_frames(raw_bundle, brief.variant)[0], brief.canvases)
            if isinstance(raw_bundle, dict)
            else None
        )

    return GenerationResult(
        generation_id=generation_id,
        topic_id=topic_id,
        project_id=project_id,
        script=str(values["script"]),
        artifacts=artifacts,
        mode=brief.mode,
        variant=brief.variant,
        duration=brief.duration,
        style=brief.style,
        language=brief.language,
        brief_snapshot=brief_snapshot(brief),
        image_prompts=image_prompts,
        fl2va_prompt_bundle=image_bundle,
        created_at=_now(),
        schema_version="generation_result.v2" if image_bundle is not None else "generation_result.v1",
    )


def generation_directory(project_directory: Path, generation_id: str) -> Path:
    if not _GENERATION_ID.fullmatch(generation_id):
        raise ValueError(f"generation_id 格式无效：{generation_id}")
    return project_directory / "generations" / generation_id


# 资产类型 → 渲染标题与旧形状 state 的语义名退化共用一份标签（两处各写一份就漂）。
KIND_LABELS: dict[str, str] = {"character": "人物", "prop": "道具", "scene": "场景"}


def image_prompt_variants(
    state: dict[str, Any],
    canvases: CanvasMap | None = None,
) -> tuple[ImagePromptVariant, ...]:
    """从结构化适配器结果构造 N 份可复制提示词（每资产一版）；旧 state 缺少时保留兼容降级。

    ``canvases`` 是用户选定画布（issue #42）：产物行的尺寸从它解析（未选回落默认表），
    显示串在渲染处取 ``canvas_display``——与请求【画布】块同源。

    state 里 ``{kind}_image_prompts`` 是**资产条目列表**（issue #40 N 化），每条四键
    ``name / positive_prompt / negative_prompt / locked_features``。两种旧形状照样可读：
    **dict 形状**（#40 之前的会话：``{zimage:…, flux2:…}``）每类降级为一条 Z-Image——
    Flux.2 版已全链取消，旧数据里的 flux2 一并弃读；**整键缺失**（#36 之前的会话）回退
    设计正文——那种现场没有专门的生图提示词，回退到设计正文总比给空串强（那是设计正文
    曾经当生图提示词喂 ComfyUI 的根源，新跑的会话永远走不到那支，``tests/test_asset_image_prompts.py`` 钉住）。
    """
    variants: list[ImagePromptVariant] = []
    kind_meta = {
        "character": ("character_image_prompts", "character_design"),
        "prop": ("prop_image_prompts", "prop_design"),
        "scene": ("scene_image_prompts", "background_design"),
    }
    for kind, (field, design_field) in kind_meta.items():
        label = KIND_LABELS[kind]
        raw = state.get(field)
        if isinstance(raw, list):
            entries = raw
        elif isinstance(raw, dict):
            # 旧双模型形状：只取 zimage 一条（flux2 已取消）；旧条目无语义名，退化为类型标签。
            # dict 里没有可用 zimage（如只剩 flux2）＝与整键缺失同待遇，回退设计正文，
            # 不静默产空（Spec 复审 (c)）。
            z = raw.get("zimage", {})
            if isinstance(z, str):
                z = {"positive_prompt": z}
            entries = [dict(z, name=z.get("name", label))] if z else [{"name": label, "positive_prompt": ""}]
        else:
            entries = [{"name": label, "positive_prompt": ""}]
        for item in entries:
            if isinstance(item, str):
                item = {"name": label, "positive_prompt": item}
            positive = str(item.get("positive_prompt") or state.get(design_field, ""))
            if not positive.strip():
                # 资产键与设计正文都空＝这一类无货可交，跳过而不是构造非法对象——
                # 向导展示会对任意精简 state 调到这里（issue #40），缺料不是错误。
                continue
            # profile 行取生图默认表的 Z-Image 行，画布尺寸走 `image_canvas_size` 读取口
            # （用户选定优先、未选回落同一张表）——issue #33 把这张表扶正为单一来源，
            # issue #42 让选定值从同一读取口进来。尺寸**无条件**取读取口：条目里混进的
            # width/height 一律不认（四键契约外的残留/幻觉不许压过用户选定，复审点名）。
            default = IMAGE_PROFILE_DEFAULTS["zimage"]
            canvas_width, canvas_height = image_canvas_size("zimage", canvases)
            variants.append(ImagePromptVariant(
                name=str(item.get("name", "") or label),
                kind=kind,
                positive_prompt=positive,
                negative_prompt=str(item.get("negative_prompt", "")),
                profile_id=str(item.get("profile_id", default.profile_id)),
                prompt_node_id=str(item.get("prompt_node_id", default.prompt_node_id)),
                width=canvas_width,
                height=canvas_height,
                profile_status=str(item.get("profile_status", "candidate")),
                # 旧条目残留的 instructions 在这里被忽略（字段已删，2026-10-24）：
                # 产物不再有「操作说明」出口，读旧会话也不会把它带回来。
                # 锁死清单（issue #53）照抄条目；旧条目没有这一键 → 空表（读口宽容，
                # 与旧 dict 形状同一口径：旧会话照旧可读，只是不收窄判据）。
                locked_features=tuple(str(x) for x in item.get("locked_features") or ()),
            ))
    return tuple(variants)


def asset_prompts_for(brief: Brief, state: dict[str, Any]) -> tuple[ImagePromptVariant, ...]:
    """路线感知的资产图提示词取数口（issue #78）：非多参路线恒为空元组。

    四处共享同一路线判据（``brief_parser.is_ref_mode``，父 spec #77），但只有三处走本
    取数口：**产物结果构造、素材登记、描述合并**；`render_generation` 拿不到 Brief，
    只拿 ``result.mode`` 用同一判据决定产物集。新增出口不接这条线就会重演 #40
    「首尾帧路线凭空多出多参产物」。``canvases`` 必须透传——多参产物行的尺寸只有这一个
    来源（未传就静默回落 profile 默认表，与用户选定自相矛盾）。
    """
    if not is_ref_mode(brief.mode):
        return ()
    return image_prompt_variants(state, brief.canvases)


def ref_asset_registry(state: dict[str, Any], brief: Brief) -> list[RefItem]:
    """多参路线的参考素材登记：语义名 + `<Picture N>` 编号 + 描述（生图提示词）。

    编号按**剧本出场顺序**分配（编号即叙事顺序，2026-10-16 裁定，CONTEXT.md「参考素材」）：
    语义名在剧本里的首次出现位置决定次序；找不到出场的资产排最后（保持人物→道具→场景的
    枚举序）——编号钉的是叙事顺序，找不到出场就没有叙事位次，但**不静默丢弃**（丢掉的
    资产会在素材台账／注入卡以「图生了却无处接线」的形态回来）。

    ``brief.refs`` 非空时以**用户登记**为准：brief 文件流程里编号是用户自己声明的，
    出场顺序不得改写它。

    **mode 守卫**（issue #41 复审 F1）：base／帧变体不派生资产登记——base 的 Picture 1/2
    是首尾帧，把资产图提示词派生成 `<Picture N>` 清单会给帧路线的 agent 挂错
    ``list_reference_assets``（实测复审：帧路线 qa/prompt_engineer 多出该工具、报资产级编号，
    AC1「首尾帧路线不受影响」软性回归）。派生只对 ``mode="ref"`` 发生。

    ``brief`` **必填**（issue #77 复审 C）：可选的 ``brief=None`` 等于留了一条绕过路线
    守卫的后门——不传就既不过滤也不合并描述，调用方漏传不会有任何症状。所有生产调用点
    都持有 Brief，测试也按路线显式构造。

    描述默认取该资产的**生图提示词**（素材画面事实规则：以生图提示词为描述主体，
    它细节最全）；若该语义名有**读图校对**记录（issue #44），取裁决合并后的**素材画面
    事实**——生图提示词为底、只定点改了不符的那处、尾部并入了补漏事实，细节不丢。
    """
    if brief.refs:
        # 路线必须显式交给描述合并（issue #78）：这一支在 mode 守卫**之前**返回，
        # 不传路线的话，旧 base 会话里残留的同名资产提示词会顶替用户登记的帧描述。
        return _with_merged_descriptions(
            sorted(brief.refs, key=lambda r: r.picture), state, brief)
    if not is_ref_mode(brief.mode):
        return []
    script = str(state.get("script", ""))

    def _appearance_key(pair: tuple[int, ImagePromptVariant]) -> tuple[int, int, int]:
        order, item = pair
        at = script.find(item.name) if item.name else -1
        if at >= 0:
            return (0, at, order)  # 有出场：按剧本首次出现位置
        return (1, order, order)   # 无出场：排最后，保持枚举序（稳定）

    # 走统一入口（issue #78）：到这里路线已确定是多参，入口只是把「透传画布」这件事
    # 也钉在同一处。
    variants = asset_prompts_for(brief, state)
    ordered = [item for _, item in sorted(enumerate(variants), key=_appearance_key)]
    return _with_merged_descriptions([
        RefItem(picture=number, name=item.name, description=item.positive_prompt)
        for number, item in enumerate(ordered, 1)
    ], state, brief)


def _with_merged_descriptions(refs: list[RefItem], state: dict[str, Any],
                              brief: Brief) -> list[RefItem]:
    """素材画面事实覆盖（issue #44 AC3）：描述取校对记录的合并结果。

    合并结果＝生图提示词为底、定点纠偏 + 尾部补漏（``merge_asset_description``），
    细节不丢。无记录时的默认优先序与校对对照底本**同源**（评审：双源不一致会让
    校对记录一来、描述从登记文本整体换源）：合并结果 ＞ 生图提示词 ＞ 用户登记描述。

    路线过滤（issue #78）：非多参路线原样返回登记的 refs——首尾帧的 refs 是首/尾帧锚点，
    拿多参残留的资产提示词或读图校对记录改写它，就是把两条路线的指称体系搅在一起。
    ``brief`` **必填**（issue #77 复审 C）：与 ``ref_asset_registry`` 同一条后门。
    """
    if not is_ref_mode(brief.mode):
        return list(refs)
    merged = proofread_descriptions(state)
    variants = asset_prompts_for(brief, state)
    prompts = {v.name: v.positive_prompt for v in variants}
    return [
        RefItem(picture=r.picture, name=r.name,
                description=merged.get(r.name) or prompts.get(r.name) or r.description,
                path=r.path)
        for r in refs
    ]


def render_asset_rows(rows: list[ImagePromptVariant], kind: str) -> str:
    """渲染一类资产的可复制提示词（N 个资产，每个一节，标题用语义名）。

    向导出站与 ``render_generation`` 共用这一份排版：两处各写一份渲染就是
    第二份可写来源，改一处漏一处（issue #40）。
    """
    labels = KIND_LABELS
    parts = [f"# {labels[kind]}生图提示词", ""]
    for item in rows:
        parts.extend([
            f"## {item.name}", "",
            f"- Workflow Profile: `{item.profile_id}`（状态：{item.profile_status}）",
            f"- 主提示词节点：`{item.prompt_node_id}`",
            # 画布行与请求【画布】块同取 `canvas_display`（issue #42 AC3）；
            # 「推荐尺寸」措辞随本票退役——那是系统钦定时代的渲染层标签。
            f"- 画布：`{canvas_display(item.width, item.height)}`", "",
        ])
        # 锁死清单单独成段（issue #53 AC1）：它是读图校对判偏差的唯一判据，必须出现在
        # **产物**里——用户看不到清单，就无从判断「系统凭什么说这条是偏差」。非空才渲染
        # （旧产物没有这一键，多一个空标题只是噪声，渲染结果与改动前逐字一致）。
        if item.locked_features:
            parts.extend(["### 锁死清单", "",
                          *[f"- {x}" for x in item.locked_features], ""])
        parts.extend([
            "### Positive Prompt", "", item.positive_prompt, "",
        ])
        if item.negative_prompt:
            parts.extend(["### Negative Prompt", "", item.negative_prompt, ""])
        # 这里曾有「### 操作说明」小节（2026-10-24 删除）：那是双模型时代告诉用户
        # 「这份粘到哪个 ComfyUI 节点」的搬运说明，Flux.2 退役后抬头已经印了节点号；
        # 而模型会把它填成正文本身（实测 877 字提示词被原样重复一次），产物就成了
        # 一整段复制品。历史会话里残留的 instructions 一律忽略，不再展示。
    return "\n".join(parts).rstrip() + "\n"


def render_image_prompt_markdown(result: GenerationResult, kind: str) -> str:
    """渲染一个资产类型的可复制提示词（N 个资产，每个一节）。"""
    rows = [item for item in result.image_prompts if item.kind == kind]
    if not rows:
        # 旧 generation 没有资产字段时，仍返回旧正文。
        return result.artifact(kind).content
    return render_asset_rows(rows, kind)


def render_fl2va_frame_markdown(result: GenerationResult, frame: str) -> str:
    """渲染首帧或尾帧的可复制提示词（单模型 Z-Image：issue #48 全链退役）。"""
    if result.fl2va_prompt_bundle is None:
        raise KeyError("生成结果中不存在 FL2VA 首尾帧提示词")
    if frame not in {"first", "last"}:
        raise ValueError(f"FL2VA 帧类型无效：{frame}")
    bundle = result.fl2va_prompt_bundle
    rows = bundle.first if frame == "first" else bundle.last
    title = "首帧" if frame == "first" else "尾帧"
    variant = str(result.variant).upper()
    if not rows:
        return f"# {variant} {title}生图提示词\n\n（{variant} 变体未生成{title}提示词）\n"
    parts = [
        f"# {variant} {title}生图提示词", "",
        "人物、道具和场景已经融合在同一张关键帧画面中。可直接复制使用。", "",
        f"- 场景锚点：{bundle.scene_anchor}",
        f"- {variant} 视频 Profile：`{bundle.profile_id}`",
        f"- 视频提示词节点：`{bundle.prompt_node_id}`", "",
    ]
    slot_id = bundle.first_frame_slot_id if frame == "first" else bundle.last_frame_slot_id
    for item in rows:
        model_title = IMAGE_FAMILY_LABELS[item.model_family]
        parts.extend([
            f"## {model_title}", "",
            f"- 静态图 Workflow Profile：`{item.profile_id}`（状态：{item.profile_status}）",
            f"- 主提示词节点：`{item.prompt_node_id}`",
            # 画布行与请求【画布】块同取 `canvas_display`（issue #42 AC3）。
            f"- 画布：`{canvas_display(item.width, item.height)}`",
            f"- {variant} 输入槽：`{slot_id}`", "",
        ])
        # 构图与光线单独成行（issue #33）：它们是**可单独替换的维度**——复跑同一张图时
        # 换光不换构图靠的就是这两行。非空才渲染：老产物这两个字段是空串（实测 18/18），
        # 那时多两行空标题只会是噪声，渲染结果与改动前逐字一致。
        if item.composition:
            parts.extend([f"- 构图：{item.composition}"])
        if item.lighting:
            parts.extend([f"- 光线：{item.lighting}"])
        if item.composition or item.lighting:
            parts.append("")
        parts.extend(["### Positive Prompt", "", item.positive_prompt, ""])
        if item.negative_prompt:
            parts.extend(["### Negative Prompt", "", item.negative_prompt, ""])
        # 「操作说明」小节同 `render_asset_rows` 一并删除（2026-10-24）：理由见那处注释，
        # 三个渲染出口（本处、资产产物、向导帧）必须一起收口，否则用户换一个入口仍会看到。
    return "\n".join(parts).rstrip() + "\n"


def render_fl2va_markdown(result: GenerationResult) -> str:
    """渲染关键帧生图提示词与连续性约束摘要（FL2VA 保持原输出不变）。"""
    if result.fl2va_prompt_bundle is None:
        raise KeyError("生成结果中不存在 FL2VA 首尾帧提示词")
    bundle = result.fl2va_prompt_bundle
    variant = str(result.variant).upper()
    if variant == "FL2VA":
        parts = [
            "# FL2VA 融合首尾帧提示词", "",
            "首帧和尾帧均把人物、道具和场景融合在同一画面中；视频提示词描述两帧之间的连续变化。", "",
            f"- 场景锚点：{bundle.scene_anchor}",
            f"- 视频 Profile：`{bundle.profile_id}`",
            f"- 视频提示词节点：`{bundle.prompt_node_id}`（输入：`{bundle.prompt_input}`）",
            f"- 首帧输入槽：`{bundle.first_frame_slot_id}`",
            f"- 尾帧输入槽：`{bundle.last_frame_slot_id}`", "",
            "## 连续性约束", "",
            *[f"- {item}" for item in bundle.continuity_constraints], "",
            "## 文件", "",
            "- 首帧：`first-frame-prompt.md`",
            "- 尾帧：`last-frame-prompt.md`",
            "- 视频剧情提示词：`video-prompt.md`", "",
        ]
        return "\n".join(parts).rstrip() + "\n"
    intro = {
        "I2VA": "首帧把人物、道具和场景融合在同一画面中；视频提示词从该首帧画面出发向前发展。",
        "L2VA": "尾帧把人物、道具和场景融合在同一画面中；视频提示词从合理开场逐步收敛到该尾帧画面。",
    }[variant]
    parts = [
        f"# {variant} 关键帧提示词", "",
        intro, "",
        f"- 场景锚点：{bundle.scene_anchor}",
        f"- 视频 Profile：`{bundle.profile_id}`",
        f"- 视频提示词节点：`{bundle.prompt_node_id}`（输入：`{bundle.prompt_input}`）",
    ]
    if bundle.first:
        parts.append(f"- 首帧输入槽：`{bundle.first_frame_slot_id}`")
    if bundle.last:
        parts.append(f"- 尾帧输入槽：`{bundle.last_frame_slot_id}`")
    parts.extend(["", "## 连续性约束", "", *[f"- {item}" for item in bundle.continuity_constraints], "", "## 文件", ""])
    if bundle.first:
        parts.append("- 首帧：`first-frame-prompt.md`")
    if bundle.last:
        parts.append("- 尾帧：`last-frame-prompt.md`")
    parts.append("- 视频剧情提示词：`video-prompt.md`")
    parts.append("")
    return "\n".join(parts).rstrip() + "\n"
def render_generation(result: GenerationResult) -> dict[str, str]:
    """返回当前生成模式的可复制产物；FL2VA 只输出融合首尾帧。

    FL2VA 的产物里**没有**资产图提示词（#40 回滚，依据 #41 AC1）：那是多参素材循环的
    产物。#40 曾在这一支追加 character/prop/scene 三键，帧链用户凭空多拿到三份多参文件。

    路线判定走 ``brief_parser.is_ref_mode``（issue #78 / 父 spec #77）：非多参只返回
    剧本与视频提示词。判据取 ``result.mode``，**不能**按 ``result.image_prompts`` 空不空判——
    修复前的 base generation.json 里还留着三键，按空表判会把三份 md 重新渲染出来。
    也不许把它们渲染成空行：``render_image_prompt_markdown`` 会把空行回落成设计正文，
    那正是「设计正文当生图提示词」的历史现场。
    """
    if result.fl2va_prompt_bundle is not None:
        return {
            "script": result.script,
            "video": result.artifact("video").content,
            "fl2va": render_fl2va_markdown(result),
            "first-frame": render_fl2va_frame_markdown(result, "first"),
            "last-frame": render_fl2va_frame_markdown(result, "last"),
        }
    if not is_ref_mode(result.mode):
        return {
            "script": result.script,
            "video": result.artifact("video").content,
        }
    return {
        "script": result.script,
        "video": result.artifact("video").content,
        "character": render_image_prompt_markdown(result, "character"),
        "prop": render_image_prompt_markdown(result, "prop"),
        "scene": render_image_prompt_markdown(result, "scene"),
    }


__all__ = [
    _REF_WORKFLOW_ALIAS,
    "GenerationResult", "PromptArtifact", "PromptKind", "ImagePromptVariant",
    "FL2VAFramePrompt", "FL2VAPromptBundle", "brief_snapshot",
    "ImageProfile", "IMAGE_PROFILE_DEFAULTS", "IMAGE_FAMILY_LABELS", "RETIRED_IMAGE_FAMILIES",
    "is_retired_image_family", "image_canvas_size",
    "composition_set", "composition_hint", "canvas_family", "canvas_display",
    "RatioPreset", "CANVAS_RATIO_PRESETS",
    "asset_prompts_for", "fl2va_bundle_from_dict", "generation_directory",
    "ref_asset_registry", "render_generation",
    "strip_foreign_frames",
    "render_fl2va_frame_markdown", "render_fl2va_markdown", "result_from_state", "variant_frame_names",
    "scene_requirement_groups", "validate_fl2va_bundle",
]
