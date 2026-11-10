"""本地访问控制：Host / Origin 校验 + 每进程启动令牌（#222 §4，D3-1）。

威胁模型只有一条：**挡住跨站页面**。本机进程本来就能直接读写 ``output/sessions/``，
所以 ``/api/v1/auth/token`` 不设防不是漏洞；令牌的用处是让「别处的网页」拿不到它。
这句话要跟着 ADR 0033 一起读，别把这里当通用认证。

三层里只实现后两层（只绑回环由 ``__main__.py`` 的 HOST 决定）：

1. ``Host`` 主机名必须是回环，**端口不限**——开发期 Vite 代理 ``changeOrigin: false``，
   浏览器过来的 Host 是 ``localhost:5173``；
2. 带 ``Origin`` 时主机名同样必须在回环集合内；没有 Origin 的请求（curl、本机脚本）
   放行，但凭据照要；
3. 凭据「请求头 **或** Cookie」二选一：``<video src>`` / ``<img src>`` 由浏览器自己
   发请求，挂不上自定义头，只认头就永远 401。

错误分码（``host_forbidden`` / ``origin_forbidden`` / ``auth_required``）是有意的：
排障时「改 Host」与「补令牌」是两件不同的事。
"""
from __future__ import annotations

import hmac
import logging
import secrets
from dataclasses import dataclass
from http.cookies import SimpleCookie
from urllib.parse import urlsplit

from fastapi import FastAPI
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from .problems import build_problem_response

logger = logging.getLogger(__name__)

TOKEN_HEADER = "X-Workbench-Token"
SESSION_COOKIE = "wb_session"
# Cookie 只发给 API：静态资源与 SPA 兜底是豁免面，没必要让它们带上凭据。
COOKIE_PATH = "/api/v1"
TOKEN_BYTES = 32

LOOPBACK_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "::1"})

# 豁免面是**闭合**的三处（#222 §4.3）：健康检查、引导端点、静态资源与 SPA 兜底。
# 最后一项按「不在 /api 下」判定，因此 /openapi.json 与 /docs 一并豁免。
# 前两处 spec 写的是 GET：PUT/POST 打到这些路径不应得到「免凭据」待遇，否则
# 「401 与 405 的差别」会泄露这两个路由存在。
EXEMPT_API_PATHS = frozenset({"/api/v1/health", "/api/v1/auth/token"})
_EXEMPT_METHODS = frozenset({"GET", "HEAD"})

_DETAILS = {
    "host_forbidden": "Host 主机名不在允许的本机回环集合内。",
    "origin_forbidden": "Origin 主机名不在允许的本机回环集合内。",
    "auth_required": "缺少或无效的工作台访问令牌。",
}


def _host_header_hostname(value: str) -> str | None:
    """从 ``Host`` 取主机名；取不出就是 None。

    ``urlsplit("//…")`` 会去掉方括号与端口并小写化；端口是否合法不在这里判——端口
    不限，``urlsplit("//host:abc").hostname`` 仍能给出 ``host``，那是我们要的语义。
    """
    try:
        return urlsplit(f"//{value}").hostname
    except ValueError:
        return None


def _origin_hostname(value: str) -> str | None:
    try:
        return urlsplit(value).hostname
    except ValueError:
        return None


def _cookie_token(headers: Headers) -> str | None:
    raw = headers.get("cookie")
    if not raw:
        return None
    jar = SimpleCookie()
    try:
        jar.load(raw)
    except Exception:  # noqa: BLE001 - 畸形 Cookie 头只表示「没有有效凭据」
        return None
    morsel = jar.get(SESSION_COOKIE)
    return morsel.value if morsel is not None else None


def _matches(presented: str | None, expected: str) -> bool:
    """常量时间比较；**按字节比**，畸形凭据只该得到 False。

    为什么不能直接 ``compare_digest(str, str)``：请求头按 latin-1 解码成 str，
    任意非 ASCII 字节（obs-text）都会让它抛 ``TypeError``——中间件在异常处理器
    外层，一个畸形头就能把 401 打成 500。
    """
    if not isinstance(presented, str):
        return False
    return hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))


def is_exempt(method: str | None, path: str) -> bool:
    """豁免面判定：闭合三处（#222 §4.3），见 ``EXEMPT_API_PATHS`` 的说明。"""
    if path in EXEMPT_API_PATHS:
        return method in _EXEMPT_METHODS
    return not (path == "/api" or path.startswith("/api/"))


@dataclass(frozen=True)
class LocalAccessPolicy:
    """一次进程生命周期的访问策略；令牌只存内存，重启即失效。"""

    token: str

    @classmethod
    def generate(cls) -> LocalAccessPolicy:
        return cls(token=secrets.token_urlsafe(TOKEN_BYTES))

    def denial(
        self, headers: Headers, method: str | None, path: str,
    ) -> tuple[int, str] | None:
        """返回 ``(状态码, 错误码)``；None 表示放行。"""
        host = headers.get("host")
        if host is None or _host_header_hostname(host) not in LOOPBACK_HOSTNAMES:
            return 403, "host_forbidden"
        origin = headers.get("origin")
        if origin is not None and _origin_hostname(origin) not in LOOPBACK_HOSTNAMES:
            return 403, "origin_forbidden"
        if is_exempt(method, path):
            return None
        if _matches(headers.get(TOKEN_HEADER), self.token):
            return None
        if _matches(_cookie_token(headers), self.token):
            return None
        return 401, "auth_required"


class LocalAccessMiddleware:
    """纯 ASGI 中间件：请求体不读、路由不碰，只在入口处判 Host/Origin/凭据。

    不用 ``BaseHTTPMiddleware``：它会包一层流，对 SSE（D2 的事件流）是多余的变量；
    这里只需要看头，纯 ASGI 的代价最低。错误响应必须**自己**构造——中间件在
    Starlette 的异常处理器外层，抛 ``HTTPException`` 只会变成 500。
    """

    def __init__(self, app: ASGIApp, *, policy: LocalAccessPolicy) -> None:
        self.app = app
        self.policy = policy

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        denial = self.policy.denial(Headers(scope=scope), scope.get("method"), scope["path"])
        if denial is None:
            await self.app(scope, receive, send)
            return
        status, code = denial
        # 日志只留路径与错误码：令牌永远不进日志，Host/Origin 原值也不回显。
        logger.warning("访问被拒绝：%s %s → %s", scope.get("method"), scope["path"], code)
        response = build_problem_response(
            status, _DETAILS[code], scope["path"], code=code)
        await response(scope, receive, send)


def attach_local_access(app: FastAPI) -> LocalAccessPolicy:
    """给 app 装上策略与中间件，并把策略放进 ``app.state`` 供端点读取。

    令牌在 ``create_app`` 阶段生成：一个进程一份，测试里每个 app 也各一份。
    """
    policy = LocalAccessPolicy.generate()
    app.state.local_access = policy
    app.add_middleware(LocalAccessMiddleware, policy=policy)
    return policy
