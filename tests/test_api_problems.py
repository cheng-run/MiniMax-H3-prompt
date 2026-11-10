"""D1-2（#207）：统一错误契约（RFC 9457 problem+json）。

钉住 404 / 422 / 500 三种失败路径都返回 ``application/problem+json``，关键字段齐全；
500 绝不泄漏堆栈、绝对路径或密钥；导出的 OpenAPI 里有 ``ProblemDetail`` 且健康接口
声明了通用错误响应。
"""
from __future__ import annotations

from fastapi.testclient import TestClient
from workbench_client import workbench_client


def _test_client() -> TestClient:
    from minimax_h3_prompt.api.app import create_app

    # raise_server_exceptions=False：让未处理异常走到我们注册的 500 problem 处理器，
    # 而不是被 TestClient 直接重抛——否则测不到真实响应体。
    return workbench_client(
        create_app(include_test_routes=True), raise_server_exceptions=False)


def _assert_is_problem(resp) -> dict:
    assert resp.headers["content-type"].startswith("application/problem+json")
    return resp.json()


def test_unknown_api_path_returns_problem_404() -> None:
    resp = _test_client().get("/api/v1/this-path-does-not-exist")

    assert resp.status_code == 404
    body = _assert_is_problem(resp)
    assert body["status"] == 404
    assert body["type"] == "about:blank"
    assert body["instance"] == "/api/v1/this-path-does-not-exist"
    assert body["title"]
    assert body["detail"]


def test_validation_error_becomes_problem_422_with_readable_errors() -> None:
    resp = _test_client().get("/api/v1/_probe/validate", params={"count": "not-an-int"})

    assert resp.status_code == 422
    body = _assert_is_problem(resp)
    assert body["status"] == 422
    errors = body["errors"]
    assert isinstance(errors, list) and errors
    assert {"loc", "msg", "type"} <= set(errors[0])


def test_unhandled_exception_returns_safe_problem_500() -> None:
    resp = _test_client().get("/api/v1/_probe/boom")

    assert resp.status_code == 500
    body = _assert_is_problem(resp)
    assert body["status"] == 500
    payload = resp.text
    # 500 响应不能带出堆栈、绝对路径或密钥。
    assert "Traceback" not in payload
    assert "secret-token" not in payload
    assert "C:\\" not in payload and "D:\\" not in payload


def test_openapi_contains_problem_detail_and_common_error_responses() -> None:
    from minimax_h3_prompt.api.app import create_app

    schema = create_app().openapi()
    assert "ProblemDetail" in schema["components"]["schemas"]

    health_responses = schema["paths"]["/api/v1/health"]["get"]["responses"]
    for code in ("404", "422", "500"):
        assert code in health_responses
