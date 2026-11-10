"""D1-5（#210）：生产形态纵向切片——FastAPI 同源托管 + SPA fallback。

钉住路由优先级：/api/* 未知不被 SPA 吞掉、/openapi.json 与 /docs 不被 fallback 拦截、
已存在静态文件按原路径返回、其余 GET/HEAD 回退 index.html、dist 缺失返回 503 problem。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from workbench_client import workbench_client

_INDEX = "<!doctype html><html><body><div id='app'>SPA-SHELL</div></body></html>"


@pytest.fixture()
def dist(tmp_path: Path) -> Path:
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text(_INDEX, encoding="utf-8")
    (root / "assets" / "app.js").write_text("console.log('hi')", encoding="utf-8")
    return root


def _client(dist_dir: Path) -> TestClient:
    from minimax_h3_prompt.api.app import create_app

    return workbench_client(create_app(dist_dir=dist_dir))


def test_api_route_still_works(dist: Path) -> None:
    resp = _client(dist).get("/api/v1/health")
    assert resp.status_code == 200
    assert resp.json()["service"] == "minimax-h3-prompt"


def test_unknown_api_path_is_problem_not_spa(dist: Path) -> None:
    resp = _client(dist).get("/api/v1/nope")
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert "SPA-SHELL" not in resp.text
    assert resp.json()["instance"] == "/api/v1/nope"


def test_openapi_and_docs_not_swallowed_by_fallback(dist: Path) -> None:
    client = _client(dist)
    openapi = client.get("/openapi.json")
    assert openapi.status_code == 200
    assert "openapi" in openapi.json()

    docs = client.get("/docs")
    assert docs.status_code == 200
    assert "swagger" in docs.text.lower()
    assert "SPA-SHELL" not in docs.text


def test_existing_static_file_served_at_path(dist: Path) -> None:
    resp = _client(dist).get("/assets/app.js")
    assert resp.status_code == 200
    assert "console.log('hi')" in resp.text


def test_deep_link_falls_back_to_index_html(dist: Path) -> None:
    resp = _client(dist).get("/diagnostics")
    assert resp.status_code == 200
    assert "SPA-SHELL" in resp.text
    assert resp.headers["content-type"].startswith("text/html")


def test_missing_dist_returns_503_problem(tmp_path: Path) -> None:
    resp = _client(tmp_path / "does-not-exist").get("/")
    assert resp.status_code == 503
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert "pnpm build" in resp.json()["detail"]


def test_no_cors_header(dist: Path) -> None:
    resp = _client(dist).get("/")
    assert "access-control-allow-origin" not in {k.lower() for k in resp.headers}
