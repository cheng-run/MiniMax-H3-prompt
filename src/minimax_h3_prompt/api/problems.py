"""RFC 9457 Problem Details 错误契约。

为什么要有：前端只需要认识一种错误形状，不必各自去猜 FastAPI 默认的 ``detail``。
所有失败路径（StarletteHTTPException、请求校验失败、未处理异常）都转成
``application/problem+json``。500 绝不回传堆栈、内部路径或密钥——只记服务端日志。

契约字段见 D1 spec §7.3。成功响应不加 ``data`` 套壳（见 health 路由）。
"""
from __future__ import annotations

import http
import logging
from typing import Any

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
    400: "请求无法解析",
    401: "需要访问凭据",
    403: "禁止访问",
    404: "资源不存在",
    405: "方法不允许",
    409: "资源状态冲突",
    # 413 既用于产物、也用于素材上传（#224）：标题写「内容」，不替某一类产物说话。
    413: "内容过大",
    415: "不支持的媒体类型",
    416: "请求的字节范围无法满足",
    422: "请求参数校验失败",
    500: "服务器内部错误",
    501: "功能尚未实现",
    507: "存储不可用",
}


class ProblemError(Exception):
    """业务层可抛出、由统一处理器落成 problem+json 的错误。

    ``code`` 是前端分支用的稳定错误码（spec §7），与 HTTP 状态码并存：
    状态码说「哪一类失败」，``code`` 说「具体哪条规则」。
    """

    status_code = 500
    code = "internal_error"

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


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


def build_problem_response(
    status: int,
    detail: str,
    path: str,
    *,
    code: str | None = None,
    errors: list[ProblemFieldError] | None = None,
) -> JSONResponse:
    """按 RFC 9457 构造 problem+json。

    单独暴露给**中间件**用：访问控制在路由外层，拿不到异常处理器，也没有 Request
    （只有 ASGI scope）。它只需要路径来做 ``instance``，所以这里收路径而不是 Request。
    """
    problem = ProblemDetail(
        title=_title_for(status),
        status=status,
        detail=detail,
        instance=path,
        code=code,
        errors=errors,
    )
    return JSONResponse(
        status_code=status,
        content=problem.model_dump(exclude_none=True),
        media_type=PROBLEM_CONTENT_TYPE,
    )


def _problem_response(
    status: int,
    detail: str,
    request: Request,
    errors: list[ProblemFieldError] | None = None,
    code: str | None = None,
) -> JSONResponse:
    """统一构造 ProblemDetail 响应：title/instance 由状态码与请求路径派生，避免三条
    失败路径各拼一遍样板（改字段时只改这一处）。"""
    return build_problem_response(
        status, detail, request.url.path, code=code, errors=errors)


def install_problem_handlers(app: FastAPI) -> None:
    """把三类失败路径都挂到统一的 problem+json 处理器上。"""

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        return _problem_response(exc.status_code, str(exc.detail), request)

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        errors = [
            ProblemFieldError(loc=list(err["loc"]), msg=err["msg"], type=err["type"])
            for err in exc.errors()
        ]
        return _problem_response(422, "请求参数未通过校验。", request, errors=errors)

    @app.exception_handler(ProblemError)
    async def _handle_problem_error(
        request: Request, exc: ProblemError
    ) -> JSONResponse:
        return _problem_response(
            exc.status_code, exc.detail, request, code=exc.code)

    # 工作台领域错误自带 HTTP 状态与稳定错误码（#215）：在这里统一落成 problem+json，
    # 路由只负责抛语义错误，不各自拼响应体。
    from ..workbench_store import WorkbenchError

    @app.exception_handler(WorkbenchError)
    async def _handle_workbench_error(
        request: Request, exc: WorkbenchError
    ) -> JSONResponse:
        return _problem_response(
            exc.status_code, str(exc), request, code=exc.code)

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        # 只在服务端记录细节；响应体保持安全、稳定，不回传堆栈或内部路径。
        logger.exception("未处理异常：%s %s", request.method, request.url.path)
        return _problem_response(500, "服务器内部错误，请联系维护者。", request)


# 每个 API 路由声明同一组通用错误响应：既让契约里出现 ProblemDetail，也让读者一眼
# 看到这些状态码都可能以 problem+json 返回。显式标注类型：FastAPI 的 responses 参数
# 要 dict[int | str, dict[str, Any]]，否则 mypy 会把不透明值推断成 dict[str, object]。
COMMON_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    404: {"model": ProblemDetail, "description": "资源不存在（problem+json）"},
    422: {"model": ProblemDetail, "description": "请求参数校验失败（problem+json）"},
    500: {"model": ProblemDetail, "description": "服务器内部错误（problem+json）"},
}

# 本地访问控制在中间件层先于路由拒绝（api/auth.py），所以「没有凭据」与「来源不对」
# 对**所有**受保护路由都可能发生。不写进契约，前端生成类型里就看不到这两个分支——
# 契约说「只有 404/422/500」，服务端却还会回 401/403，正是本模块开头反对的两处口径。
AUTH_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"model": ProblemDetail, "description": "缺少或无效的工作台访问令牌（problem+json）"},
    403: {"model": ProblemDetail, "description": "Host/Origin 不是本机回环地址（problem+json）"},
}


def apply_problem_media_type_openapi(app: FastAPI) -> None:
    """让附加错误响应在 OpenAPI 里声明 ``application/problem+json``。

    FastAPI 的 ``responses={...: {"model": ...}}`` 一律把内容标成 ``application/json``，
    与运行时真实返回的 ``problem+json`` 不一致——契约说一套、服务端做一套。这里在生成
    OpenAPI 时把「内容 schema 指向 ProblemDetail」的响应媒体键改写过来，使运行时
    ``/openapi.json`` 与入库的 ``contracts/openapi.json`` 同口径；``model`` 注册仍在，
    所以 ProblemDetail 继续留在 ``components.schemas`` 里被 ``$ref`` 引用。
    """
    original_openapi = app.openapi

    def _custom_openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        schema = original_openapi()
        for path_item in schema.get("paths", {}).values():
            for key, operation in path_item.items():
                if not isinstance(operation, dict) or key in {"parameters", "servers"}:
                    continue
                for response in operation.get("responses", {}).values():
                    content = response.get("content")
                    if not isinstance(content, dict):
                        continue
                    # 媒体键可能是 ``application/json``（普通路由），也可能是路由声明的
                    # 默认响应类型（例如 ``PlainTextResponse`` 路由下的 ``text/plain``）：
                    # 判据只看内容 schema 是否指向 ProblemDetail，不认键名。
                    for media_type, body in list(content.items()):
                        if not isinstance(body, dict):
                            continue
                        ref = body.get("schema", {}).get("$ref", "")
                        if ref.endswith("/ProblemDetail"):
                            content[PROBLEM_CONTENT_TYPE] = content.pop(media_type)
                            break
        app.openapi_schema = schema
        return schema

    app.openapi = _custom_openapi  # type: ignore[method-assign]
