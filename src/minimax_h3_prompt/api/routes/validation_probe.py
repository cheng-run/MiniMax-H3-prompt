"""测试专用路由：证明 422 校验失败真的被转成 problem+json，以及 500 处理器安全。

只在 ``create_app(include_test_routes=True)`` 时挂载，绝不进入生产入口或导出的
OpenAPI 契约（CI 的 drift check 用默认 ``create_app()``，看不到它）。
"""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/v1/_probe", tags=["_probe"])


@router.get("/validate")
def validate(count: int) -> dict[str, int]:
    """``count`` 是 int；传非整数即触发 RequestValidationError → 422 problem。"""
    return {"count": count}


@router.get("/boom")
def boom() -> None:
    """故意抛未处理异常，且消息里塞入密钥与绝对路径，用来验证 500 响应会滤掉它们。"""
    raise RuntimeError("secret-token=abc123 at D:\\secret\\internal\\path.py")
