"""引导端点：``GET /api/v1/auth/token``（#222 §4.4）。

它本身**不设防**，这是有意的：服务不设 CORS，跨站页面读不到响应体；Cookie 是
``SameSite=Strict``，跨站也用不上。本机进程能拿到——可它们本来就能直接读写
``output/sessions/``，不在威胁模型内。**令牌挡的是跨站页面，不是本机进程。**

同一个值同时下发 Cookie：``<video src>`` / ``<img src>`` 由浏览器自己发请求，
挂不上自定义头，媒体端点只认头就永远 401（#222 §4.5）。
"""
from __future__ import annotations

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

from ..auth import COOKIE_PATH, SESSION_COOKIE
from ..problems import COMMON_ERROR_RESPONSES

router = APIRouter(prefix="/api/v1", tags=["auth"])


class AuthTokenResponse(BaseModel):
    """启动令牌资源：字段名即契约，前端据此缓存到内存。"""

    token: str


@router.get(
    "/auth/token",
    response_model=AuthTokenResponse,
    responses=COMMON_ERROR_RESPONSES,
)
def get_auth_token(request: Request, response: Response) -> AuthTokenResponse:
    policy = request.app.state.local_access
    # 不设 Max-Age/Expires：会话 Cookie，随进程存活；服务重启令牌换新，旧 Cookie 失效。
    # 不设 Secure：本机走 http，Secure 会让浏览器直接不收。
    response.set_cookie(
        SESSION_COOKIE,
        policy.token,
        path=COOKIE_PATH,
        httponly=True,
        samesite="strict",
    )
    # 令牌绝不能被任何中间层缓存。
    response.headers["Cache-Control"] = "no-store"
    return AuthTokenResponse(token=policy.token)
