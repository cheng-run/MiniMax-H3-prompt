"""诊断包端点（#239 / spec #235 §8.2 + §13 + §17 D5-AC10/AC11）。

判据是**解压后的 zip 内容**，不是 builder 的返回值：清单完整性、脱敏、排除面都要在
包本身这一层成立——将来换实现（换压缩参数、改组装顺序）这些断言不该跟着动。
"""
from __future__ import annotations

import io
import json
import os
import zipfile
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from minimax_h3_prompt import diagnostics, logging_setup, paths
from minimax_h3_prompt.api.__main__ import StartupInfo
from minimax_h3_prompt.api.app import create_app
from workbench_client import workbench_client

FAKE_KEY = "sk-fake-diagnostic-key-0123456789"
BUNDLE_PATH = "/api/v1/diagnostics/bundle"


def _brief_body() -> dict:
    return {
        "mode": "base",
        "variant": "T2VA",
        "duration": 4.0,
        "style": "写实",
        "language": "Chinese",
        "plot": "雨夜旧信",
    }


@pytest.fixture
def bundle_env(tmp_path, monkeypatch):
    """一个带临时会话根与临时日志目录的 app（H3_LOG_DIR 指到 tmp_path）。"""
    root = tmp_path / "sessions"
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setenv(logging_setup.LOG_DIR_ENV, str(log_dir))
    app = create_app(sessions_root=root)
    return app, root, log_dir, workbench_client(app)


def _names(payload: bytes) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        return archive.namelist()


def _read_all(payload: bytes) -> dict[str, str]:
    result: dict[str, str] = {}
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        for name in archive.namelist():
            result[name] = archive.read(name).decode("utf-8", errors="replace")
    return result


def _make_session(client: TestClient) -> str:
    response = client.post("/api/v1/sessions", json=_brief_body())
    assert response.status_code == 201, response.text
    return response.json()["session_id"]


def test_bundle_is_a_zip_attachment_with_a_dated_filename(bundle_env):
    _app, _root, _log_dir, client = bundle_env
    response = client.get(BUNDLE_PATH)

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert "filename*=UTF-8''minimax-h3-diagnostics-" in disposition
    assert response.headers["cache-control"] == "no-store"
    assert _names(response.content), "空 zip 不是诊断包"


def test_bundle_requires_the_local_token(bundle_env):
    """诊断包不在豁免面上：没有令牌必须 401（spec §13 没给它开口子）。"""
    app, _root, _log_dir, _client = bundle_env
    anonymous = TestClient(app, base_url="http://127.0.0.1")

    response = anonymous.get(BUNDLE_PATH)

    assert response.status_code == 401
    assert response.json()["code"] == "auth_required"


def test_bundle_contains_every_entry_from_the_spec_table(bundle_env):
    """§8.2 表：manifest / environment / config / logs / sessions / artifacts。"""
    app, root, log_dir, client = bundle_env
    session_id = _make_session(client)
    app.state.session_store.write_text_artifact(
        session_id, "video-prompt.md", "# 视频提示词\n雨夜旧信。")
    (log_dir / f"app-{date.today():%Y%m%d}.log").write_text(
        "2026-11-12 10:00:00 INFO 启动完成\n", encoding="utf-8")

    payload = client.get(BUNDLE_PATH).content
    names = set(_names(payload))

    assert {"manifest.json", "environment.txt", "config.json"} <= names
    assert f"logs/app-{date.today():%Y%m%d}.log" in names
    assert f"sessions/{session_id}/session-state.json" in names
    assert f"sessions/{session_id}/events.jsonl" in names
    assert f"artifacts/{session_id}/video-prompt.md" in names
    assert "# 视频提示词" in _read_all(payload)[f"artifacts/{session_id}/video-prompt.md"]


def test_empty_store_still_has_the_three_core_files(bundle_env):
    """会话为空时允许没有 sessions/*，但三份核心文件一件都不能少。"""
    _app, _root, _log_dir, client = bundle_env
    names = set(_names(client.get(BUNDLE_PATH).content))

    assert {"manifest.json", "environment.txt", "config.json"} <= names
    assert not any(name.startswith("sessions/") for name in names)


def test_bundle_never_contains_secret_values(bundle_env, monkeypatch):
    """票面硬条：假密钥出现在日志与会话产物里，导出后包内任何文件都不许有它。"""
    monkeypatch.setenv("MINIMAX_API_KEY", FAKE_KEY)
    app, _root, log_dir, client = bundle_env
    session_id = _make_session(client)
    app.state.session_store.write_text_artifact(
        session_id, "script.md", f"# 剧本\n（泄漏的 key：{FAKE_KEY}）")
    (log_dir / f"app-{date.today():%Y%m%d}.log").write_text(
        f"2026-11-12 ERROR 上游 401 key={FAKE_KEY}\n", encoding="utf-8")

    payload = client.get(BUNDLE_PATH).content

    for name, text in _read_all(payload).items():
        assert FAKE_KEY not in text, f"密钥值出现在 {name} 里"
    assert logging_setup.REDACTION_PLACEHOLDER in _read_all(payload)[
        f"logs/app-{date.today():%Y%m%d}.log"]


def test_bundle_excludes_env_file_and_media(bundle_env, monkeypatch):
    """不含 ``%APPDATA%`` 的 .env、不含素材图片/视频（spec §8.2 的排除面）。"""
    app, root, _log_dir, client = bundle_env
    session_id = _make_session(client)
    session_dir = root / Path(session_id)
    assets = session_dir / "assets"
    assets.mkdir()
    (assets / "0123456789abcdef.png").write_bytes(b"\x89PNG\r\n\x1a\nnot-a-real-png")
    (session_dir / "final.mp4").write_bytes(b"\x00\x00\x00\x18ftypisom")

    payload = client.get(BUNDLE_PATH).content
    names = _names(payload)
    texts = _read_all(payload)

    assert not any(name.endswith(".env") for name in names)
    assert not any("assets/" in name or name.endswith((".png", ".mp4")) for name in names)
    appdata_env = str(Path(os.environ.get("APPDATA", "C:/Users/example"),
                           "MiniMax-H3-prompt", ".env"))
    for name, text in texts.items():
        assert appdata_env not in text, f"{name} 里出现了 %APPDATA% 的 .env 路径"
        assert ".env" not in text, f"{name} 里出现了 .env 字样"


def test_manifest_reports_runtime_facts_from_startup_info(bundle_env):
    """端口与形态从 ``app.state.startup`` 读；拿不到时不抛 500（用形态兜底）。"""
    app, _root, _log_dir, client = bundle_env
    app.state.startup = StartupInfo(
        host="127.0.0.1",
        port=54321,
        base_url="http://127.0.0.1:54321/",
        data_root=Path("D:/data"),
        frozen=False,
        version=app.state.version,
    )

    manifest = json.loads(_read_all(client.get(BUNDLE_PATH).content)["manifest.json"])

    assert manifest["port"] == 54321
    assert manifest["form"] == "开发"
    assert manifest["data_root"] == str(Path("D:/data"))
    assert manifest["version"] == app.state.version
    assert manifest["generated_at"]
    assert manifest["log_window"]["retention_days"] == logging_setup.DEFAULT_RETENTION_DAYS
    assert manifest["log_window"]["to"] == date.today().isoformat()


def test_manifest_falls_back_when_startup_info_is_missing(bundle_env):
    """测试 app 没有 ``state.startup``（只有启动入口才挂）：仍要 200，形态按 paths 判。"""
    _app, _root, _log_dir, client = bundle_env
    manifest = json.loads(_read_all(client.get(BUNDLE_PATH).content)["manifest.json"])

    assert manifest["port"] is None
    assert manifest["form"] == ("冻结" if paths.is_frozen() else "开发")


def test_environment_text_reports_os_python_arch_and_webview2(bundle_env):
    _app, _root, _log_dir, client = bundle_env
    text = _read_all(client.get(BUNDLE_PATH).content)["environment.txt"]

    assert "OS:" in text
    assert "Python:" in text
    assert "架构:" in text
    assert "WebView2:" in text


def test_build_bundle_ignores_a_store_without_a_sessions_root(tmp_path):
    """存储对象拿不到 ``root`` 时不许退化成「扫当前目录」——那会把无关文件收进包。"""

    class RootlessStore:
        pass

    payload, _name = diagnostics.build_bundle(
        RootlessStore(), version="0.1.0", log_dir=tmp_path, config_raw={})

    assert set(_names(payload)) == {"manifest.json", "environment.txt", "config.json"}


def test_config_snapshot_redacts_secret_shaped_keys_and_values():
    """配置快照只留键名与非敏感值；密钥类键与值是 ``<redacted>``。"""
    snapshot = diagnostics.sanitize_config(
        {
            "models": {"primary": {"model": "m1", "api_key": "sk-abc",
                                   "api_key_env": "MINIMAX_API_KEY"}},
            "mystery": "sk-abc",
        },
        secrets=("sk-abc",),
    )

    assert snapshot["models"]["primary"]["model"] == "m1"
    assert snapshot["models"]["primary"]["api_key"] == logging_setup.REDACTION_PLACEHOLDER
    # api_key_env 只是**键名**，不是密钥值：留着，排障时要看它。
    assert snapshot["models"]["primary"]["api_key_env"] == "MINIMAX_API_KEY"
    assert snapshot["mystery"] == logging_setup.REDACTION_PLACEHOLDER
