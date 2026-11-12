"""诊断包端点：``GET /api/v1/diagnostics/bundle``（#239 / spec #235 §8.2 + §13）。

这是 D5 唯一新增的业务端点（§13），其余端点与十种事件类型都不动。它**不在豁免面上**：
路径在 ``/api`` 下且不在 ``auth.is_exempt`` 的闭合三处里，中间件照常要凭据。

响应体的组装在 ``minimax_h3_prompt.diagnostics``，这里只负责「读 ``app.state`` →
调组装 → 包成 zip 下载响应」。端口与形态从 ``app.state.startup``（``StartupInfo``）
读；测试 app 没有它时组装层自己兜底，不在这里抛 500。
"""
from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Request, Response

from ... import diagnostics
from ..problems import AUTH_ERROR_RESPONSES, COMMON_ERROR_RESPONSES

router = APIRouter(prefix="/api/v1", tags=["diagnostics"],
                   responses=AUTH_ERROR_RESPONSES)

_BUNDLE_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {
        "description": "诊断包 zip：清单、环境信息、脱敏配置快照、日志窗口、"
                       "各会话状态与事件流、文本产物副本（不含密钥与素材媒体）。",
        "content": {
            "application/zip": {"schema": {"type": "string", "format": "binary"}},
        },
        "headers": {
            "Content-Disposition": {
                "description": "attachment；文件名按 RFC 5987 编码（filename*=UTF-8''…）",
                "schema": {"type": "string"},
            },
            "Cache-Control": {
                "description": "恒为 no-store（诊断包含本机路径，不许被缓存）",
                "schema": {"type": "string"},
            },
        },
    },
    **COMMON_ERROR_RESPONSES,
}


def _retention_days() -> int | None:
    """配置里的日志保留天数；配置取不到时返回 ``None``（组装层用默认 7）。

    诊断包的职责是**拿得到现场**：配置本身坏了，恰恰是最需要导出诊断包的时候，
    不能因为读不到一个数字就回 500。
    """
    try:
        from ...config import config
    except Exception:  # noqa: BLE001 - 见 docstring
        return None
    value = getattr(config, "log_retention_days", None)
    return value if isinstance(value, int) else None


@router.get(
    "/diagnostics/bundle",
    response_class=Response,
    responses=_BUNDLE_RESPONSES,
)
def get_diagnostics_bundle(request: Request) -> Response:
    """导出诊断包（``application/zip`` + ``attachment``，spec §8.2）。"""
    app = request.app
    payload, filename = diagnostics.build_bundle(
        app.state.session_store,
        version=getattr(app.state, "version", "") or "",
        startup=getattr(app.state, "startup", None),
        retention_days=_retention_days(),
    )
    return Response(
        content=payload,
        media_type="application/zip",
        headers={
            # RFC 5987：与媒体/产物的下载头同一口径（``api/media.py``）。
            "Content-Disposition": (
                f"attachment; filename*=UTF-8''{quote(filename, safe='')}"),
            "Cache-Control": "no-store",
        },
    )
