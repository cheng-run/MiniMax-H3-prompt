#!/usr/bin/env python
"""#221 D2 真实跑的验收驱动器（手动执行，不进 CI、不进 ``check.sh``）。

用法（在**主树**里、服务已另开终端启动）::

    python -m minimax_h3_prompt.api          # 终端 A：真实模型、真实 .env
    PYTHONIOENCODING=utf-8 PYTHONPATH=src \\
        .venv/Scripts/python.exe scripts/accept_d2.py \\
        --plot "雨夜街口，一位撑黑伞的女性走过积水，霓虹反光" \\
        --sessions-root output/sessions

参数：``--base-url``（默认 ``http://127.0.0.1:8000``）、``--timeout``（单步等待
秒数，默认 1800）、``--out-dir``（默认 ``output/accept-d2/<UTC 时间戳>/``）、
``--plot``、``--duration``（默认 4 秒）、``--sessions-root``（默认取项目配置的
``sessions_root``；读不到时相关核对标为「未验证」）。

为什么是独立脚本而不是 pytest：它打真实 HTTP、跑真实模型，一条要几十分钟；放进
``check.sh`` 就等于把真实模型调用塞进 CI（D2 spec §2 明确不做）。

#214 证据评论必须包含（``docs/agents/issue-tracker.md`` 的关票口径）：

1. 完整命令：服务启动命令与本脚本命令（含 ``--plot`` / ``--sessions-root`` /
   ``--base-url``）。
2. 环境：UTC 起止时间、``health.version``、``sessions_root``、会话标识。
3. 产物：``video-prompt.md`` 的路径与字节数（内容随报告落盘一份供比对）。
4. 事件流：``seq`` 范围、类型计数，以及 ``job.started`` / ``job.succeeded`` /
   ``action_item.created`` / ``action_item.resolved`` / ``artifact.created`` 的摘录。
5. 用时：建会话、阶段 1、待办决议、阶段 2、总耗时。
6. D2-AC1..AC19 逐条结论：直接贴 ``report.md`` 的表格；不可自动核对的标
   「需人工/测试证据」，不许假装通过。
7. 未达标项与四项越界确认（上传 / 鉴权 / 其他路线 / 真实模型进 CI）逐条写
   「未做」及依据。
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import httpx

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PLOT = "雨夜街口，一位撑黑伞的女性走过积水，霓虹反光，写实电影感。"
DEFAULT_POLL_SECONDS = 2.0
SNIPPET_LIMIT = 1200
PROBLEM_CONTENT_TYPE = "application/problem+json"

# 这些 AC 的证据不来自本驱动器：取消、强杀、SSE 重连、契约漂移、确定性桩、向导
# 不变性与 ``--brief`` 快路径都由用例或 CI 提供。报告里如实标出来，不许冒充通过。
_MANUAL_CHECKS: tuple[tuple[str, str, str], ...] = (
    ("D2-AC8", "取消在节点边界生效", "由 tests/test_api_jobs.py 的取消用例提供"),
    ("D2-AC9", "强杀重启后对账并置 interrupted", "由 tests/test_api_jobs.py 的启动对账用例提供"),
    ("D2-AC10", "SSE 断线 Last-Event-ID 补发与缺口", "由 tests/test_api_events.py 提供；本脚本只做尾部补发探测"),
    ("D2-AC14", "OpenAPI 与前端生成客户端无漂移", "由 CI drift check 提供"),
    ("D2-AC15", "确定性桩全链路且真实节点被执行", "由 tests/test_api_jobs.py 的 model.calls 计数提供"),
    ("D2-AC16", "向导既有测试全绿并驱动同一台步骤机", "由 tests/test_wizard*.py 提供"),
    ("D2-AC17", "--brief 快路径行为不变且不进会话列表", "由 tests/test_brief_parser.py 等用例提供"),
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _clip(value: Any, limit: int = SNIPPET_LIMIT) -> Any:
    """报告只留摘要：真实 prompt 可能有几万字符，整份塞进 json 会把证据淹没。"""
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + f"…（截断，共 {len(value)} 字符）"
    if isinstance(value, list):
        return [_clip(item, limit) for item in value]
    if isinstance(value, dict):
        return {str(key): _clip(item, limit) for key, item in value.items()}
    return value


def _detail_summary(detail: dict[str, Any]) -> dict[str, Any]:
    """详情响应的人读摘要；完整响应仍会写进 report.json 的步骤体。"""
    brief = detail.get("brief") if isinstance(detail.get("brief"), dict) else {}
    return {
        "session_id": detail.get("session_id"),
        "status": detail.get("status"),
        "legacy": detail.get("legacy"),
        "revision": detail.get("revision"),
        "last_event_seq": detail.get("last_event_seq"),
        "current_job": detail.get("current_job"),
        "jobs": detail.get("jobs"),
        "action_items": detail.get("action_items"),
        "artifacts": detail.get("artifacts"),
        "brief": {key: brief.get(key) for key in ("mode", "variant", "duration")},
    }


def is_loopback(base_url: str) -> bool:
    """base_url 的主机是否回环地址；只核对「我们连的是本机」，不证明监听范围。"""
    host = (urlparse(base_url).hostname or "").lower()
    return host in {"127.0.0.1", "localhost", "::1"}


def event_seq_problems(events: list[dict[str, Any]]) -> list[str]:
    """事件序号必须从 1 起严格 +1：无跳号、无重号、无回退（D2-AC11）。"""
    problems: list[str] = []
    expected = 1
    for index, event in enumerate(events, start=1):
        seq = event.get("seq")
        if not isinstance(seq, int):
            problems.append(f"第 {index} 条的 seq 不是整数：{seq!r}")
            continue
        if seq != expected:
            problems.append(f"第 {index} 条 seq={seq}，期望 {expected}")
        expected = seq + 1
    return problems


class StepError(RuntimeError):
    """某一步没到预期状态：驱动器立刻停下，绝不静默继续。"""

    def __init__(self, step: str, detail: str, response_text: str = "") -> None:
        super().__init__(f"{step}：{detail}")
        self.step = step
        self.detail = detail
        self.response_text = response_text


@dataclass
class Settings:
    base_url: str = "http://127.0.0.1:8000"
    timeout: float = 1800.0
    out_dir: Path = field(default_factory=lambda: ROOT / "output" / "accept-d2" / _stamp())
    plot: str = DEFAULT_PLOT
    duration: float = 4.0
    sessions_root: Path | None = None
    poll_seconds: float = DEFAULT_POLL_SECONDS
    run_id: str = field(default_factory=lambda: f"{int(time.time())}")


class Acceptor:
    """一次验收运行的记录器：HTTP 调用、步骤、AC 结论与最终报告。"""

    def __init__(self, client: httpx.Client, settings: Settings) -> None:
        self.client = client
        self.settings = settings
        self.started_at = _utc_now()
        self.steps: list[dict[str, Any]] = []
        self.checks: list[dict[str, Any]] = []
        self.timings: dict[str, float] = {}
        self.session_id: str = ""
        self.artifacts: list[dict[str, Any]] = []
        self.artifact_text_path: str = ""
        self.artifact_text_bytes = 0
        self.events: list[dict[str, Any]] = []
        self.events_source = ""
        self.failure: dict[str, Any] | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # 记录
    # ------------------------------------------------------------------
    def record(self, step: str, method: str, path: str, *,
               status: int | None, elapsed_ms: int, body: Any = None,
               replayed: str | None = None, note: str = "") -> None:
        entry = {
            "step": step, "method": method, "path": path,
            "status": status, "elapsed_ms": elapsed_ms,
            "idempotency_replayed": replayed,
        }
        if body is not None:
            entry["body"] = _clip(body)
        if note:
            entry["note"] = note
        with self._lock:
            self.steps.append(entry)

    def check(self, ac: str, title: str, ok: bool | None, detail: str) -> None:
        with self._lock:
            self.checks.append({"ac": ac, "title": title, "ok": ok, "detail": detail})
        mark = "通过" if ok is True else ("不通过" if ok is False else "需人工/测试证据")
        print(f"[AC] {ac} {title}：{mark}（{detail}）", flush=True)

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------
    def request(self, step: str, method: str, path: str, **kwargs: Any) -> httpx.Response:
        started = time.monotonic()
        try:
            response = self.client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            self.record(step, method, path, status=None, elapsed_ms=elapsed_ms,
                        note=f"HTTP 请求失败：{type(exc).__name__}: {exc}")
            raise StepError(step, f"HTTP 请求失败：{type(exc).__name__}: {exc}") from exc
        elapsed_ms = int((time.monotonic() - started) * 1000)
        body = self._body(response)
        self.record(step, method, path, status=response.status_code,
                    elapsed_ms=elapsed_ms, body=body,
                    replayed=response.headers.get("Idempotency-Replayed"),
                    note=response.headers.get("content-type", ""))
        print(f"[步骤] {step}：{method} {path} → {response.status_code}"
              f"（{elapsed_ms} ms）", flush=True)
        return response

    @staticmethod
    def _body(response: httpx.Response) -> Any:
        content_type = response.headers.get("content-type", "")
        if PROBLEM_CONTENT_TYPE in content_type or "json" in content_type:
            try:
                return response.json()
            except ValueError:
                return response.text
        return response.text

    def expect(self, response: httpx.Response, status: int, step: str) -> None:
        if response.status_code != status:
            raise StepError(step, f"期望 HTTP {status}，实际 {response.status_code}",
                            response.text)

    def json_of(self, response: httpx.Response, step: str) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise StepError(step, "响应不是 JSON", response.text) from exc

    def assert_problem(self, response: httpx.Response, status: int, code: str | None,
                       step: str) -> dict[str, Any]:
        """AC13 的机械核对：状态码、媒体类型、稳定错误码、错误体不泄漏密钥字段。"""
        content_type = response.headers.get("content-type", "")
        if response.status_code != status:
            raise StepError(step, f"期望 {status}，实际 {response.status_code}", response.text)
        if not content_type.startswith(PROBLEM_CONTENT_TYPE):
            raise StepError(step, f"content-type 不是 {PROBLEM_CONTENT_TYPE}：{content_type}",
                            response.text)
        body = self.json_of(response, step)
        if body.get("code") != code:
            raise StepError(step, f"错误码期望 {code!r}，实际 {body.get('code')!r}",
                            response.text)
        banned = {"api_key", "apikey", "token", "secret", "password", "authorization"}
        leaked = banned & {str(key).lower() for key in body}
        if leaked:
            raise StepError(step, f"错误响应含疑似密钥字段：{sorted(leaked)}", response.text)
        return body

    # ------------------------------------------------------------------
    # 轮询与事件
    # ------------------------------------------------------------------
    def session_path(self, session_id: str) -> str:
        return "/api/v1/sessions/" + quote(session_id, safe="/")

    def wait_status(self, wanted: set[str], *, label: str,
                    timeout: float | None = None) -> dict[str, Any]:
        limit = self.settings.timeout if timeout is None else timeout
        started = time.monotonic()
        polls = 0
        detail: dict[str, Any] = {}
        while time.monotonic() - started < limit:
            response = self.client.get(self.session_path(self.session_id))
            polls += 1
            if response.status_code != 200:
                self.record(label, "GET", self.session_path(self.session_id),
                            status=response.status_code, elapsed_ms=0,
                            body=self._body(response), note=f"轮询第 {polls} 次")
                raise StepError(label, f"轮询详情失败：HTTP {response.status_code}",
                                response.text)
            detail = self.json_of(response, label)
            status = detail.get("status")
            if status in wanted:
                elapsed = time.monotonic() - started
                self.timings[label] = round(elapsed, 3)
                self.record(
                    label, "GET", self.session_path(self.session_id),
                    status=response.status_code, elapsed_ms=int(elapsed * 1000),
                    body=_detail_summary(detail), note=f"轮询 {polls} 次，到达 {status}")
                return detail
            if status in {"failed", "cancelled", "interrupted"}:
                job = detail.get("current_job") or {}
                self.record(
                    label, "GET", self.session_path(self.session_id),
                    status=response.status_code, elapsed_ms=0,
                    body=_detail_summary(detail), note=f"轮询第 {polls} 次")
                raise StepError(
                    label, f"等待 {sorted(wanted)} 时进入 {status}"
                    f"（job={job.get('job_id')} error={job.get('error_code')}）",
                    response.text)
            time.sleep(self.settings.poll_seconds)
        raise StepError(label, f"{limit}s 内没有到达 {sorted(wanted)}，"
                               f"最后状态 {detail.get('status')!r}")

    def read_events(self) -> None:
        """取该会话的全部事件：优先读事件文件，读不到再退回 SSE 补发。"""
        if self.settings.sessions_root is not None:
            path = self.settings.sessions_root / Path(self.session_id) / "events.jsonl"
            if path.is_file():
                self.events_source = str(path)
                text = path.read_text(encoding="utf-8")
                try:
                    self.events = [
                        json.loads(line) for line in text.splitlines() if line.strip()
                    ]
                except ValueError as exc:
                    raise StepError("事件流", f"事件文件有无法解析的行：{exc}", text) from exc
                # 事件文件是证据本体：复制进报告目录，避免会话目录以后被清理。
                (self.settings.out_dir / "events.jsonl").write_text(
                    text, encoding="utf-8")
                return
        self.events_source = "SSE /events?last_event_id=0"
        self.events = self._collect_sse(cursor=0)
        (self.settings.out_dir / "events.jsonl").write_text(
            "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in self.events),
            encoding="utf-8")

    def _collect_sse(self, cursor: int) -> list[dict[str, Any]]:
        """读 SSE 直到空闲超时；缺失部分由 ``Last-Event-ID`` 补发语义保证。"""
        events: list[dict[str, Any]] = []
        timeout = httpx.Timeout(connect=10.0, read=5.0, write=10.0, pool=10.0)
        try:
            with self.client.stream(
                "GET", self.session_path(self.session_id) + "/events",
                params={"last_event_id": cursor}, timeout=timeout,
            ) as response:
                if response.status_code != 200:
                    raise StepError("事件流", f"SSE 返回 HTTP {response.status_code}")
                for line in response.iter_lines():
                    if line.startswith("data: "):
                        events.append(json.loads(line[6:]))
        except httpx.ReadTimeout:
            pass
        return events

    def probe_sse_tail(self) -> list[int]:
        """尾部补发探测：从尾号前两条起订阅，期望收到最后两条。失败只记「未验证」。"""
        tail = self.events[-1]["seq"] if self.events else 0
        if tail < 2:
            return []
        cursor = tail - 2
        ids: list[int] = []
        timeout = httpx.Timeout(connect=5.0, read=5.0, write=5.0, pool=5.0)
        try:
            with self.client.stream(
                "GET", self.session_path(self.session_id) + "/events",
                params={"last_event_id": cursor}, timeout=timeout,
            ) as response:
                if response.status_code != 200:
                    return []
                for line in response.iter_lines():
                    if line.startswith("id: "):
                        ids.append(int(line[4:]))
                    if len(ids) >= 2:
                        break
        except (httpx.HTTPError, ValueError):
            return ids
        return ids


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def _create_payload(settings: Settings) -> dict[str, Any]:
    return {
        "mode": "base",
        "variant": "T2VA",
        "duration": settings.duration,
        "style": "写实",
        "language": "Chinese",
        "plot": settings.plot,
    }


def _check_state_file(acc: Acceptor, phase: str) -> None:
    """AC2：快照是 workbench.v1、session_id 自校验、无残留临时文件。"""
    root = acc.settings.sessions_root
    if root is None:
        acc.check("D2-AC2", f"workbench.v1 与原子写（{phase}）", None,
                  "未提供 --sessions-root，无法读快照文件")
        return
    problems: list[str] = []
    directory = root / Path(acc.session_id)
    path = directory / "session-state.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        acc.check("D2-AC2", f"workbench.v1 与原子写（{phase}）", False,
                  f"读不到或解析不了 {path}：{exc}")
        return
    if raw.get("schema_version") != "workbench.v1":
        problems.append(f"schema_version={raw.get('schema_version')!r}")
    if raw.get("session_id") != acc.session_id:
        problems.append(f"session_id={raw.get('session_id')!r} 与目录不符")
    if not isinstance(raw.get("revision"), int) or raw["revision"] < 1:
        problems.append(f"revision={raw.get('revision')!r}")
    if not isinstance(raw.get("last_event_seq"), int):
        problems.append(f"last_event_seq={raw.get('last_event_seq')!r}")
    leftovers = sorted({
        item.name
        for pattern in ("*.tmp", ".*.tmp")
        for item in directory.glob(pattern)
    })
    if leftovers:
        problems.append(f"残留临时文件：{leftovers}")
    # 代码证据：原子写的实现必须是 tmp + os.replace（文件层面只能看到结果）。
    try:
        import inspect

        from minimax_h3_prompt.session_store import atomic_write_text

        source = inspect.getsource(atomic_write_text)
        if "os.replace" not in source or "mkstemp" not in source:
            problems.append("atomic_write_text 未使用 mkstemp + os.replace")
    except Exception as exc:  # noqa: BLE001 - 只影响补充证据，不该打断验收
        problems.append(f"原子写代码探测不可用：{type(exc).__name__}")
    acc.check("D2-AC2", f"workbench.v1 与原子写（{phase}）", not problems,
              "schema/自校验/无残留临时文件" if not problems else "；".join(problems))


def _concurrent_advance(acc: Acceptor, path: str,
                        keys: list[str]) -> list[tuple[str, httpx.Response, float]]:
    """同时发两条不同幂等键的 advance（AC3 的单写入者探测）。"""

    def fire(key: str) -> tuple[str, httpx.Response, float]:
        started = time.monotonic()
        response = acc.client.post(path, headers={"Idempotency-Key": key})
        return key, response, time.monotonic() - started

    with ThreadPoolExecutor(max_workers=len(keys)) as pool:
        futures = [pool.submit(fire, key) for key in keys]
        return [future.result() for future in futures]


def _bootstrap_credential(acc: Acceptor) -> None:
    """取启动令牌并挂到客户端默认头上（D3-1 起所有 /api/* 都要凭据）。

    **不走 ``acc.request``**：那会把响应体（含令牌）原样写进报告。令牌是真凭据，
    证据目录要能被随手拷走，凭据一旦进报告就等于泄露。
    """
    started = time.monotonic()
    response = acc.client.get("/api/v1/auth/token")
    elapsed_ms = int((time.monotonic() - started) * 1000)
    acc.record("取启动令牌", "GET", "/api/v1/auth/token",
               status=response.status_code, elapsed_ms=elapsed_ms,
               note="响应体不入报告（含令牌）")
    if response.status_code != 200:
        raise StepError("取启动令牌", f"期望 HTTP 200，实际 {response.status_code}",
                        response.text)
    try:
        token = response.json().get("token")
    except ValueError as exc:
        raise StepError("取启动令牌", "响应不是 JSON", response.text) from exc
    if not isinstance(token, str) or not token:
        raise StepError("取启动令牌", "响应里没有 token", response.text)
    # 头名从实现派生，避免与 auth.TOKEN_HEADER 各写一份（改常量时静默失效）。
    from minimax_h3_prompt.api.auth import TOKEN_HEADER

    acc.client.headers[TOKEN_HEADER] = token
    print("[凭据] 已取到启动令牌并挂到后续请求", flush=True)


def _run_flow(acc: Acceptor) -> None:
    settings = acc.settings
    run_id = settings.run_id

    # --- 凭据先行：健康检查是豁免面，但后面的每个 /api/* 都要（D3-1） --------
    _bootstrap_credential(acc)

    # --- 健康检查与监听范围（AC18 的可自动部分） ---------------------------
    health = acc.request("健康检查", "GET", "/api/v1/health")
    acc.expect(health, 200, "健康检查")
    health_body = acc.json_of(health, "健康检查")
    if health_body.get("status") != "ok":
        raise StepError("健康检查", f"status 不是 ok：{health_body!r}")
    extra_keys = set(health_body) - {"status", "service", "version"}
    acc.check(
        "D2-AC18", "服务只绑回环地址（base_url）", is_loopback(settings.base_url) and not extra_keys,
        f"base_url={settings.base_url}（回环={is_loopback(settings.base_url)}）；"
        f"健康字段={sorted(health_body)}；监听地址由 api/__main__.py 的 HOST=127.0.0.1 决定")

    # --- 建会话 + 幂等重放（AC1 / AC4 建会话部分） -------------------------
    create_key = f"accept-d2-create-{run_id}"
    create_path = "/api/v1/sessions"
    response = acc.request("建会话", "POST", create_path,
                           json=_create_payload(settings),
                           headers={"Idempotency-Key": create_key})
    acc.expect(response, 201, "建会话")
    created = acc.json_of(response, "建会话")
    acc.session_id = str(created.get("session_id") or "")
    if not acc.session_id:
        raise StepError("建会话", "响应里没有 session_id", response.text)
    print(f"[会话] {acc.session_id}", flush=True)

    replay = acc.request("建会话幂等重发", "POST", create_path,
                         json=_create_payload(settings),
                         headers={"Idempotency-Key": create_key})
    acc.expect(replay, 200, "建会话幂等重发")
    replayed_body = acc.json_of(replay, "建会话幂等重发")
    replay_ok = (replayed_body.get("session_id") == acc.session_id
                 and replay.headers.get("Idempotency-Replayed") == "true")
    acc.check("D2-AC4", "同一 Idempotency-Key 重发返回第一次的会话", replay_ok,
              f"session_id={replayed_body.get('session_id')}，"
              f"重放头={replay.headers.get('Idempotency-Replayed')}")

    # --- 可列、可查、URL 编码可用（AC1） -----------------------------------
    listing = acc.request("会话列表", "GET", "/api/v1/sessions?limit=200")
    acc.expect(listing, 200, "会话列表")
    page = acc.json_of(listing, "会话列表")
    listed = [item.get("session_id") for item in page.get("items", [])]
    detail = acc.request("会话详情（建后）", "GET", acc.session_path(acc.session_id))
    acc.expect(detail, 200, "会话详情（建后）")
    detail_body = acc.json_of(detail, "会话详情（建后）")
    ac1_ok = (acc.session_id in listed
              and detail_body.get("session_id") == acc.session_id
              and detail_body.get("status") == "created")
    acc.check("D2-AC1", "会话可建、可列、可查（URL 编码路径可用）", ac1_ok,
              f"列表含会话={acc.session_id in listed}；详情状态={detail_body.get('status')}")
    _check_state_file(acc, "建后")

    # --- 单写入者（AC3）+ 起任务幂等（AC4 任务部分） ------------------------
    jobs_path = acc.session_path(acc.session_id) + "/jobs"
    keys = [f"accept-d2-adv-a-{run_id}", f"accept-d2-adv-b-{run_id}"]
    try:
        results = _concurrent_advance(acc, jobs_path, keys)
    except httpx.HTTPError as exc:
        raise StepError("并发 advance", f"请求失败：{type(exc).__name__}: {exc}") from exc
    accepted: list[tuple[str, dict[str, Any]]] = []
    busy: list[tuple[str, dict[str, Any]]] = []
    for key, response, elapsed in results:
        body = acc._body(response)
        acc.record("并发 advance", "POST", jobs_path, status=response.status_code,
                   elapsed_ms=int(elapsed * 1000), body=body,
                   note=f"key={key}")
        if response.status_code in (200, 202):
            accepted.append((key, acc.json_of(response, "并发 advance")))
        elif response.status_code == 409:
            problem = acc.json_of(response, "并发 advance")
            if problem.get("code") == "session_busy":
                busy.append((key, problem))
    ac3_ok = len(accepted) == 1 and len(busy) == 1
    acc.check("D2-AC3", "同一会话并发 advance 只有一个生效，另一个 409 session_busy",
              ac3_ok, f"受理 {len(accepted)} 条，session_busy {len(busy)} 条")
    if not accepted:
        raise StepError("并发 advance", "两条都没有被受理", "")
    winner_key, winner_job = accepted[0]

    replay_job = acc.request("起任务幂等重发", "POST", jobs_path,
                             headers={"Idempotency-Key": winner_key})
    acc.expect(replay_job, 202, "起任务幂等重发")
    replay_job_body = acc.json_of(replay_job, "起任务幂等重发")
    job_replay_ok = (replay_job_body.get("job_id") == winner_job.get("job_id")
                     and replay_job.headers.get("Idempotency-Replayed") == "true")
    acc.check("D2-AC4", "同一 Idempotency-Key 重发返回第一次的 job id", job_replay_ok,
              f"job_id={replay_job_body.get('job_id')}，"
              f"重放头={replay_job.headers.get('Idempotency-Replayed')}")

    # --- 阶段 1 到人工闸门（AC5） ------------------------------------------
    gated = acc.wait_status({"awaiting_human"}, label="阶段 1 到人工闸门")
    items = gated.get("action_items") or []
    pending = [item for item in items
               if item.get("kind") == "stage1_review" and item.get("status") == "pending"]
    acc.check("D2-AC5", "阶段 1 停在 awaiting_human 且只有一条 stage1_review 待办",
              gated.get("status") == "awaiting_human" and len(pending) == 1,
              f"状态={gated.get('status')}，pending stage1_review={len(pending)}")
    if len(pending) != 1:
        raise StepError("阶段 1 到人工闸门",
                        f"期望唯一一条 pending stage1_review，实际 {len(pending)} 条", "")
    item_id = str(pending[0]["item_id"])
    listed_items = acc.request("待办列表", "GET",
                               acc.session_path(acc.session_id) + "/action-items")
    acc.expect(listed_items, 200, "待办列表")
    listed_item_ids = [item.get("item_id")
                       for item in acc.json_of(listed_items, "待办列表")]
    acc.check("D2-AC5", "待办列表与详情同源", item_id in listed_item_ids,
              f"item_id={item_id} 在列表中={item_id in listed_item_ids}")
    _check_state_file(acc, "阶段 1 后")

    # --- approve 进入阶段 2（AC6）+ 决议幂等 ------------------------------
    resolve_path = (acc.session_path(acc.session_id)
                    + f"/action-items/{item_id}/resolve")
    resolve_key = f"accept-d2-resolve-{run_id}"
    resolve = acc.request("决议通过", "POST", resolve_path,
                          json={"decision": "approve"},
                          headers={"Idempotency-Key": resolve_key})
    acc.expect(resolve, 202, "决议通过")
    resolve_job = acc.json_of(resolve, "决议通过")
    resolve_replay = acc.request("决议幂等重发", "POST", resolve_path,
                                 json={"decision": "approve"},
                                 headers={"Idempotency-Key": resolve_key})
    acc.expect(resolve_replay, 202, "决议幂等重发")
    resolve_replay_body = acc.json_of(resolve_replay, "决议幂等重发")
    acc.check("D2-AC4", "同一 Idempotency-Key 重发决议返回第一次的 job id",
              resolve_replay_body.get("job_id") == resolve_job.get("job_id")
              and resolve_replay.headers.get("Idempotency-Replayed") == "true",
              f"job_id={resolve_replay_body.get('job_id')}")

    completed = acc.wait_status({"completed"}, label="阶段 2 完成")
    succeeded_jobs = [job for job in completed.get("jobs", [])
                      if job.get("status") == "succeeded"]
    acc.check("D2-AC6", "approve 进入阶段 2 并跑到 completed",
              completed.get("status") == "completed" and len(succeeded_jobs) >= 2,
              f"状态={completed.get('status')}，succeeded jobs={len(succeeded_jobs)}")

    # --- 产物与文本内容（AC7） --------------------------------------------
    artifacts_resp = acc.request("产物清单", "GET",
                                 acc.session_path(acc.session_id) + "/artifacts")
    acc.expect(artifacts_resp, 200, "产物清单")
    artifacts = acc.json_of(artifacts_resp, "产物清单")
    acc.artifacts = artifacts if isinstance(artifacts, list) else []
    prompt_info = next(
        (item for item in acc.artifacts if item.get("name") == "video-prompt.md"), None)
    if prompt_info is None:
        raise StepError("产物清单", "清单里没有 video-prompt.md", artifacts_resp.text)
    content_path = ("/api/v1/artifacts/"
                    + quote(str(prompt_info["artifact_id"]), safe="/")
                    + "/content")
    content = acc.request("读取 video-prompt.md", "GET", content_path)
    acc.expect(content, 200, "读取 video-prompt.md")
    text = content.text
    if not text.strip():
        raise StepError("读取 video-prompt.md", "内容为空", text)
    actual_bytes = len(text.encode("utf-8"))
    if actual_bytes != prompt_info.get("bytes"):
        raise StepError("读取 video-prompt.md",
                        f"字节数不符：清单 {prompt_info.get('bytes')}，实际 {actual_bytes}",
                        text)
    acc.artifact_text_bytes = actual_bytes
    (acc.settings.out_dir / "video-prompt.md").write_text(text, encoding="utf-8")
    if acc.settings.sessions_root is not None:
        acc.artifact_text_path = str(
            acc.settings.sessions_root / Path(acc.session_id) / "video-prompt.md")
    acc.check("D2-AC7", "completed 且产物含 video-prompt.md、content 能取到全文",
              True, f"artifact_id={prompt_info.get('artifact_id')}，{actual_bytes} 字节")

    # --- 事件流（AC11） ----------------------------------------------------
    acc.read_events()
    problems = event_seq_problems(acc.events)
    types = [event.get("type") for event in acc.events]
    required = {"session.created", "job.started", "job.progress", "job.succeeded",
                "action_item.created", "action_item.resolved", "artifact.created"}
    missing = sorted(required - set(types))
    detail_now = acc.request("会话详情（完成后）", "GET", acc.session_path(acc.session_id))
    acc.expect(detail_now, 200, "会话详情（完成后）")
    detail_body = acc.json_of(detail_now, "会话详情（完成后）")
    tail = acc.events[-1]["seq"] if acc.events else 0
    reconcile_ok = detail_body.get("last_event_seq") == tail
    acc.check("D2-AC11", "事件 seq 严格单调、无跳号，快照 last_event_seq 可对账",
              not problems and not missing and reconcile_ok,
              f"共 {len(acc.events)} 条，seq 范围=1..{tail}，"
              f"序号问题={problems or '无'}，缺类型={missing or '无'}，"
              f"last_event_seq={detail_body.get('last_event_seq')}")

    tail_ids = acc.probe_sse_tail()
    expected_tail = [event["seq"] for event in acc.events[-2:]]
    acc.check("D2-AC10", "SSE 尾部按 Last-Event-ID 补发", None,
              f"期望尾部 {expected_tail}，探测收到 {tail_ids or '无'}；"
              "完整断线重连语义仍以 tests/test_api_events.py 为准")

    # --- 错误契约（AC13） --------------------------------------------------
    missing_session = acc.request(
        "错误契约（会话不存在）", "GET", "/api/v1/sessions/不存在/GEN999")
    acc.assert_problem(missing_session, 404, "session_not_found", "错误契约（会话不存在）")
    bad_create = acc.request("错误契约（请求体非法）", "POST", "/api/v1/sessions", json={})
    acc.assert_problem(bad_create, 422, None, "错误契约（请求体非法）")
    acc.check("D2-AC13", "错误一律 problem+json 且带稳定错误码", True,
              "404 session_not_found；422 校验错误；错误体无密钥字段")

    # --- 历史会话只读（AC12） ----------------------------------------------
    legacy = [item for item in page.get("items", []) if item.get("legacy")]
    if not legacy:
        acc.check("D2-AC12", "历史会话列表可见、可查、起 job 回 409 session_legacy", None,
                  "本次 sessions_root 里没有 legacy 会话，未观察到")
    else:
        legacy_id = str(legacy[0]["session_id"])
        legacy_detail = acc.request("历史会话详情", "GET", acc.session_path(legacy_id))
        acc.expect(legacy_detail, 200, "历史会话详情")
        legacy_job = acc.request(
            "历史会话起任务", "POST", acc.session_path(legacy_id) + "/jobs",
            headers={"Idempotency-Key": f"accept-d2-legacy-{run_id}"})
        problem = acc.assert_problem(legacy_job, 409, "session_legacy", "历史会话起任务")
        acc.check("D2-AC12", "历史会话列表可见、可查、起 job 回 409 session_legacy",
                  bool(problem), f"legacy={legacy_id}，列表共 {len(legacy)} 条")

    # --- 真实跑本身即 AC19 的证据；评论发布由协调会话完成 ------------------
    acc.check("D2-AC19", "真实 base 短视频一次 headless 跑通", True,
              "本报告即真实跑证据；证据评论发布到 #214 由协调会话完成")


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------

def _write_reports(acc: Acceptor, ok: bool) -> Path:
    settings = acc.settings
    settings.out_dir.mkdir(parents=True, exist_ok=True)
    type_counts: dict[str, int] = {}
    for event in acc.events:
        key = str(event.get("type"))
        type_counts[key] = type_counts.get(key, 0) + 1

    report = {
        "ticket": 221,
        "ok": ok,
        "failure": acc.failure,
        "started_at": acc.started_at,
        "finished_at": _utc_now(),
        "base_url": settings.base_url,
        "command": (
            f"scripts/accept_d2.py --plot {settings.plot!r} --duration {settings.duration}"
            f" --base-url {settings.base_url} --timeout {settings.timeout}"
            f" --sessions-root {settings.sessions_root} --out-dir {settings.out_dir}"),
        "sessions_root": str(settings.sessions_root) if settings.sessions_root else None,
        "session_id": acc.session_id,
        "timings_seconds": acc.timings,
        "steps": acc.steps,
        "checks": acc.checks,
        "artifacts": acc.artifacts,
        "video_prompt": {
            "path": acc.artifact_text_path,
            "bytes": acc.artifact_text_bytes,
            "copy": str(settings.out_dir / "video-prompt.md"),
        },
        "events": {
            "source": acc.events_source,
            "total": len(acc.events),
            "seq_range": [acc.events[0]["seq"], acc.events[-1]["seq"]] if acc.events else [],
            "type_counts": type_counts,
            "head": acc.events[:3],
            "tail": acc.events[-3:],
        },
    }
    (settings.out_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# D2 真实验收报告（#221）",
        "",
        f"- 结果：**{'PASS' if ok else 'FAIL'}**",
        f"- 起止（UTC）：{report['started_at']} → {report['finished_at']}",
        f"- base_url：`{settings.base_url}`",
        f"- sessions_root：`{settings.sessions_root}`",
        f"- 会话：`{acc.session_id or '（未创建）'}`",
        f"- 命令：`{report['command']}`",
        "",
        "## 用时",
        "",
        "| 环节 | 秒 |",
        "|---|---|",
    ]
    for label, seconds in acc.timings.items():
        lines.append(f"| {label} | {seconds} |")
    lines += ["", "## 步骤", "", "| 步骤 | 方法 | 路径 | 状态 | 耗时(ms) |",
              "|---|---|---|---|---|"]
    for step in acc.steps:
        lines.append(f"| {step['step']} | {step['method']} | `{step['path']}` | "
                     f"{step['status']} | {step['elapsed_ms']} |")
    lines += ["", "## D2-AC 逐条结论", "", "| AC | 结论 | 说明 |", "|---|---|---|"]
    for check in acc.checks:
        mark = "✅" if check["ok"] is True else ("❌" if check["ok"] is False else "⏳")
        lines.append(f"| {check['ac']} {check['title']} | {mark} | {check['detail']} |")
    for ac, title, detail in _MANUAL_CHECKS:
        if not any(check["ac"] == ac for check in acc.checks):
            lines.append(f"| {ac} {title} | ⏳ | {detail} |")
    if not any(check["ac"] == "D2-AC19" for check in acc.checks):
        lines.append("| D2-AC19 真实 base 短视频一次 headless 跑通 | ❌ | "
                     "流程未走到 completed，见下方「失败」 |")
    lines += ["", "## 产物", ""]
    if acc.artifacts:
        for item in acc.artifacts:
            lines.append(f"- `{item.get('artifact_id')}`（{item.get('kind')}，"
                         f"{item.get('bytes')} 字节）")
    else:
        lines.append("- （未取到产物清单）")
    if acc.artifact_text_path:
        lines.append(f"- video-prompt.md 原路径：`{acc.artifact_text_path}`")
    lines += ["", "## 事件流", "",
              f"- 来源：`{acc.events_source}`",
              f"- 条数：{len(acc.events)}；seq 范围："
              f"{report['events']['seq_range']}",
              f"- 类型计数：`{json.dumps(type_counts, ensure_ascii=False)}`"]
    if acc.events:
        lines += ["", "```json",
                  json.dumps(acc.events[-3:], ensure_ascii=False, indent=2), "```"]
    if acc.failure:
        lines += ["", "## 失败", "", f"- 步骤：{acc.failure.get('step')}",
                  f"- 详情：{acc.failure.get('detail')}", "",
                  "```text", str(acc.failure.get("response_text", ""))[:2000], "```"]
    lines += [
        "",
        "## #214 证据评论清单",
        "",
        "1. 服务启动命令与本脚本命令（见上方「命令」）。",
        "2. UTC 起止时间、health.version、sessions_root、会话标识。",
        "3. video-prompt.md 的原路径与字节数（本目录留了一份副本）。",
        "4. 事件流摘录：上方 seq 范围、类型计数、尾三条。",
        "5. 用时表（建会话 / 阶段 1 / 决议 / 阶段 2 / 总耗时）。",
        "6. D2-AC1..AC19 表格原样贴入；⏳ 项注明由哪个用例或 CI 提供。",
        "7. 未达标项与四项越界确认（上传 / 鉴权 / 其他路线 / 真实模型进 CI）逐条写「未做」。",
        "",
    ]
    (settings.out_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")
    return settings.out_dir


def run_acceptance(client: httpx.Client, settings: Settings) -> int:
    """跑完整条验收；返回进程退出码（0 = 全部自动核对通过）。"""
    settings.out_dir.mkdir(parents=True, exist_ok=True)
    acc = Acceptor(client, settings)
    started = time.monotonic()
    try:
        _run_flow(acc)
    except StepError as exc:
        acc.failure = {"step": exc.step, "detail": exc.detail,
                       "response_text": exc.response_text}
        print(f"[失败] {exc}", file=sys.stderr, flush=True)
        if exc.response_text:
            print("最后响应：", file=sys.stderr, flush=True)
            print(exc.response_text[:4000], file=sys.stderr, flush=True)
    except Exception as exc:  # noqa: BLE001 - 驱动器自身出错也要落报告，不能只剩堆栈
        import traceback

        trace = traceback.format_exc()
        acc.failure = {"step": "未预期异常",
                       "detail": f"{type(exc).__name__}: {exc}",
                       "response_text": trace[-4000:]}
        print(trace, file=sys.stderr, flush=True)
    acc.timings["总耗时"] = round(time.monotonic() - started, 3)
    ok = acc.failure is None and all(check["ok"] is not False for check in acc.checks)
    out_dir = _write_reports(acc, ok)
    print(f"\n报告：{out_dir / 'report.md'}", flush=True)
    print(f"机读：{out_dir / 'report.json'}", flush=True)
    return 0 if ok else 1


def _default_sessions_root() -> Path | None:
    try:
        from minimax_h3_prompt.config import config

        return Path(config.sessions_root)
    except Exception:  # noqa: BLE001 - 只影响文件级核对，命令行可显式指定
        return None


def _parse_args(argv: list[str] | None) -> Settings:
    parser = argparse.ArgumentParser(description="D2 真实跑验收驱动器（#221）")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=float, default=1800.0,
                        help="单步等待秒数（轮询到目标状态的上限）")
    parser.add_argument("--out-dir", default=None)
    parser.add_argument("--plot", default=DEFAULT_PLOT)
    parser.add_argument("--duration", type=float, default=4.0)
    parser.add_argument("--sessions-root", default=None,
                        help="默认取项目配置的 sessions_root；读不到时文件级核对标为未验证")
    args = parser.parse_args(argv)
    out_dir = Path(args.out_dir) if args.out_dir else (
        ROOT / "output" / "accept-d2" / _stamp())
    sessions_root = (
        Path(args.sessions_root) if args.sessions_root else _default_sessions_root())
    return Settings(
        base_url=args.base_url,
        timeout=args.timeout,
        out_dir=out_dir,
        plot=args.plot,
        duration=args.duration,
        sessions_root=sessions_root,
    )


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台默认 cp936：不切 UTF-8，验收过程里的中文进度会变乱码。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    settings = _parse_args(argv)
    # 不设总超时：阶段 1/2 的真实模型耗时就是验收对象；每一步用 --timeout 兜底。
    with httpx.Client(base_url=settings.base_url,
                      timeout=httpx.Timeout(60.0)) as client:
        return run_acceptance(client, settings)


if __name__ == "__main__":
    sys.exit(main())
