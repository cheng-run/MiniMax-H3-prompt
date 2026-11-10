"""素材媒体读取：``GET/HEAD /api/v1/media/{asset_id:path}``（#222 §6，D3-3）。

为什么单独一个路由模块：与 ``assets`` 同一条理由——``{asset_id:path}`` 会吞斜杠，
媒体路径自带会话标识里的斜杠，放回 ``sessions.py`` 只会让「谁写在前面」重新变成
人记的规矩。Range 语义与字节响应住 ``api/media.py``，成片的 content 端点共用它。

GET 与 HEAD 分成两个路由而不是共用一个 ``methods=[...]``：HEAD 的成功响应按定义
没有响应体，共用一份 ``responses`` 会让契约声称 HEAD 也回视频字节（错误响应两条
路径同形，仍是 problem+json）。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response

from ...workbench_store import ASSET_MEDIA_TYPES
from ..media import (
    MEDIA_RESPONSE_HEADERS,
    RANGE_RESPONSE_HEADERS,
    binary_content,
    build_media_response,
)
from ..problems import AUTH_ERROR_RESPONSES, ProblemDetail

router = APIRouter(prefix="/api/v1", tags=["media"], responses=AUTH_ERROR_RESPONSES)

# 可选 Content-Type 从素材格式表派生：加一种上传格式就自动出现在契约里，不必再
# 回来手抄一遍（抄漏的时候契约看着齐全、运行时却回了个没声明的类型）。
_MEDIA_CONTENT: dict[str, Any] = binary_content(ASSET_MEDIA_TYPES.values())

_MEDIA_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {
        "description": "整份字节；Content-Type 由素材的实际格式决定。",
        "content": _MEDIA_CONTENT,
        "headers": MEDIA_RESPONSE_HEADERS,
    },
    206: {
        "description": "单段字节范围（Range: bytes=…）。",
        "content": _MEDIA_CONTENT,
        "headers": RANGE_RESPONSE_HEADERS,
    },
    404: {"model": ProblemDetail,
          "description": "素材不存在、标识越界，或不是素材命名形态（problem+json）"},
    416: {"model": ProblemDetail,
          "description": "Range 越界、语法非法或多段（problem+json）；带 Content-Range: bytes */<len>"},
    # 显式声明 422，别让 FastAPI 拿它的默认 HTTPValidationError 顶上来：本仓所有
    # 失败都走 problem+json，契约里多一套校验错误 schema 就是两处口径。
    422: {"model": ProblemDetail, "description": "请求参数校验失败（problem+json）"},
    500: {"model": ProblemDetail, "description": "服务器内部错误（problem+json）"},
}

# HEAD 没有**成功响应**的响应体，所以 200/206 只声明头；错误仍是 problem+json
# （错误体由统一处理器产出，与 GET 同形——HEAD 的差异只在成功那条路径上）。
_HEAD_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {"description": "与 GET 同头、无体。", "headers": MEDIA_RESPONSE_HEADERS},
    206: {"description": "与 GET 同头、无体。", "headers": RANGE_RESPONSE_HEADERS},
    404: _MEDIA_RESPONSES[404],
    416: _MEDIA_RESPONSES[416],
    422: _MEDIA_RESPONSES[422],
    500: _MEDIA_RESPONSES[500],
}


def _read_media(asset_id: str, request: Request) -> Response:
    media = request.app.state.session_store.resolve_asset_media(asset_id)
    return build_media_response(
        media,
        request.headers.get("range"),
        request_path=request.url.path,
        head=request.method == "HEAD",
    )


@router.get(
    "/media/{asset_id:path}",
    response_class=Response,
    responses=_MEDIA_RESPONSES,
)
def get_media(asset_id: str, request: Request) -> Response:
    """读素材字节；``Range`` 只支持单段（#222 §6.2）。"""
    return _read_media(asset_id, request)


@router.head(
    "/media/{asset_id:path}",
    response_class=Response,
    responses=_HEAD_RESPONSES,
)
def head_media(asset_id: str, request: Request) -> Response:
    """探测素材：头与 GET 一致、无体；供播放器决定要不要发 Range。"""
    return _read_media(asset_id, request)
