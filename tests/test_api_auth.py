"""D3-1（#223）：本地鉴权纵向切片——Host / Origin / 启动令牌 / 会话 Cookie。

只从 HTTP 面观察（``TestClient``），不碰中间件内部结构：拒绝、放行、错误码、
豁免面与 Cookie 属性都是契约（#222 §4、§10）。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from minimax_h3_prompt.api.app import create_app


def _app(tmp_path: Path | None = None):
    return create_app(sessions_root=tmp_path) if tmp_path is not None else create_app()


def _client(app, base_url: str = "http://127.0.0.1") -> TestClient:
    return TestClient(app, base_url=base_url)


# ---------------------------------------------------------------------------
# Host 校验（D3-AC1）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "base_url",
    ["http://127.0.0.1", "http://localhost:5173", "http://[::1]:8000"],
)
def test_loopback_hosts_are_allowed_without_port_restriction(base_url: str) -> None:
    """端口不限是硬要求：Vite 代理不换 Host，浏览器过来的是 ``localhost:5173``。"""
    resp = _client(_app(), base_url=base_url).get("/api/v1/health")

    assert resp.status_code == 200


def test_non_loopback_host_is_forbidden(tmp_path: Path) -> None:
    resp = _client(_app(tmp_path)).get(
        "/api/v1/sessions/不存在/GEN999", headers={"Host": "evil.example.com"})

    assert resp.status_code == 403
    assert resp.headers["content-type"].startswith("application/problem+json")
    body = resp.json()
    assert body["code"] == "host_forbidden"
    assert body["status"] == 403


def test_host_forbidden_body_does_not_echo_the_host(tmp_path: Path) -> None:
    """错误体不回显 Host/Origin 原值（#222 §12）：排障值属于日志，不属于响应。"""
    resp = _client(_app(tmp_path)).get(
        "/api/v1/health", headers={"Host": "leaky.example.com"})

    assert resp.status_code == 403
    assert "leaky.example.com" not in resp.text


# ---------------------------------------------------------------------------
# Origin 校验（D3-AC1）
# ---------------------------------------------------------------------------

def test_loopback_origin_is_allowed() -> None:
    resp = _client(_app()).get(
        "/api/v1/health", headers={"Origin": "http://localhost:5173"})

    assert resp.status_code == 200


def test_missing_origin_is_allowed(tmp_path: Path) -> None:
    """curl / 本机脚本 / 同源 GET 常常不带 Origin：放行，但令牌照要。"""
    app = _app(tmp_path)
    client = _client(app)
    client.headers["X-Workbench-Token"] = app.state.local_access.token

    assert client.get("/api/v1/sessions").status_code == 200


def test_cross_site_origin_is_forbidden(tmp_path: Path) -> None:
    resp = _client(_app(tmp_path)).get(
        "/api/v1/health", headers={"Origin": "https://evil.example.com"})

    assert resp.status_code == 403
    body = resp.json()
    assert body["code"] == "origin_forbidden"
    assert "evil.example.com" not in resp.text


def test_origin_null_is_forbidden() -> None:
    """沙箱页面 / ``file://`` 的 Origin 是字面量 ``null``，不能当「没有 Origin」。"""
    resp = _client(_app()).get("/api/v1/health", headers={"Origin": "null"})

    assert resp.status_code == 403
    assert resp.json()["code"] == "origin_forbidden"


# ---------------------------------------------------------------------------
# 启动令牌与凭据（D3-AC2 / AC3）
# ---------------------------------------------------------------------------

def test_missing_credential_is_401_auth_required(tmp_path: Path) -> None:
    resp = _client(_app(tmp_path)).get("/api/v1/sessions")

    assert resp.status_code == 401
    assert resp.headers["content-type"].startswith("application/problem+json")
    body = resp.json()
    assert body["code"] == "auth_required"
    assert body["instance"] == "/api/v1/sessions"


def test_unknown_api_path_outside_exempt_surface_still_needs_credential(
    tmp_path: Path,
) -> None:
    """豁免面是闭合的三处：未知 /api/* 未持令牌先 401，别让它泄露路由存在性。"""
    resp = _client(_app(tmp_path)).get("/api/v1/definitely-not-a-route")

    assert resp.status_code == 401
    assert resp.json()["code"] == "auth_required"


def test_header_credential_passes(tmp_path: Path) -> None:
    app = _app(tmp_path)
    client = _client(app)
    client.headers["X-Workbench-Token"] = app.state.local_access.token

    assert client.get("/api/v1/sessions").status_code == 200


def test_wrong_header_credential_is_rejected(tmp_path: Path) -> None:
    client = _client(_app(tmp_path))
    client.headers["X-Workbench-Token"] = "not-the-token"

    assert client.get("/api/v1/sessions").status_code == 401


def test_cookie_credential_passes(tmp_path: Path) -> None:
    """``<video>`` / ``<img>`` 挂不上自定义头：Cookie 必须单独就够用。"""
    app = _app(tmp_path)
    client = _client(app)
    client.cookies.set("wb_session", app.state.local_access.token)

    assert client.get("/api/v1/sessions").status_code == 200


def test_cookie_credential_works_even_with_a_wrong_header(tmp_path: Path) -> None:
    """二选一：请求头错了但 Cookie 对，仍算持凭据。"""
    app = _app(tmp_path)
    client = _client(app)
    client.headers["X-Workbench-Token"] = "stale-token-after-restart"
    client.cookies.set("wb_session", app.state.local_access.token)

    assert client.get("/api/v1/sessions").status_code == 200


def test_wrong_cookie_is_rejected(tmp_path: Path) -> None:
    client = _client(_app(tmp_path))
    client.cookies.set("wb_session", "not-the-token")

    assert client.get("/api/v1/sessions").status_code == 401


def test_origin_denial_wins_over_a_valid_credential(tmp_path: Path) -> None:
    app = _app(tmp_path)
    client = _client(app)
    client.headers["X-Workbench-Token"] = app.state.local_access.token

    resp = client.get("/api/v1/sessions", headers={"Origin": "https://evil.example.com"})

    assert resp.status_code == 403
    assert resp.json()["code"] == "origin_forbidden"


def test_host_denial_wins_over_a_valid_credential(tmp_path: Path) -> None:
    app = _app(tmp_path)
    client = _client(app)
    client.headers["X-Workbench-Token"] = app.state.local_access.token

    resp = client.get("/api/v1/sessions", headers={"Host": "evil.example.com"})

    assert resp.status_code == 403
    assert resp.json()["code"] == "host_forbidden"


def test_token_is_per_process_and_rejects_the_other_process_s_token(
    tmp_path: Path,
) -> None:
    first, second = _app(tmp_path), _app(tmp_path)
    assert first.state.local_access.token != second.state.local_access.token

    client = _client(second)
    client.headers["X-Workbench-Token"] = first.state.local_access.token

    assert client.get("/api/v1/sessions").status_code == 401


def test_401_body_never_echoes_the_token(tmp_path: Path) -> None:
    app = _app(tmp_path)
    client = _client(app)
    token = app.state.local_access.token
    client.headers["X-Workbench-Token"] = f"{token}-wrong"

    resp = client.get("/api/v1/sessions")

    assert resp.status_code == 401
    assert token not in resp.text


# ---------------------------------------------------------------------------
# 豁免面与引导端点（D3-AC2 / AC3）
# ---------------------------------------------------------------------------

def test_token_endpoint_is_exempt_and_issues_a_strict_cookie() -> None:
    app = _app()
    resp = _client(app).get("/api/v1/auth/token")

    assert resp.status_code == 200
    token = resp.json()["token"]
    assert token == app.state.local_access.token
    assert resp.headers["cache-control"] == "no-store"

    set_cookie = resp.headers["set-cookie"].lower()
    assert f"wb_session={token}".lower() in set_cookie
    assert "httponly" in set_cookie
    assert "samesite=strict" in set_cookie
    assert "path=/api/v1" in set_cookie
    # 会话 Cookie：不带 Max-Age/Expires，进程结束即失效。
    assert "max-age" not in set_cookie
    assert "expires" not in set_cookie


def test_health_endpoint_is_exempt() -> None:
    assert _client(_app()).get("/api/v1/health").status_code == 200


def test_static_and_spa_fallback_are_exempt(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>SPA-SHELL</html>", encoding="utf-8")
    (dist / "asset.js").write_text("console.log(1)", encoding="utf-8")

    client = _client(create_app(sessions_root=tmp_path, dist_dir=dist))

    assert client.get("/").status_code == 200
    assert client.get("/asset.js").status_code == 200
    assert client.get("/diagnostics").status_code == 200
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/docs").status_code == 200


def test_auth_token_cookie_from_one_app_works_on_that_app(tmp_path: Path) -> None:
    """浏览器路径的端到端形状：取令牌 → 浏览器存 Cookie → 之后只凭 Cookie 通过。"""
    app = _app(tmp_path)
    client = _client(app)

    client.get("/api/v1/auth/token")
    client.headers.pop("X-Workbench-Token", None)

    assert client.get("/api/v1/sessions").status_code == 200
