"""单段 HTTP Range 的解析与字节响应（#222 §6.2 / §7，D3-3）。

素材与成片（``final.mp4``）共用这一份实现：两者只是**解析到文件的路径不同**，
Range 语义、缓存头与 416 的形状必须一模一样——各写一份迟早出现「素材能拖、成片
拖不动」这种只在一半端点上成立的差异。

只支持单段（spec 明写）：浏览器 ``<video>`` 的播放与拖动只用单段，多段要
``multipart/byteranges``，那是另一套响应体格式，为一个没有消费者的能力不值得。
"""
from __future__ import annotations

from collections.abc import Iterable, Iterator
from email.utils import formatdate
from pathlib import Path
import re
from typing import Any
from urllib.parse import quote

from fastapi import Response
from fastapi.responses import StreamingResponse

from ..workbench_store import MediaResource, WorkbenchError
from .problems import build_problem_response

# 读文件块大小：一次一块进内存，「流式」才不是一句空话（64 KiB 与上传侧同档）。
MEDIA_CHUNK_BYTES = 64 * 1024

# 契约里的响应头与响应体声明：媒体端点与成片的 content 端点共用这一份。运行时发的
# 是同一组头（``build_media_response``），契约里一处有一处没有就是两套口径。
MEDIA_RESPONSE_HEADERS: dict[str, Any] = {
    "Accept-Ranges": {"description": "恒为 bytes（单段）", "schema": {"type": "string"}},
    "ETag": {"description": "缓存标识（素材即内容哈希）", "schema": {"type": "string"}},
    "Last-Modified": {"description": "文件修改时间（HTTP-date）",
                      "schema": {"type": "string"}},
    "Content-Length": {"description": "本次响应的字节数（HEAD 时为 GET 会给的字节数）",
                       "schema": {"type": "integer"}},
    "Content-Disposition": {"description": "inline；显示名按 RFC 5987 编码",
                            "schema": {"type": "string"}},
}
RANGE_RESPONSE_HEADERS: dict[str, Any] = {
    **MEDIA_RESPONSE_HEADERS,
    "Content-Range": {"description": "bytes <start>-<end>/<len>",
                      "schema": {"type": "string"}},
}


def binary_content(media_types: Iterable[str]) -> dict[str, Any]:
    """契约里的二进制响应体：按给定 Content-Type 逐个列，去重保序。"""
    return {media_type: {"schema": {"type": "string", "format": "binary"}}
            for media_type in dict.fromkeys(media_types)}

# ``bytes=<first>-<last>`` 两段都可空：``a-b`` / ``a-`` / ``-n`` 三种形态都落在这里。
_RANGE_SPEC_RE = re.compile(r"^(?P<first>\d*)-(?P<last>\d*)$")


class RangeNotSatisfiable(WorkbenchError):
    """越界、语法非法或多段的 Range（#222 §6.2 / §10）。

    端点会捕获它并补上 ``Content-Range: bytes */<len>``；没捕获时它仍是一条
    416 problem+json——缺的只是那半句用来告诉客户端「文件到底多大」。
    """

    status_code = 416
    code = "range_not_satisfiable"


def parse_range_header(value: str | None, size: int) -> tuple[int, int] | None:
    """把 ``Range`` 头解析成闭区间 ``(start, end)``；``None``＝整份 200。

    越界、``a>b``、语法非法与多段一律抛 ``RangeNotSatisfiable``（#222 §6.2）。
    不认识的**单位**（不是 ``bytes``）按 RFC 7233 §3.1 忽略——那是「服务器不理解的
    可选特性」，忽略后回整份是协议要求，不是失败。
    """
    if value is None:
        return None
    unit, sep, spec = value.partition("=")
    if not sep or unit.strip().lower() != "bytes":
        return None
    spec = spec.strip()
    if "," in spec:
        raise RangeNotSatisfiable("本端点只支持单段 Range，不支持 multipart/byteranges。")
    match = _RANGE_SPEC_RE.match(spec)
    if match is None:
        raise RangeNotSatisfiable(f"Range 语法非法：{value!r}。")
    first_raw, last_raw = match.group("first"), match.group("last")
    if not first_raw and not last_raw:
        raise RangeNotSatisfiable(f"Range 语法非法：{value!r}。")
    if not first_raw:
        # 后缀形态 ``-n``：最后 n 个字节；``-0`` 是零长度，按 RFC 属不可满足。
        suffix = int(last_raw)
        if suffix == 0 or size == 0:
            raise RangeNotSatisfiable(f"Range 在 {size} 字节的文件上无法满足。")
        return max(size - suffix, 0), size - 1
    start = int(first_raw)
    end = int(last_raw) if last_raw else size - 1
    if last_raw and end < start:
        raise RangeNotSatisfiable(f"Range 结束早于开始：{value!r}。")
    if start >= size:
        raise RangeNotSatisfiable(f"Range 起点 {start} 超出 {size} 字节的文件。")
    # 结束位置越界不是错误：RFC 允许截到文件末尾（回 206 而不是 416）。
    return start, min(end, size - 1)


def _file_chunks(path: Path, start: int, length: int) -> Iterator[bytes]:
    """按块读区间；``length`` 由调用方按 Range 算好，不在这里重新数文件。"""
    with path.open("rb") as handle:
        handle.seek(start)
        remaining = length
        while remaining > 0:
            chunk = handle.read(min(MEDIA_CHUNK_BYTES, remaining))
            if not chunk:
                return
            remaining -= len(chunk)
            yield chunk


def _range_not_satisfiable(size: int, request_path: str) -> Response:
    response = build_problem_response(
        416,
        f"请求的字节范围无法满足（文件 {size} 字节）；本端点只支持单段 Range。",
        request_path,
        code="range_not_satisfiable",
    )
    # 416 必须告诉客户端文件多大（RFC 7233 §4.4），否则它没法重新挑范围。
    response.headers["Content-Range"] = f"bytes */{size}"
    return response


def build_media_response(
    media: MediaResource,
    range_header: str | None,
    *,
    request_path: str,
    head: bool = False,
) -> Response:
    """按 ``Range`` 头构造 200 / 206 / 416 之一。

    固定头（``Accept-Ranges`` / ``ETag`` / ``Last-Modified`` / ``Content-Type`` /
    ``Content-Disposition``）在两条成功响应（200 / 206）上都给：浏览器拖动时会重复
    请求同一段，没有缓存标识就只能整段重读。
    """
    headers = {
        "Accept-Ranges": "bytes",
        "ETag": f'"{media.etag}"',
        "Last-Modified": formatdate(media.mtime, usegmt=True),
        "Content-Type": media.media_type,
    }
    if media.filename:
        # RFC 5987：显示名可能是中文，``filename*`` 用 UTF-8 百分号编码带出去。
        headers["Content-Disposition"] = (
            f"inline; filename*=UTF-8''{quote(media.filename, safe='')}")
    try:
        byte_range = parse_range_header(range_header, media.size)
    except RangeNotSatisfiable:
        return _range_not_satisfiable(media.size, request_path)
    if byte_range is None:
        status = 200
        start, length = 0, media.size
    else:
        status = 206
        start, end = byte_range
        length = end - start + 1
        headers["Content-Range"] = f"bytes {start}-{end}/{media.size}"
    headers["Content-Length"] = str(length)
    if head:
        # HEAD 与 GET 同头、无体：Content-Length 说的是「GET 会给多少字节」。
        return Response(status_code=status, headers=headers)
    return StreamingResponse(
        _file_chunks(media.path, start, length),
        status_code=status,
        headers=headers,
    )
