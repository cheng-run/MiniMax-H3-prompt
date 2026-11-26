"""画布比例档只读端点：``GET /api/v1/canvas-presets``（#364 / spec #363）。

**为什么是独立路径而不是会话子资源**：表单在**建会话之前**就要渲染比例档选择器，
此时会话还没有标识（与 ``clarification-questions`` 同一形态）。放独立段也顺带避开
``{session_id:path}`` 吞斜杠那条路由顺序规矩。

**为什么返回服务端常量**：前端一旦把那张比例档表抄一份，以后加一档比例时界面与后端
推导就会分家——用户按界面选、拿到的尺寸却不是后端会跑的那个（#42 起这张表就是
``generation.CANVAS_RATIO_PRESETS`` 的唯一真源）。这里只做投影，**不复制表**。
"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from ..problems import AUTH_ERROR_RESPONSES, COMMON_ERROR_RESPONSES

router = APIRouter(prefix="/api/v1", tags=["canvas"], responses=AUTH_ERROR_RESPONSES)

_RESPONSES = COMMON_ERROR_RESPONSES


class CanvasSizeOut(BaseModel):
    """一档预设尺寸（像素宽高）。字段名与创建口的 ``canvas`` 同形，前端可直接回填。"""

    width: int
    height: int


class RatioPresetOut(BaseModel):
    """比例档（``generation.RatioPreset`` 的投影）。

    ``w_ratio / h_ratio`` 是比例的宽/高**份数**不是像素——与 ``RatioPreset`` 同名同义，
    前端据此显示「16:9」并知道约分后的份数。
    """

    name: str
    w_ratio: int
    h_ratio: int
    sizes: list[CanvasSizeOut]


class FallbackCanvasOut(BaseModel):
    """某生图模型未选画布时的兜底尺寸（``generation.IMAGE_PROFILE_DEFAULTS`` 的一行）。"""

    model: str
    label: str
    width: int
    height: int


class CanvasPresetsOut(BaseModel):
    """比例档表 ＋ 向导会问的家族 ＋ 各生图模型的兜底画布。

    ``canvas_choice_families`` 是「哪几档会被问画布」（``CANVAS_CHOICE_FAMILIES``）：
    其余档没有选定值，直接回落 ``fallback_canvases`` 里自己那一行。前端用它决定
    选择器影响面，不在界面里硬编码家族名。
    """

    presets: list[RatioPresetOut]
    canvas_choice_families: list[str]
    fallback_canvases: list[FallbackCanvasOut]


@router.get(
    "/canvas-presets",
    response_model=CanvasPresetsOut,
    responses=_RESPONSES,
)
def list_canvas_presets() -> CanvasPresetsOut:
    """比例档与兜底画布（只读：不建会话、不落盘）。

    数据**现取** ``generation`` 的常量：这张表是 CLI 向导与请求【画布】块的同一份
    真源，投影一份字面量就等于开了第二处口径。
    """
    from ...generation import (
        CANVAS_CHOICE_FAMILIES,
        CANVAS_RATIO_PRESETS,
        IMAGE_FAMILY_LABELS,
        IMAGE_PROFILE_DEFAULTS,
    )

    return CanvasPresetsOut(
        presets=[
            RatioPresetOut(
                name=preset.name,
                w_ratio=preset.w_ratio,
                h_ratio=preset.h_ratio,
                sizes=[CanvasSizeOut(width=width, height=height)
                       for width, height in preset.sizes],
            )
            for preset in CANVAS_RATIO_PRESETS
        ],
        canvas_choice_families=list(CANVAS_CHOICE_FAMILIES),
        fallback_canvases=[
            FallbackCanvasOut(
                model=family,
                label=IMAGE_FAMILY_LABELS[family],
                width=profile.width,
                height=profile.height,
            )
            for family, profile in IMAGE_PROFILE_DEFAULTS.items()
        ],
    )
