"""D1-1（#206）：健康接口纵向切片。

钉住三件事：成功响应直接返回资源（无 data 套壳）、版本来自包元数据、响应带
``Cache-Control: no-store`` 且不出现 CORS 头。version 读不到时必须在构造 app
（即启动）阶段失败，而不是返回假版本号。
"""
from __future__ import annotations

import importlib.metadata

import pytest
from fastapi.testclient import TestClient

# Starlette 1.10 的 TestClient 会在本机回环开一个真实 socket（127.0.0.1 随机端口），
# 而 conftest 的出网守卫连回环也一并拦。本文件全部请求只打到进程内 app 的回环服务，
# 不触达任何外部网络，因此整文件放行（这是守卫文档里写明的 escape hatch）。
pytestmark = pytest.mark.allow_network


def _client() -> TestClient:
    from minimax_h3_prompt.api.app import create_app

    return TestClient(create_app())


def test_health_returns_resource_with_no_store_header() -> None:
    resp = _client().get("/api/v1/health")

    assert resp.status_code == 200
    assert resp.json() == {
        "status": "ok",
        "service": "minimax-h3-prompt",
        "version": importlib.metadata.version("minimax-h3-prompt"),
    }
    assert resp.headers["cache-control"] == "no-store"
    header_names = {name.lower() for name in resp.headers}
    assert "access-control-allow-origin" not in header_names


def test_health_version_read_failure_fails_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    from minimax_h3_prompt.api import app as app_module

    def _missing(_name: str) -> str:
        raise importlib.metadata.PackageNotFoundError("minimax-h3-prompt")

    monkeypatch.setattr(app_module.importlib.metadata, "version", _missing)

    with pytest.raises(RuntimeError, match="版本元数据"):
        app_module.create_app()
