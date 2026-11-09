"""健康检查接口：``GET /api/v1/health``。

返回裸资源（不套 ``data``），带 ``Cache-Control: no-store``——健康状态不该被任何
中间层缓存。版本号由 app 在启动时从包元数据解析后放进 ``app.state``，路由只读，
不再重复解析（读不到时 ``create_app`` 会先失败，见 ``api/app.py``）。
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

from .. import SERVICE_NAME

router = APIRouter(prefix="/api/v1", tags=["health"])


class HealthResponse(BaseModel):
    """健康状态资源。字段约定见 D1 spec §7.1。"""

    status: Literal["ok"]
    service: str
    version: str


@router.get("/health", response_model=HealthResponse)
def get_health(request: Request, response: Response) -> HealthResponse:
    response.headers["Cache-Control"] = "no-store"
    return HealthResponse(
        status="ok",
        service=SERVICE_NAME,
        version=request.app.state.version,
    )
