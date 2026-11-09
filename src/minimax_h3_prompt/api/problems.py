"""RFC 9457 Problem Details 错误契约。

为什么要有：前端只需要认识一种错误形状，不必各自去猜 FastAPI 默认的 ``detail``。
所有失败路径（StarletteHTTPException、请求校验失败、未处理异常）都转成
``application/problem+json``。500 绝不回传堆栈、内部路径或密钥——只记服务端日志。

契约字段见 D1 spec §7.3。成功响应不加 ``data`` 套壳（见 health 路由）。
"""
from __future__ import annotations

import http
import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)

# Problem Details 的媒体类型：前端据此识别「这是本服务的标准错误」。
PROBLEM_CONTENT_TYPE = "application/problem+json"

# 常见状态码的中文短标题；未登记的退回 HTTP 标准短语。
_TITLE_BY_STATUS = {
    403: "禁止访问",
    404: "资源不存在",
    405: "方法不允许",
    422: "请求参数校验失败",
    500: "服务器内部错误",
}


class ProblemFieldError(BaseModel):
    """422 校验错误元素：``{loc, msg, type}``。"""

    loc: list[str | int]
    msg: str
    type: str


class ProblemDetail(BaseModel):
    """统一错误资源（RFC 9457）。"""

    type: str = "about:blank"
    title: str
    status: int
    detail: str
    instance: str
    code: str | None = None
    errors: list[ProblemFieldError] | None = None


def _title_for(status: int) -> str:
    if status in _TITLE_BY_STATUS:
        return _TITLE_BY_STATUS[status]
    try:
        return http.HTTPStatus(status).phrase
    except ValueError:
        return "请求错误"


def _problem_response(problem: ProblemDetail) -> JSONResponse:
    return JSONResponse(
        status_code=problem.status,
        content=problem.model_dump(exclude_none=True),
        media_type=PROBLEM_CONTENT_TYPE,
    )


def install_problem_handlers(app: FastAPI) -> None:
    """把三类失败路径都挂到统一的 problem+json 处理器上。"""

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        problem = ProblemDetail(
            title=_title_for(exc.status_code),
            status=exc.status_code,
            detail=str(exc.detail),
            instance=request.url.path,
        )
        return _problem_response(problem)

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        errors = [
            ProblemFieldError(loc=list(err["loc"]), msg=err["msg"], type=err["type"])
            for err in exc.errors()
        ]
        problem = ProblemDetail(
            title=_title_for(422),
            status=422,
            detail="请求参数未通过校验。",
            instance=request.url.path,
            errors=errors,
        )
        return _problem_response(problem)

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        # 只在服务端记录细节；响应体保持安全、稳定，不回传堆栈或内部路径。
        logger.exception("未处理异常：%s %s", request.method, request.url.path)
        problem = ProblemDetail(
            title=_title_for(500),
            status=500,
            detail="服务器内部错误，请联系维护者。",
            instance=request.url.path,
        )
        return _problem_response(problem)


# 每个 API 路由声明同一组通用错误响应：既让契约里出现 ProblemDetail，也让读者一眼
# 看到这些状态码都可能以 problem+json 返回。
COMMON_ERROR_RESPONSES = {
    404: {"model": ProblemDetail, "description": "资源不存在（problem+json）"},
    422: {"model": ProblemDetail, "description": "请求参数校验失败（problem+json）"},
    500: {"model": ProblemDetail, "description": "服务器内部错误（problem+json）"},
}
