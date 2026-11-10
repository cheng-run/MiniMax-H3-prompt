"""D3-3（#225）：媒体读取——单段 Range、HEAD 与缓存标识（#222 §6 / §7）。

判据尽量来自**独立来源**：字节与磁盘上的文件逐字对照，Content-Range 自己算一遍
区间，ETag 用测试侧现算的 sha256 前缀比——不让端点自己证明自己。

成片走的是**另一个**端点（``/artifacts/{id}/content``），但 Range 语义必须与素材
一模一样：D3-3 的取舍就是两者共用一套解析与 416 形状。
"""
from __future__ import annotations

import asyncio
import hashlib
import tracemalloc
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote

import pytest

from minimax_h3_prompt.api.app import create_app
from minimax_h3_prompt.api.media import build_media_response
from minimax_h3_prompt.workbench_store import ASSET_MEDIA_TYPES
from workbench_client import workbench_client

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
# 40 字节：``a-b`` / ``a-`` / ``-n`` 三种形态都能切出互不相同的段。
PAYLOAD = PNG_MAGIC + bytes(range(32))
DISPLAY_NAME = "雨夜 图.png"
CACHE_HEADERS = ("content-type", "content-length", "etag", "last-modified",
                 "accept-ranges", "content-disposition")


def _new_session(client) -> str:
    body = {
        "mode": "base", "variant": "T2VA", "duration": 4.0,
        "style": "写实", "language": "Chinese", "plot": "雨夜旧信",
    }
    return client.post("/api/v1/sessions", json=body).json()["session_id"]


def _upload(client, session_id: str, *, payload: bytes = PAYLOAD,
            name: str = DISPLAY_NAME) -> dict:
    resp = client.post(
        f"/api/v1/sessions/{session_id}/assets",
        files={"file": (name, payload, "image/png")})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _media_url(asset_id: str) -> str:
    # asset_id 自带会话标识里的斜杠：拼进 URL 时逐段保留，其余一律百分号编码。
    return "/api/v1/media/" + quote(asset_id, safe="/")


def _assert_problem(resp, status: int, code: str) -> dict:
    assert resp.status_code == status, resp.text
    assert resp.headers["content-type"].startswith("application/problem+json")
    body = resp.json()
    assert body["status"] == status
    assert body["code"] == code
    return body


@pytest.fixture
def workbench(tmp_path):
    app = create_app(sessions_root=tmp_path)
    with workbench_client(app) as client:
        session_id = _new_session(client)
        asset = _upload(client, session_id)
        yield client, session_id, asset, tmp_path


# ---------------------------------------------------------------------------
# 整份读取与缓存标识（D3-AC10 的 GET / HEAD / ETag / Last-Modified）
# ---------------------------------------------------------------------------

def test_full_get_and_head_carry_the_same_cache_headers(workbench):
    client, session_id, asset, root = workbench
    digest = hashlib.sha256(PAYLOAD).hexdigest()
    url = _media_url(asset["asset_id"])

    full = client.get(url)
    head = client.head(url)

    assert full.status_code == 200, full.text
    assert full.content == PAYLOAD
    assert (root / Path(session_id) / "assets"
            / f"{digest[:16]}.png").read_bytes() == PAYLOAD
    assert full.headers["content-type"] == "image/png"
    assert full.headers["accept-ranges"] == "bytes"
    assert full.headers["content-length"] == str(len(PAYLOAD))
    # ETag 是内容哈希（文件名本身就是它）；带引号是 HTTP 的形态要求。
    assert full.headers["etag"] == f'"{digest[:16]}"'
    assert parsedate_to_datetime(full.headers["last-modified"])
    assert full.headers["content-disposition"] == (
        "inline; filename*=UTF-8''" + quote(DISPLAY_NAME, safe=""))

    assert head.status_code == 200
    assert head.content == b""
    for header in CACHE_HEADERS:
        assert head.headers[header] == full.headers[header], header


# ---------------------------------------------------------------------------
# Range 矩阵（D3-AC10）：三种合法形态、越界、非法、多段
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("range_header", "expected"), [
    ("bytes=0-3", PAYLOAD[0:4]),
    ("bytes=4-", PAYLOAD[4:]),
    ("bytes=-4", PAYLOAD[-4:]),
    # 结束位置越界不是错误：RFC 7233 允许截到文件末尾。
    ("bytes=0-999", PAYLOAD),
    ("bytes=39-39", PAYLOAD[39:40]),
])
def test_single_range_forms_return_206(workbench, range_header, expected):
    client, _session_id, asset, _root = workbench

    resp = client.get(_media_url(asset["asset_id"]),
                      headers={"Range": range_header})

    assert resp.status_code == 206, resp.text
    assert resp.content == expected
    start, end = resp.headers["content-range"].removeprefix("bytes ").split("/")[0].split("-")
    assert int(start) == expected_start(range_header, expected)
    assert int(end) == int(start) + len(expected) - 1
    assert resp.headers["content-range"].endswith(f"/{len(PAYLOAD)}")
    assert resp.headers["content-length"] == str(len(expected))


def expected_start(range_header: str, expected: bytes) -> int:
    """从字节内容反推区间起点：三种形态各按自己的规则算，不从响应头抄。"""
    if range_header.startswith("bytes=-"):
        return len(PAYLOAD) - int(range_header.removeprefix("bytes=-"))
    return int(range_header.removeprefix("bytes=").split("-")[0])


@pytest.mark.parametrize("range_header", [
    "bytes=40-",         # 起点等于长度
    "bytes=100-200",     # 整段在文件之外
    "bytes=5-2",         # 结束早于开始
    "bytes=abc",         # 语法非法
    "bytes=",            # 语法非法
    "bytes=-0",          # 后缀长度为零
    "bytes=0-1,4-5",     # 多段：明确不支持
    "bytes= 1 -",        # 段里带空格也不认
])
def test_unsatisfiable_or_malformed_ranges_are_416(workbench, range_header):
    client, _session_id, asset, _root = workbench

    resp = client.get(_media_url(asset["asset_id"]),
                      headers={"Range": range_header})

    _assert_problem(resp, 416, "range_not_satisfiable")
    # 416 必须告诉客户端文件多大，否则它没法重新挑范围。
    assert resp.headers["content-range"] == f"bytes */{len(PAYLOAD)}"


def test_head_with_a_range_answers_headers_only(workbench):
    client, _session_id, asset, _root = workbench

    resp = client.head(_media_url(asset["asset_id"]),
                       headers={"Range": "bytes=8-15"})

    assert resp.status_code == 206
    assert resp.content == b""
    assert resp.headers["content-range"] == f"bytes 8-15/{len(PAYLOAD)}"
    assert resp.headers["content-length"] == "8"


def test_head_with_an_unsatisfiable_range_answers_headers_only(workbench):
    """HEAD 的 416 也不带体，但头与 GET 一致（Content-Range 必须说清文件多大）。"""
    client, _session_id, asset, _root = workbench

    resp = client.head(_media_url(asset["asset_id"]),
                       headers={"Range": "bytes=9999-"})

    assert resp.status_code == 416
    assert resp.content == b""
    assert resp.headers["content-range"] == f"bytes */{len(PAYLOAD)}"
    assert resp.headers["content-type"].startswith("application/problem+json")


def test_unknown_range_unit_is_ignored(workbench):
    """RFC 7233 §3.1：不认识的单位要忽略（回整份），不是 416。"""
    client, _session_id, asset, _root = workbench

    resp = client.get(_media_url(asset["asset_id"]),
                      headers={"Range": "items=0-1"})

    assert resp.status_code == 200
    assert resp.content == PAYLOAD


# ---------------------------------------------------------------------------
# 成片走 content 端点：同一套 Range 口径（D3-AC11）
# ---------------------------------------------------------------------------

def test_final_mp4_range_matches_the_media_rules(tmp_path):
    payload = b"\x00\x00\x00\x18ftypmp42" + bytes(range(256)) * 4
    with workbench_client(create_app(sessions_root=tmp_path)) as client:
        session_id = _new_session(client)
        (tmp_path / Path(session_id) / "final.mp4").write_bytes(payload)
        url = f"/api/v1/artifacts/{session_id}/final.mp4/content"

        full = client.get(url)
        chunk = client.get(url, headers={"Range": "bytes=4-11"})
        bad = client.get(url, headers={"Range": "bytes=9999-"})

    assert full.status_code == 200, full.text
    assert full.content == payload
    assert full.headers["content-type"] == "video/mp4"
    assert full.headers["accept-ranges"] == "bytes"
    assert full.headers["etag"] and full.headers["last-modified"]

    assert chunk.status_code == 206
    assert chunk.content == payload[4:12]
    assert chunk.headers["content-range"] == f"bytes 4-11/{len(payload)}"

    _assert_problem(bad, 416, "range_not_satisfiable")
    assert bad.headers["content-range"] == f"bytes */{len(payload)}"


def test_final_mp4_answers_head_like_get(tmp_path):
    """spec §6.2 的 HEAD 一格同样适用于成片：§7 说它走同一套 Range 矩阵。"""
    payload = b"\x00\x00\x00\x18ftypmp42" + bytes(range(256)) * 4
    with workbench_client(create_app(sessions_root=tmp_path)) as client:
        session_id = _new_session(client)
        (tmp_path / Path(session_id) / "final.mp4").write_bytes(payload)
        url = f"/api/v1/artifacts/{session_id}/final.mp4/content"

        full = client.get(url)
        head = client.head(url)
        chunk = client.head(url, headers={"Range": "bytes=4-11"})

    assert head.status_code == 200
    assert head.content == b""
    for header in ("content-type", "content-length", "etag", "last-modified",
                   "accept-ranges", "content-disposition"):
        assert head.headers[header] == full.headers[header], header
    assert chunk.status_code == 206
    assert chunk.content == b""
    assert chunk.headers["content-range"] == f"bytes 4-11/{len(payload)}"
    assert chunk.headers["content-length"] == "8"


def test_text_artifact_answers_head_without_a_body(tmp_path):
    """文本产物的 HEAD：头照 GET 报（UTF-8 字节数），体按定义不发。"""
    with workbench_client(create_app(sessions_root=tmp_path)) as client:
        session_id = _new_session(client)
        (tmp_path / Path(session_id) / "video-prompt.md").write_text(
            "整片提示词", encoding="utf-8")
        url = f"/api/v1/artifacts/{session_id}/video-prompt.md/content"

        head = client.head(url)
        full = client.get(url)

    assert head.status_code == 200
    assert head.content == b""
    assert head.headers["content-type"].startswith("text/plain")
    assert head.headers["content-length"] == full.headers["content-length"]
    assert full.content.decode("utf-8") == "整片提示词"


def test_text_artifacts_stay_plain_text(tmp_path):
    """同一端点上的文本产物形态不变：纯文本、不带 Range 头（#222 §7 的表）。"""
    with workbench_client(create_app(sessions_root=tmp_path)) as client:
        session_id = _new_session(client)
        (tmp_path / Path(session_id) / "video-prompt.md").write_text(
            "整片提示词", encoding="utf-8")
        resp = client.get(
            f"/api/v1/artifacts/{session_id}/video-prompt.md/content",
            headers={"Range": "bytes=0-1"})

    assert resp.status_code == 200
    assert resp.text == "整片提示词"
    assert resp.headers["content-type"].startswith("text/plain")
    assert "content-range" not in resp.headers


# ---------------------------------------------------------------------------
# 越界标识（D3-AC12）：素材与产物两处都 404
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("asset_id", [
    "../secret/{hash}",
    "/etc/{hash}",
    "{session}/../../secret/{hash}",
    "C:/x/{hash}",
    "不存在/GEN999/{hash}",
    "{session}/{HASH_UPPER}",
    "{session}/{hash}.",
    "{session}/{hash}.png",     # 标识不带扩展名：扩展名是落盘形态，不是标识的一部分
    "{session}/{SHORT}",
    "{session}",                # 只有会话段
])
def test_escaping_and_off_shape_asset_ids_are_problem_404(workbench, asset_id):
    client, session_id, _asset, root = workbench
    digest = hashlib.sha256(PAYLOAD).hexdigest()
    hash16 = digest[:16]
    outside = root.parent / "secret"
    outside.mkdir(exist_ok=True)
    (outside / f"{hash16}.png").write_bytes("根外机密".encode("utf-8"))

    target = asset_id.format(session=session_id, hash=hash16,
                             HASH_UPPER=hash16.upper(), SHORT=hash16[:8])
    # 点号要百分号编码：httpx 会在发请求前把字面 ``..`` 规范化掉，那样测的就不是
    # 服务端的越界判定，而是客户端自己的路径清洗（test_api_artifacts 同款）。
    resp = client.get("/api/v1/media/" + target.replace(".", "%2E"))

    _assert_problem(resp, 404, "asset_not_found")
    assert "根外机密" not in resp.text


def test_symlinked_asset_outside_the_root_is_404(workbench):
    client, session_id, asset, root = workbench
    digest = hashlib.sha256(PAYLOAD).hexdigest()
    outside = root.parent / "secret.png"
    outside.write_bytes("根外机密".encode("utf-8"))
    link = root / Path(session_id) / "assets" / f"{digest[:16]}.png"
    try:
        link.unlink()
        link.symlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"本机建不了符号链接：{exc}")

    resp = client.get(_media_url(asset["asset_id"]))

    _assert_problem(resp, 404, "asset_not_found")
    assert "根外机密" not in resp.text


@pytest.mark.parametrize("artifact_id", [
    "../secret/final.mp4",
    "/etc/final.mp4",
    "{session}/../../secret/final.mp4",
    "{session}/notes.mp4",      # 不在登记表里
    "{session}/final.mov",      # 近似名也不算
    "final.mp4",
])
def test_escaping_or_unregistered_artifact_media_ids_are_404(tmp_path, artifact_id):
    with workbench_client(create_app(sessions_root=tmp_path)) as client:
        session_id = _new_session(client)
        outside = tmp_path.parent / "secret"
        outside.mkdir(exist_ok=True)
        (outside / "final.mp4").write_bytes("根外机密".encode("utf-8"))
        target = artifact_id.format(session=session_id)
        encoded = target.replace(".", "%2E")
        resp = client.get(f"/api/v1/artifacts/{encoded}/content")

    _assert_problem(resp, 404, "artifact_not_found")
    assert "根外机密" not in resp.text


# ---------------------------------------------------------------------------
# 凭据（D3-AC2 / §4.5：媒体端点不在豁免面上）
# ---------------------------------------------------------------------------

def test_media_without_credential_is_401(tmp_path):
    from fastapi.testclient import TestClient

    app = create_app(sessions_root=tmp_path)
    with workbench_client(app) as client:
        asset = _upload(client, _new_session(client))
    anonymous = TestClient(app, base_url="http://127.0.0.1")
    resp = anonymous.get(_media_url(asset["asset_id"]))

    _assert_problem(resp, 401, "auth_required")


# ---------------------------------------------------------------------------
# 内存边界：读文件按块，不整体进内存
# ---------------------------------------------------------------------------

def _write_big_file(path: Path, total: int, head: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    block = b"\x00" * (1024 * 1024)
    with path.open("wb") as handle:
        handle.write(head)
        remaining = total - len(head)
        while remaining > 0:
            chunk = block[: min(len(block), remaining)]
            handle.write(chunk)
            remaining -= len(chunk)


def test_serving_does_not_read_the_whole_file_into_memory(tmp_path):
    """64 MiB 的素材，服务侧 Python 峰值必须远小于文件本身（按块读）。"""
    total = 64 * 1024 * 1024
    hash16 = "ab" * 8
    with workbench_client(create_app(sessions_root=tmp_path)) as client:
        session_id = _new_session(client)
        _write_big_file(tmp_path / Path(session_id) / "assets" / f"{hash16}.mp4",
                        total, b"\x00\x00\x00\x18ftypmp42")
        store = client.app.state.session_store  # type: ignore[attr-defined]

    media = store.resolve_asset_media(f"{session_id}/{hash16}")
    response = build_media_response(media, None, request_path="/api/v1/media/x")

    async def _drain() -> int:
        read = 0
        async for chunk in response.body_iterator:
            read += len(chunk)
        return read

    tracemalloc.start()
    try:
        read = asyncio.run(_drain())
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert read == total
    assert peak < 8 * 1024 * 1024, f"峰值 {peak} 字节：文件被整体读入了"


# ---------------------------------------------------------------------------
# 契约（D3-AC14）
# ---------------------------------------------------------------------------

def test_contract_declares_media_get_head_and_their_failures(tmp_path):
    app = create_app(sessions_root=tmp_path)
    schema = app.openapi()
    path = schema["paths"]["/api/v1/media/{asset_id}"]

    assert {"get", "head"} <= set(path)
    for method in ("get", "head"):
        responses = path[method]["responses"]
        for status in ("200", "206", "404", "416"):
            assert status in responses, (method, status)
        for status in ("401", "403"):
            assert status in responses, (method, status)
        for status in ("404", "416"):
            assert ("application/problem+json"
                    in responses[status]["content"]), (method, status)

    # 可选 Content-Type 由素材格式表现算：加一种上传格式就自动出现在这里。
    declared = set(path["get"]["responses"]["200"]["content"])
    assert declared == set(ASSET_MEDIA_TYPES.values())
    # 固定头（§6.1）在契约里也要给全：Content-Length 同样是浏览器要看的。
    for method in ("get", "head"):
        headers = path[method]["responses"]["200"]["headers"]
        for header in ("Accept-Ranges", "ETag", "Last-Modified",
                       "Content-Length", "Content-Disposition"):
            assert header in headers, (method, header)
    # HEAD 按定义没有响应体：契约里不该声明 content。
    assert "content" not in path["head"]["responses"]["200"]
    assert "content" not in path["head"]["responses"]["206"]
