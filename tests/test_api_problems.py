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


# ---------------------------------------------------------------------------
# 5xx 才记 error 日志（#348，父 #296 的 F5）
# ---------------------------------------------------------------------------
# 为什么要有：按异常类型分发的三个处理器（StarletteHTTPException / ProblemError /
# WorkbenchError）原先**一条日志都不写**，2026-11-19 #340 验收里的一次 502
# （clarification_generation_failed）在 logs/app-*.log 里毫无记录，病因只能靠复现猜。
# 判据是「状态码 ≥ 500」——4xx 是正常客户端错误，记 error 只会淹没真故障。


def _problem_logs(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records
            if r.name.startswith("minimax_h3_prompt.api.problems")]


def test_server_error_domain_fault_is_logged_with_code_and_path(caplog) -> None:
    """502 走领域错误处理器时要留一条 error 日志，含错误码、方法与路径、异常类名。"""
    import logging

    with caplog.at_level(logging.ERROR, logger="minimax_h3_prompt.api.problems"):
        resp = _test_client().get("/api/v1/_probe/fault")

    assert resp.status_code == 502
    # 响应体一字不变：改了日志不许动契约。
    body = _assert_is_problem(resp)
    assert body["status"] == 502
    assert body["code"] == "upstream_fault"

    messages = _problem_logs(caplog)
    assert messages, "5xx 领域错误没有产生任何日志（#348 的原始缺陷）"
    joined = "\n".join(messages)
    assert "upstream_fault" in joined, "日志里没有错误码"
    assert "/api/v1/_probe/fault" in joined, "日志里没有请求路径"
    assert "UpstreamFault" in joined, "日志里没有异常类名"


def test_client_error_domain_fault_stays_silent(caplog) -> None:
    """4xx 不写 error 日志：正常失败路径保持静默，否则真故障会被刷屏淹掉。"""
    import logging

    with caplog.at_level(logging.ERROR, logger="minimax_h3_prompt.api.problems"):
        resp = _test_client().get("/api/v1/_probe/client-error")

    assert resp.status_code == 409
    assert _problem_logs(caplog) == [], "4xx 不该产生 error 日志（#348）"


def test_not_found_stays_silent(caplog) -> None:
    """HTTPException 那一路的 4xx 同样静默。"""
    import logging

    with caplog.at_level(logging.ERROR, logger="minimax_h3_prompt.api.problems"):
        resp = _test_client().get("/api/v1/this-path-does-not-exist")

    assert resp.status_code == 404
    assert _problem_logs(caplog) == [], "404 不该产生 error 日志（#348）"
