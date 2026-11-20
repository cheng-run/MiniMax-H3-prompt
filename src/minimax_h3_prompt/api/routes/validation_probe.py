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


@router.get("/fault")
def fault() -> None:
    """抛一个 5xx 的**领域**错误（走 ``ProblemError`` 处理器，不是兜底那一档）。

    #348：按异常类型分发的处理器原先一条日志都不写，#340 验收里的一次 502 就这样
    在服务端日志里毫无记录。这条路由让「5xx 要留痕、4xx 不写」可被测试驱动。
    """
    from ..problems import ProblemError

    class UpstreamFault(ProblemError):
        status_code = 502
        code = "upstream_fault"

    raise UpstreamFault("上游服务响应失败。")


@router.get("/client-error")
def client_error() -> None:
    """抛一个 4xx 的领域错误：它**不该**产生 error 日志（#348 的反向断言）。"""
    from ..problems import ProblemError

    class ClientFault(ProblemError):
        status_code = 409
        code = "client_conflict"

    raise ClientFault("资源状态冲突。")
