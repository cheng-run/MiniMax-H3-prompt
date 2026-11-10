#!/usr/bin/env python
"""#226 D3 真实媒体验收驱动器（手动执行，不进 CI、不进 ``check.sh``）。

用法（在**主树**里、服务已另开终端启动）::

    python -m minimax_h3_prompt.api          # 终端 A：真实服务（只绑 127.0.0.1:8000）
    PYTHONIOENCODING=utf-8 PYTHONPATH=src \\
        .venv/Scripts/python.exe scripts/accept_d3.py \\
        --sessions-root output/sessions

默认自动挑一份真实照片（``output/`` 下最大的 PNG/JPEG）与最新的真实
``final.mp4``（``sessions_root`` 下）；也可以用 ``--photo`` / ``--artifact`` 指定。

为什么是独立脚本而不是 pytest：它打真实 HTTP、读用户机器上的真实素材与真实成片，
产物落在 gitignore 的 ``output/accept-d3/``；塞进 CI 既没有这些文件，也违背「真实
模型/真实素材不进 CI」的边界（#222 §14）。

证据里**不出现令牌**（ADR 0033 第 7 条）：取引导端点时只读状态码与 ``token`` 字段，
不把响应体写进报告；所有 URL 都不带凭据（媒体元素靠 ``wb_session`` Cookie）。

#226 证据评论必须包含：

1. 完整命令与服务启动方式。
2. 环境：UTC 起止、``health.version``、``base_url``、``sessions_root``。
3. 输入：照片路径/字节数/sha256、成片 ``artifact_id``/路径/字节数。
4. 每一步的状态码、耗时与字节核对结果。
5. D3-AC 逐条结论（本脚本覆盖「可在真实服务上验」的那些，其余指向 pytest /
   Playwright / CI，并明确哪些没有在本轮实测）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote

import httpx

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASE_URL = "http://127.0.0.1:8000"
PROBLEM = "application/problem+json"
# 与实现常量分开写：抄一份字面值，常量改了这条核对才会红（仓规）。
RANGE_FORMS = ("bytes=0-63", "bytes=-64")


class AcceptanceError(RuntimeError):
    """一步不达标：记录后继续能记的步骤，最后整体退出码非零。"""


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise AcceptanceError(message)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _encode_media(asset_id: str) -> str:
    """asset_id 自带会话标识里的斜杠：逐段保留，其余百分号编码（同 #214 §5.1）。"""
    return "/api/v1/media/" + quote(asset_id, safe="/")


def _encode_artifact(artifact_id: str) -> str:
    return "/api/v1/artifacts/" + quote(artifact_id, safe="/") + "/content"


def _problem(resp: httpx.Response, status: int, code: str) -> dict[str, Any]:
    _expect(resp.status_code == status, f"期望 {status}，实得 {resp.status_code}：{resp.text[:200]}")
    _expect(resp.headers.get("content-type", "").startswith(PROBLEM),
            f"错误响应不是 problem+json：{resp.headers.get('content-type')}")
    body = resp.json()
    _expect(body.get("code") == code, f"错误码 {body.get('code')!r} != {code!r}")
    return body


def _content_range(start: int, end: int, total: int) -> str:
    return f"bytes {start}-{end}/{total}"


def _assert_range(resp: httpx.Response, payload: bytes, start: int, end: int,
                  label: str) -> None:
    _expect(resp.status_code == 206, f"{label}：期望 206，实得 {resp.status_code}")
    expected = payload[start:end + 1]
    _expect(resp.content == expected,
            f"{label}：字节不一致（实得 {len(resp.content)}，期望 {len(expected)}）")
    _expect(resp.headers.get("content-range") == _content_range(start, end, len(payload)),
            f"{label}：Content-Range={resp.headers.get('content-range')!r}")


@dataclass
class Report:
    steps: list[dict[str, Any]] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)

    @contextmanager
    def step(self, name: str) -> Iterator[dict[str, Any]]:
        entry: dict[str, Any] = {"name": name, "ok": False, "ms": 0.0}
        started = time.perf_counter()
        try:
            yield entry
            entry["ok"] = True
        except AcceptanceError as exc:
            entry["error"] = str(exc)
            self.failures.append(f"{name}：{exc}")
        finally:
            entry["ms"] = round((time.perf_counter() - started) * 1000, 1)
            self.steps.append(entry)


def discover_photo(root: Path) -> Path | None:
    """挑一份真实照片：output 下最大的 PNG/JPEG（排除本次证据目录）。"""
    candidates: list[Path] = []
    for pattern in ("*.png", "*.jpg", "*.jpeg"):
        candidates.extend(p for p in root.rglob(pattern)
                          if "accept-d3" not in p.parts and p.stat().st_size > 200 * 1024)
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_size)


def discover_artifact(sessions_root: Path) -> tuple[str, Path] | None:
    """挑最新的一份真实 final.mp4；产物标识＝相对 sessions_root 的 POSIX 路径。"""
    candidates = [p for p in sessions_root.rglob("final.mp4") if p.is_file()]
    if not candidates:
        return None
    newest = max(candidates, key=lambda p: p.stat().st_mtime)
    artifact_id = newest.relative_to(sessions_root).as_posix()
    return artifact_id, newest


def discover_legacy_session(sessions_root: Path) -> str | None:
    """找一个快照 schema 明确不是工作台版本的会话；没有就返回 None。

    只凭磁盘事实判断，不拿「最新成片所在目录」猜——猜错时上传会真的往用户会话里
    写素材，然后那一步报红，等于用用户数据做判据。

    会话文件名与工作台 schema 从实现里取，不在脚本里重抄一份（抄漏了会静默判错）。
    """
    from minimax_h3_prompt.session_store import SESSION_FILENAME
    from minimax_h3_prompt.workbench_store import WORKBENCH_SCHEMA_VERSION

    for path in sorted(sessions_root.rglob(SESSION_FILENAME)):
        try:
            snapshot = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if snapshot.get("schema_version") != WORKBENCH_SCHEMA_VERSION:
            return path.parent.relative_to(sessions_root).as_posix()
    return None


def _fetch_token(client: httpx.Client) -> str:
    # 只取 token 字段；响应体不进日志、不进报告（ADR 0033 第 7 条）。
    resp = client.get("/api/v1/auth/token")
    _expect(resp.status_code == 200, f"引导令牌失败：{resp.status_code}")
    token = resp.json().get("token")
    _expect(isinstance(token, str) and len(token) > 0, "引导端点没有返回 token")
    return token


def _create_session(client: httpx.Client, token: str) -> str:
    resp = client.post(
        "/api/v1/sessions",
        headers={"X-Workbench-Token": token},
        json={"mode": "base", "variant": "T2VA", "duration": 4.0,
              "style": "写实", "language": "Chinese", "plot": "D3 真实媒体验收"},
    )
    _expect(resp.status_code in (200, 201), f"建会话失败：{resp.status_code} {resp.text[:200]}")
    session_id = resp.json().get("session_id")
    _expect(isinstance(session_id, str) and len(session_id) > 0,
            "建会话没有返回 session_id")
    return session_id


def _upload(client: httpx.Client, token: str, session_id: str, path: Path,
            *, name: str | None = None, idempotency_key: str | None = None) -> httpx.Response:
    headers = {"X-Workbench-Token": token}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    encoded = quote(session_id, safe="/")
    return client.post(
        f"/api/v1/sessions/{encoded}/assets",
        headers=headers,
        files={"file": (name or path.name, path.read_bytes(), "application/octet-stream")},
    )


def run(args: argparse.Namespace, report: Report) -> None:
    base_url = args.base_url.rstrip("/")
    with httpx.Client(base_url=base_url, timeout=args.timeout) as client:
        with report.step("环境与健康检查") as entry:
            health = client.get("/api/v1/health")
            _expect(health.status_code == 200, f"健康检查失败：{health.status_code}")
            version = health.json().get("version")
            entry["detail"] = {"version": version, "base_url": base_url}
            report.facts["version"] = version

        token = _fetch_token(client)

        with report.step("本地鉴权（D3-AC1 / AC2）") as entry:
            # 非回环 Host / Origin 必须分码 403；开发期 localhost:5173 必须放行。
            bad_host = client.get("/api/v1/health", headers={"Host": "evil.example"})
            _problem(bad_host, 403, "host_forbidden")
            dev_host = client.get("/api/v1/health", headers={"Host": "localhost:5173"})
            _expect(dev_host.status_code == 200, f"localhost:5173 应放行，实得 {dev_host.status_code}")
            bad_origin = client.get(
                "/api/v1/sessions",
                headers={"Origin": "http://evil.example", "X-Workbench-Token": token})
            _problem(bad_origin, 403, "origin_forbidden")
            # 无凭据的独立客户端：401；错误体里不出现令牌。
            with httpx.Client(base_url=base_url, timeout=args.timeout) as anonymous:
                unauth = anonymous.get("/api/v1/sessions")
                _problem(unauth, 401, "auth_required")
                _expect(token not in unauth.text, "错误体里出现了令牌")
            entry["detail"] = {
                "bad_host": bad_host.status_code,
                "dev_host_port_allowed": dev_host.status_code,
                "bad_origin": bad_origin.status_code,
                "anonymous": unauth.status_code,
            }

        session_id = _create_session(client, token)
        report.facts["session_id"] = session_id

        photo = Path(args.photo) if args.photo else discover_photo(ROOT / "output")
        if photo is None or not photo.is_file():
            report.failures.append("没有找到真实照片：请用 --photo 指定")
            return
        photo_bytes = photo.read_bytes()
        photo_sha = hashlib.sha256(photo_bytes).hexdigest()
        report.facts["photo"] = {
            "path": str(photo), "bytes": len(photo_bytes), "sha256": photo_sha,
        }

        with report.step("真实照片上传（D3-AC5 / AC6）") as entry:
            before = client.get(f"/api/v1/sessions/{quote(session_id, safe='/')}",
                                headers={"X-Workbench-Token": token}).json()
            first = _upload(client, token, session_id, photo)
            _expect(first.status_code == 201, f"上传失败：{first.status_code} {first.text[:200]}")
            asset = first.json()
            _expect(asset["asset_id"] == f"{session_id}/{photo_sha[:16]}",
                    f"asset_id 与内容哈希不符：{asset['asset_id']}")
            _expect(asset["kind"] == "image", f"嗅探类型不是 image：{asset['kind']}")
            # 扩展名由嗅探决定，不取文件名：这里按「哈希前缀 + 任意正本后缀」找，
            # 写死 .png 会在默认挑到 JPEG 时把整轮验收打断。
            assets_dir = Path(args.sessions_root) / Path(session_id) / "assets"
            payloads = [p for p in assets_dir.glob(f"{photo_sha[:16]}.*")
                        if p.suffix != ".json"]
            _expect(len(payloads) == 1,
                    f"哈希前缀对应的正本应恰好一份，实得 {len(payloads)}")
            on_disk = payloads[0]
            _expect(on_disk.read_bytes() == photo_bytes, "落盘字节与源文件不一致")
            # 同内容重复上传：同一个标识、不多一份文件。
            second = _upload(client, token, session_id, photo)
            _expect(second.status_code == 201, f"重复上传失败：{second.status_code}")
            _expect(second.json()["asset_id"] == asset["asset_id"], "同内容给出了不同标识")
            on_disk_count = len(list(on_disk.parent.glob(f"{photo_sha[:16]}.*")))
            # 正本 + 边车 = 两个文件；再多一份就是重复写盘。
            _expect(on_disk_count == 2, f"同内容落盘文件数 {on_disk_count} != 2（正本+边车）")
            after = client.get(f"/api/v1/sessions/{quote(session_id, safe='/')}",
                               headers={"X-Workbench-Token": token}).json()
            _expect(after["last_event_seq"] == before["last_event_seq"],
                    "上传改变了会话事件序列（应不产生事件）")
            entry["detail"] = {
                "asset_id": asset["asset_id"], "kind": asset["kind"],
                "bytes": asset["bytes"], "on_disk": str(on_disk),
                "dedupe_files": on_disk_count,
                "last_event_seq": after["last_event_seq"],
            }
            report.facts["asset"] = asset

        with report.step("伪装扩展名 415（D3-AC6）") as entry:
            fake = client.post(
                f"/api/v1/sessions/{quote(session_id, safe='/')}/assets",
                headers={"X-Workbench-Token": token},
                files={"file": ("fake.png", b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64,
                                "image/png")},
            )
            _problem(fake, 415, "unsupported_media_type")
            entry["detail"] = {"status": fake.status_code}

        with report.step("幂等重放（D3-AC7）") as entry:
            key = f"accept-d3-{photo_sha[:8]}"
            one = _upload(client, token, session_id, photo, idempotency_key=key)
            two = _upload(client, token, session_id, photo, idempotency_key=key)
            _expect(one.status_code == 201, f"首次幂等上传：{one.status_code}")
            _expect(two.status_code == 200, f"重放应回 200，实得 {two.status_code}")
            _expect(two.headers.get("idempotency-replayed") == "true", "缺少重放标记")
            _expect(one.json()["asset_id"] == two.json()["asset_id"], "重放换了标识")
            entry["detail"] = {"first": one.status_code, "replay": two.status_code}

        artifact_pick = (discover_artifact(Path(args.sessions_root))
                         if not args.artifact else None)
        if args.artifact:
            artifact_id = args.artifact
            artifact_path = Path(args.sessions_root) / Path(artifact_id)
        elif artifact_pick:
            artifact_id, artifact_path = artifact_pick
        else:
            artifact_id, artifact_path = "", Path()
        if not artifact_id or not artifact_path.is_file():
            report.failures.append("没有找到真实 final.mp4：请用 --artifact 指定")
            return
        artifact_bytes = artifact_path.read_bytes()
        report.facts["artifact"] = {
            "artifact_id": artifact_id, "path": str(artifact_path),
            "bytes": len(artifact_bytes),
        }

        media_url = _encode_media(asset["asset_id"])
        artifact_url = _encode_artifact(artifact_id)

        with report.step("媒体整份/HEAD/Cookie（D3-AC3 / AC10）") as entry:
            full = client.get(media_url, headers={"X-Workbench-Token": token})
            _expect(full.status_code == 200, f"整份读取：{full.status_code}")
            _expect(full.content == photo_bytes, "整份字节与源文件不一致")
            _expect(full.headers.get("accept-ranges") == "bytes", "缺 Accept-Ranges")
            _expect(full.headers.get("etag") == f'"{photo_sha[:16]}"',
                    f"ETag 不是内容哈希：{full.headers.get('etag')}")
            parsedate_to_datetime(full.headers["last-modified"])
            _expect("inline;" in full.headers.get("content-disposition", ""),
                    "缺 Content-Disposition: inline")
            # 不带令牌头、只靠 wb_session Cookie（浏览器媒体元素的唯一路径）。
            cookie_only = client.get(media_url)
            _expect(cookie_only.status_code == 200,
                    f"仅 Cookie 读取媒体失败：{cookie_only.status_code}")
            head = client.head(media_url, headers={"X-Workbench-Token": token})
            _expect(head.status_code == 200, f"HEAD：{head.status_code}")
            _expect(head.content == b"", "HEAD 不应有响应体")
            for header in ("content-type", "content-length", "etag", "last-modified",
                           "accept-ranges", "content-disposition"):
                _expect(head.headers.get(header) == full.headers.get(header),
                        f"HEAD 与 GET 的 {header} 不一致")
            entry["detail"] = {
                "status": full.status_code, "cookie_only": cookie_only.status_code,
                "etag": full.headers.get("etag"),
                "last_modified": full.headers.get("last-modified"),
            }

        with report.step("素材 Range 矩阵（D3-AC10）") as entry:
            total = len(photo_bytes)
            for form in RANGE_FORMS:
                resp = client.get(media_url, headers={"X-Workbench-Token": token, "Range": form})
                if form == "bytes=0-63":
                    _assert_range(resp, photo_bytes, 0, 63, form)
                else:
                    _assert_range(resp, photo_bytes, total - 64, total - 1, form)
            tail = client.get(media_url, headers={"X-Workbench-Token": token,
                                                  "Range": f"bytes={total // 2}-"})
            _assert_range(tail, photo_bytes, total // 2, total - 1, "bytes=a-")
            over = client.get(media_url, headers={"X-Workbench-Token": token,
                                                  "Range": f"bytes={total}-"})
            _problem(over, 416, "range_not_satisfiable")
            _expect(over.headers.get("content-range") == f"bytes */{total}",
                    f"416 的 Content-Range：{over.headers.get('content-range')}")
            multi = client.get(media_url, headers={"X-Workbench-Token": token,
                                                   "Range": "bytes=0-1,4-5"})
            _problem(multi, 416, "range_not_satisfiable")
            entry["detail"] = {"forms": list(RANGE_FORMS) + ["bytes=a-"],
                               "416_out_of_range": over.status_code,
                               "416_multi_range": multi.status_code}

        with report.step("成片字节与 Range（D3-AC11）") as entry:
            full = client.get(artifact_url, headers={"X-Workbench-Token": token})
            _expect(full.status_code == 200, f"成片整份读取：{full.status_code}")
            _expect(full.content == artifact_bytes, "成片字节与磁盘不一致")
            _expect(full.headers.get("content-type") == "video/mp4",
                    f"成片 Content-Type：{full.headers.get('content-type')}")
            start, end = 0, min(63, len(artifact_bytes) - 1)
            chunk = client.get(artifact_url, headers={"X-Workbench-Token": token,
                                                      "Range": f"bytes={start}-{end}"})
            _assert_range(chunk, artifact_bytes, start, end, "成片 bytes=0-63")
            head = client.head(artifact_url, headers={"X-Workbench-Token": token})
            _expect(head.status_code == 200 and head.content == b"", "成片 HEAD 形态不对")
            entry["detail"] = {"bytes": len(artifact_bytes),
                               "content_type": full.headers.get("content-type"),
                               "range": chunk.headers.get("content-range")}

        with report.step("越界标识 404（D3-AC12）") as entry:
            # 点号必须百分号编码：httpx 会在发请求前规范化字面 ..。
            traversal = client.get(
                "/api/v1/media/" + f"../secret/{photo_sha[:16]}".replace(".", "%2E"),
                headers={"X-Workbench-Token": token})
            _problem(traversal, 404, "asset_not_found")
            escaped = client.get(
                _encode_artifact("../secret/final.mp4").replace(".", "%2E"),
                headers={"X-Workbench-Token": token})
            _problem(escaped, 404, "artifact_not_found")
            entry["detail"] = {"media": traversal.status_code,
                               "artifact": escaped.status_code}

        with report.step("legacy 409（D3-AC8）") as entry:
            # 别拿「最新成片所在目录」冒充 legacy：那份目录可能是工作台会话，上传会
            # 真的写一份素材进用户数据再报红。只挑快照 schema 明确是 legacy 的会话；
            # 一个都没有就如实标「未验证」（本条的确定性证据在 pytest 里）。
            legacy_session = discover_legacy_session(Path(args.sessions_root))
            if legacy_session is None:
                entry["detail"] = {"skipped": "sessions_root 下没有 legacy 会话"}
            else:
                attempt = _upload(client, token, legacy_session, photo)
                _problem(attempt, 409, "session_legacy")
                entry["detail"] = {"session": legacy_session,
                                   "status": attempt.status_code}

        with report.step("素材列表与详情（D3-AC5）") as entry:
            encoded = quote(session_id, safe="/")
            listed = client.get(f"/api/v1/sessions/{encoded}/assets",
                                headers={"X-Workbench-Token": token}).json()
            detail = client.get(f"/api/v1/sessions/{encoded}",
                                headers={"X-Workbench-Token": token}).json()
            _expect(any(item["asset_id"] == asset["asset_id"] for item in listed),
                    "列表里没有刚上传的素材")
            _expect(any(item["asset_id"] == asset["asset_id"] for item in detail["assets"]),
                    "详情 assets 里没有刚上传的素材")
            entry["detail"] = {"listed": len(listed), "detail_assets": len(detail["assets"])}




def _write_report(report: Report, out_dir: Path) -> None:
    facts = report.facts
    step_rows = "\n".join(
        f"| {item['name']} | {'✅' if item['ok'] else '❌'} | {item['ms']:.0f} | "
        f"{item.get('error', '') or ''} |"
        for item in report.steps)
    photo = facts.get("photo", {})
    artifact = facts.get("artifact", {})
    lines = [
        "# D3 真实媒体验收（#226）",
        "",
        f"- 开始（UTC）：{facts.get('started_at', '')}",
        f"- 结束（UTC）：{facts.get('finished_at', '')}",
        f"- 服务版本：{facts.get('version', '')}",
        f"- 命令：`{facts.get('command', '')}`",
        f"- 会话：`{facts.get('session_id', '')}`",
        "",
        "## 输入（真实素材）",
        "",
        f"- 照片：`{photo.get('path', '')}`（{photo.get('bytes', 0)} 字节，"
        f"sha256 `{photo.get('sha256', '')}`）",
        f"- 成片：`{artifact.get('artifact_id', '')}` → `{artifact.get('path', '')}`"
        f"（{artifact.get('bytes', 0)} 字节）",
        "",
        "## 步骤与耗时",
        "",
        "| 步骤 | 结果 | 毫秒 | 备注 |",
        "|---|---|---|---|",
        step_rows,
        "",
        "## 结论",
        "",
        "全部步骤通过。" if not report.failures else f"未达标 {len(report.failures)} 项：",
    ]
    for failure in report.failures:
        lines.append(f"- {failure}")
    lines.append("")
    (out_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")
    (out_dir / "report.json").write_text(
        json.dumps({**facts, "steps": report.steps, "failures": report.failures},
                   ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="D3 真实媒体验收驱动器（#226）")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--out-dir", default=None)
    parser.add_argument("--photo", default=None, help="真实照片路径（默认自动挑）")
    parser.add_argument("--artifact", default=None,
                        help="真实成片 artifact_id（默认挑 sessions_root 下最新的 final.mp4）")
    parser.add_argument("--sessions-root", default=None,
                        help="默认取项目配置的 sessions_root，读不到时用 output/sessions")
    parser.add_argument("--command", default="", help="报告里记录的实际命令行")
    args = parser.parse_args(argv)
    root = args.sessions_root
    if root is None:
        try:
            from minimax_h3_prompt.config import config

            root = str(config.sessions_root)
        except Exception:  # noqa: BLE001 - 配置读不到不该挡住验收本身
            root = str(ROOT / "output" / "sessions")
    args.sessions_root = str(Path(root).resolve() if Path(root).is_absolute()
                             else (ROOT / root).resolve())
    return args


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass
    args = parse_args(argv)
    out_dir = (Path(args.out_dir) if args.out_dir
               else ROOT / "output" / "accept-d3" / _stamp())
    out_dir.mkdir(parents=True, exist_ok=True)
    report = Report()
    report.facts["output_dir"] = str(out_dir)
    # 起止与命令在任何失败路径上都要有值：报告本身就是证据。
    report.facts["started_at"] = _utc_now()
    report.facts["command"] = args.command or " ".join(sys.argv)
    try:
        run(args, report)
    except Exception as exc:  # noqa: BLE001 - 连不上或未预期异常也要落一份报告
        report.failures.append(f"未预期异常：{type(exc).__name__}: {exc}")
    report.facts["finished_at"] = _utc_now()
    _write_report(report, out_dir)
    if report.failures:
        print("验收未全部通过：")
        for failure in report.failures:
            print(f"- {failure}")
        print(f"报告：{out_dir}")
        return 1
    print(f"全部步骤通过：{len(report.steps)} 步；报告在 {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
