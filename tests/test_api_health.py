"""D1-1（#206）：健康接口纵向切片；D5-3（#238）追加配置与密钥落位字段。

钉住三件事：成功响应直接返回资源（无 data 套壳）、版本来自包元数据、响应带
``Cache-Control: no-store`` 且不出现 CORS 头。version 读不到时必须在构造 app
（即启动）阶段失败，而不是返回假版本号。

#238 追加的四个字段（``data_root`` / ``config_path`` / ``env_path`` / ``missing_keys``）
是用户 2026-11-12 明确裁定的**加法扩展**：spec §13「其余端点不变」在这里让位于
「概览页要能告诉用户去哪个文件配密钥」。它们只读、**只有键名没有值**。

#237 再加 ``base_url``（spec #235 §5.2 / D5-AC2）：值只从 ``app.state.base_url`` 读，
入口写它是实际端口的单一来源；直接 ``create_app()`` 的测试没有这个属性，回落成空串
（= 未知），**不是 500、也不是自己拼的端口**。
"""
from __future__ import annotations

import importlib.metadata

import pytest
from fastapi.testclient import TestClient
from workbench_client import workbench_client


def _client() -> TestClient:
    from minimax_h3_prompt.api.app import create_app

    return workbench_client(create_app())


def _strip_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """摘掉环境里的密钥变量（含 anthropic 的回退名），造出「没配密钥」的机器。"""
    from minimax_h3_prompt.config import config

    covered = {
        config.primary_model.api_key_env,
        config.vision_model.api_key_env,
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
    }
    for name in covered:
        monkeypatch.delenv(name, raising=False)


def test_health_returns_resource_with_no_store_header() -> None:
    resp = _client().get("/api/v1/health")
    body = resp.json()

    assert resp.status_code == 200
    assert body["status"] == "ok"
    assert body["service"] == "minimax-h3-prompt"
    assert body["version"] == importlib.metadata.version("minimax-h3-prompt")
    # 字段集合是闭合的：多一个少一个都要在这里看见（D1 三件 + D5-3 四件 + #237 的
    # base_url）。
    assert set(body) == {
        "status", "service", "version",
        "data_root", "config_path", "env_path", "missing_keys",
        "base_url",
    }
    assert isinstance(body["missing_keys"], list)
    assert resp.headers["cache-control"] == "no-store"
    header_names = {name.lower() for name in resp.headers}
    assert "access-control-allow-origin" not in header_names


def test_health_reports_config_locations_from_the_paths_module() -> None:
    """三处路径必须等于 ``paths`` 的产出：界面上的「去这里改」不能各算一份。"""
    from minimax_h3_prompt import paths

    body = _client().get("/api/v1/health").json()

    assert body["data_root"] == str(paths.data_root())
    assert body["config_path"] == str(paths.config_path())
    assert body["env_path"] == str(paths.env_file_path())


def test_health_is_200_and_names_missing_keys_when_nothing_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缺密钥时系统不崩：健康接口照常 200，``missing_keys`` 列出变量名。"""
    from minimax_h3_prompt.config import config

    _strip_keys(monkeypatch)

    resp = _client().get("/api/v1/health")

    assert resp.status_code == 200
    body = resp.json()
    assert config.primary_model.api_key_env in body["missing_keys"]
    assert config.vision_model.api_key_env in body["missing_keys"]


def test_health_never_carries_key_values(monkeypatch: pytest.MonkeyPatch) -> None:
    """配好密钥后 ``missing_keys`` 清空，且响应体里不出现任何一个密钥值（脱敏）。"""
    from minimax_h3_prompt.config import config

    secret = "sk-test-DO-NOT-LEAK-238"
    monkeypatch.setenv(config.primary_model.api_key_env, secret)
    monkeypatch.setenv(config.vision_model.api_key_env, secret)

    resp = _client().get("/api/v1/health")

    assert secret not in resp.text
    assert resp.json()["missing_keys"] == []


def test_health_reports_the_actual_base_url_from_app_state() -> None:
    """启动入口落下的实际地址原样返回（端口不在这里二次拼接）。"""
    from minimax_h3_prompt.api.app import create_app

    app = create_app()
    app.state.base_url = "http://127.0.0.1:51234/"

    body = workbench_client(app).get("/api/v1/health").json()

    assert body["base_url"] == "http://127.0.0.1:51234/"


def test_health_base_url_is_empty_when_app_was_not_started_by_the_entrypoint() -> None:
    """直接 ``create_app()`` 的 app 没有这个属性：回落空串（= 未知），不是 500。"""
    resp = _client().get("/api/v1/health")

    assert resp.status_code == 200
    assert resp.json()["base_url"] == ""


def test_health_version_read_failure_fails_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    from minimax_h3_prompt.api import app as app_module

    def _missing(_name: str) -> str:
        raise importlib.metadata.PackageNotFoundError("minimax-h3-prompt")

    monkeypatch.setattr(app_module.importlib.metadata, "version", _missing)

    with pytest.raises(RuntimeError, match="版本元数据"):
        app_module.create_app()
