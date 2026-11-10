"""D3-2（#224）：素材上传——流式落盘、字节嗅探、内容哈希标识（#222 §5）。

接缝只有两个：**HTTP 面**（上传、列表、详情里的 ``assets``）与 ``store_asset`` 的
**内存边界**（``tracemalloc`` 峰值证明 64 MiB 内容不整体读入——这条从 HTTP 面看不见，
而它正是「流式」这三个字唯一能被机器验证的地方）。

判据尽量来自**独立来源**：哈希标识在用例里自己算一遍 sha256，落盘文件名与磁盘逐字
对照，不让接口自己证明自己。
"""
from __future__ import annotations

import hashlib
import json
import tracemalloc
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from minimax_h3_prompt import workbench_store
from minimax_h3_prompt.api.app import create_app
from minimax_h3_prompt.session_store import LEGACY_SCHEMA_VERSION, SESSION_FILENAME
from minimax_h3_prompt.workbench_store import EVENT_TYPES, EVENTS_FILENAME
from workbench_client import workbench_client

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
MP4_MAGIC = b"\x00\x00\x00\x18ftypmp42"
IMAGE_LIMIT = 20 * 1024 * 1024  # spec §5.2 的字面值：与实现常量分开写才咬得住


def _png(size: int = 4096) -> bytes:
    """魔数正确、内容不解码的“图片”：只需要过嗅探（#222 §5.2 只嗅探不解码）。"""
    return PNG_MAGIC + b"\x00" * (size - len(PNG_MAGIC))


def _mp4(size: int = 4096) -> bytes:
    return MP4_MAGIC + b"\x00" * (size - len(MP4_MAGIC))


def _client(tmp_path: Path) -> TestClient:
    return workbench_client(create_app(sessions_root=tmp_path))


def _new_session(client: TestClient) -> str:
    body = {
        "mode": "base", "variant": "T2VA", "duration": 4.0,
        "style": "写实", "language": "Chinese", "plot": "雨夜旧信",
    }
    session = client.post("/api/v1/sessions", json=body).json()
    return session["session_id"]


def _assets_dir(root: Path, session_id: str) -> Path:
    return root / Path(session_id) / "assets"


def _assert_problem(resp, status: int, code: str) -> dict:
    assert resp.status_code == status, resp.text
    assert resp.headers["content-type"].startswith("application/problem+json")
    body = resp.json()
    assert body["status"] == status
    assert body["code"] == code
    return body


def _write_legacy(root: Path, session_id: str = "旧主题/GEN001") -> Path:
    directory = root / Path(session_id)
    directory.mkdir(parents=True)
    (directory / SESSION_FILENAME).write_text(json.dumps({
        "schema_version": LEGACY_SCHEMA_VERSION,
        "status": "completed",
        "brief": {"mode": "base", "variant": "T2VA", "duration": 4.0,
                  "plot": "旧主题"},
        "stage_state": {"script": "旧剧本"},
        "created_at": "2026-10-01T00:00:00+00:00",
        "updated_at": "2026-10-01T00:10:00+00:00",
    }, ensure_ascii=False), encoding="utf-8")
    return directory


# ---------------------------------------------------------------------------
# 上传成功与元数据（D3-AC5：标识是内容哈希，与产物标识同形）
# ---------------------------------------------------------------------------

def test_upload_returns_metadata_and_lands_on_the_content_hash(tmp_path):
    payload = _png()
    digest = hashlib.sha256(payload).hexdigest()
    with workbench_client(create_app(sessions_root=tmp_path)) as client:
        session_id = _new_session(client)
        resp = client.post(
            f"/api/v1/sessions/{session_id}/assets",
            files={"file": ("雨夜.png", payload, "image/png")})

        assert resp.status_code == 201, resp.text
        asset = resp.json()
        listed = client.get(f"/api/v1/sessions/{session_id}/assets").json()
        detail = client.get(f"/api/v1/sessions/{session_id}").json()

    assert asset["asset_id"] == f"{session_id}/{digest[:16]}"
    assert asset["kind"] == "image"
    assert asset["name"] == "雨夜.png"
    assert asset["bytes"] == len(payload)
    assert asset["created_at"]
    # 落盘名由内容决定，与用户文件名无关（#222 §5.2）。
    on_disk = _assets_dir(tmp_path, session_id) / f"{digest[:16]}.png"
    assert on_disk.read_bytes() == payload
    assert listed == [asset]
    assert detail["assets"] == [asset]


def test_same_content_twice_yields_one_file_and_the_same_id(tmp_path):
    """D3-AC5：同内容重复上传不产生第二份文件。"""
    payload = _png()
    with workbench_client(create_app(sessions_root=tmp_path)) as client:
        session_id = _new_session(client)
        first = client.post(f"/api/v1/sessions/{session_id}/assets",
                            files={"file": ("a.png", payload, "image/png")})
        second = client.post(f"/api/v1/sessions/{session_id}/assets",
                             files={"file": ("b.png", payload, "image/png")})
        listed = client.get(f"/api/v1/sessions/{session_id}/assets").json()

    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["asset_id"] == second.json()["asset_id"]
    # 首次的显示名与创建时间保持不变：去重不是「再写一遍然后覆盖」。
    assert second.json()["name"] == "a.png"
    assert second.json()["created_at"] == first.json()["created_at"]
    assert len(listed) == 1
    files = [path for path in _assets_dir(tmp_path, session_id).iterdir()
             if path.suffix != ".json"]
    assert len(files) == 1


def test_extension_comes_from_the_bytes_not_the_filename(tmp_path):
    """扩展名由嗅探结果决定：文件名没有扩展名也照样落成 ``.png``。"""
    payload = _png()
    digest = hashlib.sha256(payload).hexdigest()
    with workbench_client(create_app(sessions_root=tmp_path)) as client:
        session_id = _new_session(client)
        resp = client.post(f"/api/v1/sessions/{session_id}/assets",
                           files={"file": ("没有扩展名", payload, "application/octet-stream")})

    assert resp.status_code == 201, resp.text
    assert resp.json()["name"] == "没有扩展名"
    assert (_assets_dir(tmp_path, session_id) / f"{digest[:16]}.png").is_file()


def test_video_upload_is_accepted(tmp_path):
    with workbench_client(create_app(sessions_root=tmp_path)) as client:
        session_id = _new_session(client)
        resp = client.post(f"/api/v1/sessions/{session_id}/assets",
                           files={"file": ("clip.mp4", _mp4(), "video/mp4")})

    assert resp.status_code == 201, resp.text
    assert resp.json()["kind"] == "video"


# 真实的 WebM/Matroska 前 32 字节：EBML 头 + DocType 元素（0x4282）。
_EBML_HEAD = (b"\x1a\x45\xdf\xa3\x9f\x42\x86\x81\x01\x42\xf7\x81\x01\x42\xf2\x81\x04"
              b"\x42\xf3\x81\x08\x42\x82\x84")


def _webm(size: int = 4096) -> bytes:
    head = _EBML_HEAD + b"webm"
    return head + b"\x00" * (size - len(head))


def _mkv(size: int = 4096) -> bytes:
    head = _EBML_HEAD + b"matroska"
    return head + b"\x00" * (size - len(head))


def _wav(size: int = 4096) -> bytes:
    head = b"RIFF\x00\x00\x00\x00WAVEfmt "
    return head + b"\x00" * (size - len(head))


def _mp3(size: int = 4096) -> bytes:
    head = b"ID3\x04\x00\x00\x00\x00\x00\x00"
    return head + b"\x00" * (size - len(head))


@pytest.mark.parametrize(("payload", "filename", "kind", "ext"), [
    (_wav(), "参考.wav", "audio", "wav"),
    (_mp3(), "参考.mp3", "audio", "mp3"),
    (_webm(), "参考.webm", "video", "webm"),
])
def test_audio_and_webm_are_accepted(tmp_path, payload, filename, kind, ext):
    """§5.2 的白名单三类都要收：audio 是 WAV/MP3（64 MiB），WebM 要真的 DocType。"""
    digest = hashlib.sha256(payload).hexdigest()
    with workbench_client(create_app(sessions_root=tmp_path)) as client:
        session_id = _new_session(client)
        resp = client.post(f"/api/v1/sessions/{session_id}/assets",
                           files={"file": (filename, payload, "application/octet-stream")})

    assert resp.status_code == 201, resp.text
    assert resp.json()["kind"] == kind
    assert (_assets_dir(tmp_path, session_id) / f"{digest[:16]}.{ext}").is_file()


@pytest.mark.parametrize(("payload", "filename"), [
    (_mkv(), "视频.mkv"),      # EBML 但 DocType 是 matroska，不是 WebM
    (_mp4(size=32).replace(b"mp42", b"qt  ", 1), "视频.mp4"),  # QuickTime 的 brand
    (b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00", "图片.heic"),  # HEIC：同盒子、是图片
])
def test_containers_outside_the_whitelist_are_415(tmp_path, payload, filename):
    """容器头相同不等于格式受支持：EBML 要 DocType 是 webm，``ftyp`` 要 brand 在名单里。"""
    with workbench_client(create_app(sessions_root=tmp_path)) as client:
        session_id = _new_session(client)
        resp = client.post(f"/api/v1/sessions/{session_id}/assets",
                           files={"file": (filename, payload, "video/mp4")})

    _assert_problem(resp, 415, "unsupported_media_type")


# ---------------------------------------------------------------------------
# 类型判定（D3-AC6）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("filename", ["伪装.png", "伪装.jpg", "a.webm"])
def test_bytes_outside_the_whitelist_are_415(tmp_path, filename):
    with workbench_client(create_app(sessions_root=tmp_path)) as client:
        session_id = _new_session(client)
        resp = client.post(
            f"/api/v1/sessions/{session_id}/assets",
            files={"file": (filename, b"#!/bin/sh\necho not-media\n", "image/png")})
        listed = client.get(f"/api/v1/sessions/{session_id}/assets").json()

    _assert_problem(resp, 415, "unsupported_media_type")
    assert listed == []
    assert list(_assets_dir(tmp_path, session_id).iterdir()) == []


def test_png_bytes_named_jpg_are_415(tmp_path):
    """#222 §14 逐字要求的那条：声明的扩展名与字节矛盾时不替它挑一个。"""
    with workbench_client(create_app(sessions_root=tmp_path)) as client:
        session_id = _new_session(client)
        resp = client.post(f"/api/v1/sessions/{session_id}/assets",
                           files={"file": ("其实是png.jpg", _png(), "image/jpeg")})

    _assert_problem(resp, 415, "unsupported_media_type")


def test_empty_upload_is_415(tmp_path):
    with workbench_client(create_app(sessions_root=tmp_path)) as client:
        session_id = _new_session(client)
        resp = client.post(f"/api/v1/sessions/{session_id}/assets",
                           files={"file": ("空.png", b"", "image/png")})

    _assert_problem(resp, 415, "unsupported_media_type")


# ---------------------------------------------------------------------------
# 边写边判超限（D3-AC4）
# ---------------------------------------------------------------------------

def test_oversized_upload_is_413_and_leaves_no_residue(tmp_path):
    """上限按字面取 20 MiB：超一个字节就中止，盘上不留正本、也不留残片。"""
    payload = _png(IMAGE_LIMIT + 1)
    with workbench_client(create_app(sessions_root=tmp_path)) as client:
        session_id = _new_session(client)
        resp = client.post(f"/api/v1/sessions/{session_id}/assets",
                           files={"file": ("大图.png", payload, "image/png")})
        listed = client.get(f"/api/v1/sessions/{session_id}/assets").json()

    _assert_problem(resp, 413, "asset_too_large")
    assert listed == []
    assets = _assets_dir(tmp_path, session_id)
    assert list(assets.iterdir()) == []


def test_upload_does_not_read_the_whole_body_into_memory(tmp_path):
    """64 MiB 的上传，Python 侧峰值必须远小于内容本身（托盘落临时文件，不进内存）。"""
    total = 64 * 1024 * 1024
    with workbench_client(create_app(sessions_root=tmp_path)) as client:
        session_id = _new_session(client)
    store = client.app.state.session_store  # type: ignore[attr-defined]

    tracemalloc.start()
    try:
        asset, replayed = store.store_asset(
            session_id, _GeneratedStream(total, MP4_MAGIC), filename="clip.mp4")
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert replayed is False
    assert asset.bytes == total
    assert peak < 8 * 1024 * 1024, f"峰值 {peak} 字节：内容被整体读入了"


class _GeneratedStream:
    """按块吐出伪 MP4 字节的只读流；模拟 Starlette 的 SpooledTemporaryFile。"""

    def __init__(self, total: int, head: bytes) -> None:
        self._left = total
        self._head = head

    def read(self, size: int = -1) -> bytes:
        if self._left <= 0:
            return b""
        size = self._left if size < 0 else min(size, self._left)
        chunk = (self._head + b"\x00" * size)[:size] if self._left else b""
        self._left -= size
        return chunk


# ---------------------------------------------------------------------------
# 幂等（D3-AC7）
# ---------------------------------------------------------------------------

def test_same_idempotency_key_replays_without_a_second_write(tmp_path):
    payload = _png()
    with workbench_client(create_app(sessions_root=tmp_path)) as client:
        session_id = _new_session(client)
        headers = {"Idempotency-Key": "upload-1"}
        first = client.post(f"/api/v1/sessions/{session_id}/assets",
                            files={"file": ("a.png", payload, "image/png")},
                            headers=headers)
        second = client.post(f"/api/v1/sessions/{session_id}/assets",
                             files={"file": ("a.png", payload, "image/png")},
                             headers=headers)
        listed = client.get(f"/api/v1/sessions/{session_id}/assets").json()

    assert first.status_code == 201 and "idempotency-replayed" not in first.headers
    assert second.status_code == 200
    assert second.headers["idempotency-replayed"] == "true"
    assert second.json()["asset_id"] == first.json()["asset_id"]
    assert len(listed) == 1


def test_same_key_with_different_content_is_a_conflict(tmp_path):
    with workbench_client(create_app(sessions_root=tmp_path)) as client:
        session_id = _new_session(client)
        headers = {"Idempotency-Key": "upload-2"}
        client.post(f"/api/v1/sessions/{session_id}/assets",
                    files={"file": ("a.png", _png(2048), "image/png")}, headers=headers)
        resp = client.post(f"/api/v1/sessions/{session_id}/assets",
                           files={"file": ("a.png", _png(2049), "image/png")},
                           headers=headers)

    _assert_problem(resp, 409, "idempotency_conflict")


# ---------------------------------------------------------------------------
# legacy 与越界（D3-AC8 / §8 的路径白名单）
# ---------------------------------------------------------------------------

def test_legacy_session_upload_is_409_and_not_404(tmp_path):
    _write_legacy(tmp_path)
    with workbench_client(create_app(sessions_root=tmp_path)) as client:
        resp = client.post("/api/v1/sessions/旧主题/GEN001/assets",
                           files={"file": ("a.png", _png(), "image/png")})

    _assert_problem(resp, 409, "session_legacy")


@pytest.mark.parametrize("session_id", ["../secret", "不存在/GEN999", "C:/x"])
def test_upload_to_an_unknown_or_escaping_session_is_404(tmp_path, session_id):
    outside = tmp_path.parent / "secret"
    outside.mkdir(exist_ok=True)
    with workbench_client(create_app(sessions_root=tmp_path)) as client:
        resp = client.post(
            f"/api/v1/sessions/{quote(session_id, safe='')}/assets",
            files={"file": ("a.png", _png(), "image/png")})
        listed = client.get(
            f"/api/v1/sessions/{quote(session_id, safe='')}/assets")

    _assert_problem(resp, 404, "session_not_found")
    _assert_problem(listed, 404, "session_not_found")
    assert not (outside / "assets").exists()


# ---------------------------------------------------------------------------
# 启动清理（D3-AC9）
# ---------------------------------------------------------------------------

def test_temp_files_are_invisible_then_cleaned_and_orphans_get_a_sidecar(tmp_path):
    orphan_hash = "a" * 16
    with workbench_client(create_app(sessions_root=tmp_path)) as client:
        session_id = _new_session(client)
        assets = _assets_dir(tmp_path, session_id)
        assets.mkdir(parents=True)
        (assets / ".tmp-deadbeef").write_bytes(b"partial")
        (assets / f"{orphan_hash}.png").write_bytes(_png(1024))

        # 残片不出现在列表里（命名规则就能保证，不靠清理）。
        listed = client.get(f"/api/v1/sessions/{session_id}/assets").json()
        assert [item["asset_id"] for item in listed] == [f"{session_id}/{orphan_hash}"]

    # 新进程启动：删残片、给孤儿正本补最小边车。
    with workbench_client(create_app(sessions_root=tmp_path)) as client:
        assert not (assets / ".tmp-deadbeef").exists()
        repaired = json.loads((assets / f"{orphan_hash}.json").read_text(encoding="utf-8"))
        listed = client.get(f"/api/v1/sessions/{session_id}/assets").json()

    assert repaired["kind"] == "image"
    assert repaired["bytes"] == 1024
    assert listed[0]["name"] == f"asset-{orphan_hash}.png"


# ---------------------------------------------------------------------------
# 与运行的关系（D3-AC13）：不改状态、不产生事件
# ---------------------------------------------------------------------------

def test_upload_changes_no_session_state_and_writes_no_event(tmp_path):
    with workbench_client(create_app(sessions_root=tmp_path)) as client:
        session_id = _new_session(client)
        directory = tmp_path / Path(session_id)
        before = client.get(f"/api/v1/sessions/{session_id}").json()
        events_before = (directory / EVENTS_FILENAME).read_text(encoding="utf-8")

        client.post(f"/api/v1/sessions/{session_id}/assets",
                    files={"file": ("a.png", _png(), "image/png")})

        after = client.get(f"/api/v1/sessions/{session_id}").json()
        events_after = (directory / EVENTS_FILENAME).read_text(encoding="utf-8")

    assert after["status"] == before["status"]
    assert after["revision"] == before["revision"]
    assert after["last_event_seq"] == before["last_event_seq"]
    assert events_after == events_before
    assert len(EVENT_TYPES) == 10


# ---------------------------------------------------------------------------
# 请求形状的失败面（§5.2 的 400 upload_invalid）
# ---------------------------------------------------------------------------

def test_missing_file_field_is_400_upload_invalid(tmp_path):
    with workbench_client(create_app(sessions_root=tmp_path)) as client:
        session_id = _new_session(client)
        resp = client.post(f"/api/v1/sessions/{session_id}/assets",
                           files={"other": ("a.png", _png(), "image/png")})

    _assert_problem(resp, 400, "upload_invalid")


def test_unparsable_multipart_is_400_upload_invalid(tmp_path):
    with workbench_client(create_app(sessions_root=tmp_path)) as client:
        session_id = _new_session(client)
        resp = client.post(
            f"/api/v1/sessions/{session_id}/assets",
            content=b"this is not multipart at all",
            headers={"Content-Type": "multipart/form-data; boundary=deadbeef"})

    _assert_problem(resp, 400, "upload_invalid")


def test_upload_without_credential_is_401(tmp_path):
    app = create_app(sessions_root=tmp_path)
    with workbench_client(app) as client:
        session_id = _new_session(client)
        anonymous = TestClient(app, base_url="http://127.0.0.1")
        resp = anonymous.post(f"/api/v1/sessions/{session_id}/assets",
                              files={"file": ("a.png", _png(), "image/png")})

    _assert_problem(resp, 401, "auth_required")


# ---------------------------------------------------------------------------
# 落盘失败（§10 的 storage_unavailable）
# ---------------------------------------------------------------------------

def test_disk_failure_is_507_and_cleans_the_temp_file(tmp_path, monkeypatch):
    real_replace = workbench_store.os.replace

    def _boom(src, dst, *args, **kwargs):
        raise OSError("磁盘满了")

    with workbench_client(create_app(sessions_root=tmp_path)) as client:
        session_id = _new_session(client)
        monkeypatch.setattr(workbench_store.os, "replace", _boom)
        try:
            resp = client.post(f"/api/v1/sessions/{session_id}/assets",
                               files={"file": ("a.png", _png(), "image/png")})
        finally:
            monkeypatch.setattr(workbench_store.os, "replace", real_replace)

    _assert_problem(resp, 507, "storage_unavailable")
    assert list(_assets_dir(tmp_path, session_id).iterdir()) == []


# ---------------------------------------------------------------------------
# 契约（D3-AC14）
# ---------------------------------------------------------------------------

def test_contract_declares_the_upload_trio(tmp_path):
    from minimax_h3_prompt.api.problems import PROBLEM_CONTENT_TYPE

    app = create_app(sessions_root=tmp_path)
    schema = app.openapi()
    path = "/api/v1/sessions/{session_id}/assets"
    operations = schema["paths"][path]

    assert {"get", "post"} <= set(operations)
    post = operations["post"]
    assert post["requestBody"]["content"]["multipart/form-data"]
    assert "201" in post["responses"] and "200" in post["responses"]
    for status in ("400", "404", "409", "413", "415", "507"):
        assert status in post["responses"], status
        assert PROBLEM_CONTENT_TYPE in post["responses"][status]["content"], status
    for status in ("401", "403"):
        assert status in post["responses"], status

    schemas = schema["components"]["schemas"]
    assert set(schemas["Asset"]["properties"]) == {
        "asset_id", "kind", "name", "bytes", "created_at"}
    assert "assets" in schemas["SessionDetail"]["properties"]
