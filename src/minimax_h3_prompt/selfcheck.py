"""冻结产物自检（D5-6 / #241）：不建窗口，验证「这个包能不能跑」。

三步（顺序即依赖）：**冻结环境自检 → ffmpeg → 桩管线阶段 1/2**。每一步都只写
报告行，失败记 FAIL；最终退出码 0/1 是 CI 的判据，报告文件是给人看的现场。

**为什么不在 ``desktop.py`` 里加这个开关**：``--self-check`` 面向的是打包产物与 CI
（``MINIMAX_FAKE_PIPELINE=1`` 的桩管线），不是桌面外壳的公开命令行；它住在打包
入口 ``scripts/exe_entry.py`` 的分派里，业务外壳（#240）保持原样。

**为什么走 HTTP 而不是直接调 JobRunner**：冻结包要证明的是「打进去的东西真能跑」，
而服务入口、鉴权、静态托管、阶段 1/2 的懒导入全在真实 HTTP 路径后面。自检在进程内
起同一个服务（随机端口、临时数据根），再用标准库 urllib 驱动——不额外引入 httpx。
"""
from __future__ import annotations

import argparse
import base64
import importlib.metadata
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Sequence

from . import paths
from .api import __main__ as entry
from .api.fake_pipeline import FAKE_PIPELINE_ENV
from .bootstrap import CONFIG_TEMPLATE_NAMES

PACKAGE_NAME = "minimax-h3-prompt"
# 首次启动必须能复制出来的四份配置来自 ``bootstrap.CONFIG_TEMPLATE_NAMES``（见顶部
# import）：它只依赖 ``shutil`` / ``pathlib`` / ``paths``，为读一个名字而复制一份同形
# 元组才是真风险（#243 复审）。包内非 py 资源的**唯一清单**同理：打包流水线
# （``scripts/build_exe.py``）与这里的冻结自检都读它，脚本侧原先抄了一份同形字面量，
# 新增资源目录时必然漏改一处（#243 复审）。
PACKAGE_DATA_DIRS: tuple[str, ...] = ("prompts", "knowledge", "references")

PIPELINE_TIMEOUT_S = 180.0
POLL_INTERVAL_S = 0.05

# 1x1 PNG（首/尾帧用不同内容，避免被素材去重成同一份）。内容与
# ``tests/test_api_fake_pipeline.py`` 的夹具同源，是真正的可解码图片。
PNG_FIRST = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
PNG_LAST = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
    "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


class SelfCheckError(RuntimeError):
    """自检中可预期的一步失败（转成 FAIL 行，不裸抛 traceback）。"""


# ---------------------------------------------------------------- 冻结环境与资源

def static_checks(resource_root: Path | None = None) -> list[tuple[bool, str]]:
    """冻结形态、只读资源与配置模板是否都在包里。

    返回 ``(是否通过, 说明)`` 列表；**不抛异常**——自检要把所有缺项一次列全，
    而不是修一个再撞下一个。
    """
    root = Path(resource_root) if resource_root is not None else paths.resource_root()
    checks: list[tuple[bool, str]] = []
    checks.append((paths.is_frozen(), "冻结形态：sys.frozen 已设置"
                   if paths.is_frozen() else "非冻结形态：sys.frozen 未设置"))
    checks.append((root.is_dir(), f"只读资源根：{root}"))
    index = root / "frontend" / "dist" / "index.html"
    checks.append((index.is_file(), f"前端构建产物：{index}"))
    for name in CONFIG_TEMPLATE_NAMES:
        path = root / "config" / name
        checks.append((path.is_file(), f"配置模板：{path}"))
    for name in PACKAGE_DATA_DIRS:
        path = root / "minimax_h3_prompt" / name
        checks.append((path.is_dir(), f"包内资源：{path}"))
    return checks


def version_check() -> tuple[bool, str]:
    """版本自报：``api/app.py`` 的 ``_resolve_version`` 靠这份 dist-info 启动。"""
    try:
        version = importlib.metadata.version(PACKAGE_NAME)
    except importlib.metadata.PackageNotFoundError:
        return False, f"版本元数据缺失：{PACKAGE_NAME} 的 dist-info 没进包"
    return True, f"版本自报：{PACKAGE_NAME}={version}"


# ----------------------------------------------------------------------- ffmpeg

def ffmpeg_check(run=subprocess.run) -> tuple[bool, str]:
    """冻结后真调一次 ffmpeg：二进制在包里、且能执行。"""
    from .tools.seam_audit import find_ffmpeg

    try:
        ffmpeg = find_ffmpeg()
    except RuntimeError as exc:
        return False, f"ffmpeg：{exc}"
    result = run([str(ffmpeg), "-version"], capture_output=True, text=True)
    if result.returncode != 0:
        return False, f"ffmpeg 不能执行（退出码 {result.returncode}）：{ffmpeg}"
    first = (result.stdout or "").strip().splitlines()[:1]
    return True, f"ffmpeg：{ffmpeg}；{first[0] if first else '（无输出）'}"


# ------------------------------------------------------------------- HTTP 驱动

def request_url(base_url: str, path: str) -> str:
    """拼请求 URL：路径段做百分号编码（``/`` 保留）。

    会话 id 是「主题 slug/代次」，slug 来自用户输入的主题，可能是中文；HTTP 请求行
    只收 ASCII，不编码就抛 ``'ascii' codec can't encode``（2026-11-12 实跑抓到）。
    """
    return base_url.rstrip("/") + urllib.parse.quote(path, safe="/")


def _http(
    base_url: str, method: str, path: str, *, token: str,
    body: bytes | None = None, content_type: str | None = None,
    timeout: float = 30.0,
) -> tuple[int, bytes]:
    request = urllib.request.Request(request_url(base_url, path), data=body, method=method)
    if content_type:
        request.add_header("Content-Type", content_type)
    request.add_header("X-Workbench-Token", token)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _json(status: int, body: bytes, what: str):
    if status >= 500:
        raise SelfCheckError(f"{what}：HTTP {status} {body[:300]!r}")
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise SelfCheckError(f"{what}：响应不是 JSON（HTTP {status}）：{body[:300]!r}") from exc


def _expect(status: int, expected: int, what: str, body: bytes = b"") -> None:
    if status != expected:
        raise SelfCheckError(f"{what}：期望 HTTP {expected}，得到 {status} {body[:300]!r}")


def _wait_job(base_url: str, token: str, session_id: str, job_id: str,
              timeout: float = PIPELINE_TIMEOUT_S) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status, body = _http(
            base_url, "GET", f"/api/v1/sessions/{session_id}/jobs/{job_id}", token=token)
        _expect(status, 200, "查询任务", body)
        info = _json(status, body, "查询任务")
        if info.get("status") != "running":
            return info
        time.sleep(POLL_INTERVAL_S)
    raise SelfCheckError(f"任务 {job_id} 没有在 {timeout:g}s 内结束")


def _upload_png(base_url: str, token: str, session_id: str, filename: str,
                data: bytes) -> str:
    boundary = "----h3selfcheckboundary"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: image/png\r\n\r\n"
    ).encode("ascii") + data + f"\r\n--{boundary}--\r\n".encode("ascii")
    status, response = _http(
        base_url, "POST", f"/api/v1/sessions/{session_id}/assets", token=token,
        body=body, content_type=f"multipart/form-data; boundary={boundary}")
    _expect(status, 201, f"上传 {filename}", response)
    return str(_json(status, response, f"上传 {filename}")["asset_id"])


def run_fake_pipeline(base_url: str, token: str,
                      timeout: float = PIPELINE_TIMEOUT_S) -> list[str]:
    """桩管线的阶段 1 → 人工闸门 → 交帧 → 阶段 2，全程真实 HTTP。

    返回逐步说明（进报告）；任何一步不符合预期抛 :class:`SelfCheckError`。
    """
    steps: list[str] = []
    status, body = _http(base_url, "GET", "/api/v1/health", token=token)
    _expect(status, 200, "健康检查", body)
    steps.append("健康检查：200")

    # 首页是冻结包里的前端产物（静态托管与 SPA 兜底都要走通）。
    status, body = _http(base_url, "GET", "/", token=token)
    _expect(status, 200, "首页", body)
    if b"<html" not in body.lower():
        raise SelfCheckError("首页：响应里没有 HTML（前端产物没被托管）")
    steps.append("首页：200，返回前端 HTML")

    brief = {"mode": "base", "variant": "FL2VA", "duration": 4.0, "style": "写实",
             "language": "Chinese", "plot": "雨夜旧信（自检）"}
    status, body = _http(base_url, "POST", "/api/v1/sessions", token=token,
                         body=json.dumps(brief).encode("utf-8"),
                         content_type="application/json")
    _expect(status, 201, "创建会话", body)
    session_id = _json(status, body, "创建会话")["session_id"]
    steps.append(f"创建会话：{session_id}")

    status, body = _http(base_url, "POST", f"/api/v1/sessions/{session_id}/jobs",
                         token=token)
    _expect(status, 202, "启动阶段 1", body)
    job_id = _json(status, body, "启动阶段 1")["job_id"]
    info = _wait_job(base_url, token, session_id, job_id, timeout)
    if info.get("status") != "succeeded":
        raise SelfCheckError(f"阶段 1 失败：{info}")
    steps.append(f"阶段 1：completed（job {job_id}）")

    status, body = _http(base_url, "GET", f"/api/v1/sessions/{session_id}", token=token)
    _expect(status, 200, "读取会话", body)
    session = _json(status, body, "读取会话")
    if session.get("status") != "awaiting_human":
        raise SelfCheckError(f"阶段 1 后状态异常：{session.get('status')!r}")
    names = {item.get("name") for item in session.get("artifacts") or []}
    for required in ("script.md", "shot-table.md"):
        if required not in names:
            raise SelfCheckError(f"阶段 1 产物缺失：{required}（实际 {sorted(names)}）")
    steps.append("阶段 1 产物：script.md / shot-table.md")

    review = [item for item in session.get("action_items") or []
              if item.get("status") == "pending"][0]
    status, body = _http(
        base_url, "POST",
        f"/api/v1/sessions/{session_id}/action-items/{review['item_id']}/resolve",
        token=token, body=json.dumps({"decision": "approve"}).encode("utf-8"),
        content_type="application/json")
    _expect(status, 202, "通过阶段 1 审查", body)
    steps.append("阶段 1 审查：approve")

    status, body = _http(base_url, "GET", f"/api/v1/sessions/{session_id}", token=token)
    _expect(status, 200, "读取会话", body)
    session = _json(status, body, "读取会话")
    frame_item = [item for item in session.get("action_items") or []
                  if item.get("status") == "pending"][0]
    if frame_item.get("kind") != "frame_submission":
        raise SelfCheckError(f"审查后待办不是交帧：{frame_item.get('kind')!r}")

    assets = {
        "first": _upload_png(base_url, token, session_id, "first.png", PNG_FIRST),
        "last": _upload_png(base_url, token, session_id, "last.png", PNG_LAST),
    }
    status, body = _http(
        base_url, "POST",
        f"/api/v1/sessions/{session_id}/action-items/{frame_item['item_id']}/resolve",
        token=token, body=json.dumps({"frames": assets}).encode("utf-8"),
        content_type="application/json")
    _expect(status, 202, "提交首尾帧", body)
    job_id = _json(status, body, "提交首尾帧")["job_id"]
    info = _wait_job(base_url, token, session_id, job_id, timeout)
    if info.get("status") != "succeeded":
        raise SelfCheckError(f"阶段 2 失败：{info}")
    steps.append(f"阶段 2：completed（job {job_id}）")

    status, body = _http(base_url, "GET", f"/api/v1/sessions/{session_id}", token=token)
    _expect(status, 200, "读取会话", body)
    session = _json(status, body, "读取会话")
    if session.get("status") != "completed":
        raise SelfCheckError(f"阶段 2 后状态异常：{session.get('status')!r}")
    status, body = _http(
        base_url, "GET",
        f"/api/v1/artifacts/{session_id}/video-prompt.md/content", token=token)
    _expect(status, 200, "读取视频提示词", body)
    steps.append("阶段 2 产物：video-prompt.md 可读")
    return steps


# ------------------------------------------------------------------------ 入口

def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="MiniMax-H3-prompt.exe --self-check",
        description="冻结产物自检：冻结环境、ffmpeg、桩管线阶段 1/2；不打开窗口。",
    )
    parser.add_argument("--data-root", default=None,
                        help="自检用的可写数据根（默认建临时目录）")
    parser.add_argument("--report", default=None,
                        help="报告文件路径（默认 <data-root>/selfcheck.log）")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    # 桩开关只认环境变量；自检自己打开它，且只影响本进程。
    os.environ[FAKE_PIPELINE_ENV] = "1"
    data_root = (Path(args.data_root) if args.data_root
                 else Path(tempfile.mkdtemp(prefix="h3-selfcheck-")))
    report_path = Path(args.report) if args.report else data_root / "selfcheck.log"

    lines: list[str] = [f"[selfcheck] 数据根：{data_root}", f"[selfcheck] 报告：{report_path}"]
    passed = True

    def record(ok: bool, message: str) -> None:
        nonlocal passed
        passed = passed and ok
        lines.append(("[OK]   " if ok else "[FAIL] ") + message)

    for ok, message in static_checks():
        record(ok, message)
    record(*version_check())
    record(*ffmpeg_check())

    handle = None
    try:
        handle = entry.start(["--port", "0", "--data-root", str(data_root)])
        # 自检与 app 同进程：令牌直接从策略对象取，不必再走 /auth/token。
        token = getattr(handle.app, "state").local_access.token
        lines.append(f"[OK]   服务已就绪：{handle.base_url}")
        for step in run_fake_pipeline(handle.base_url, token):
            lines.append(f"[OK]   {step}")
    except Exception as exc:  # noqa: BLE001 - 自检用退出码收场，报告给出原因
        record(False, f"桩管线/服务：{exc}")
    finally:
        if handle is not None:
            handle.close()

    lines.append("[selfcheck] " + ("PASS" if passed else "FAIL"))
    text = "\n".join(lines) + "\n"
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(text, encoding="utf-8")
    except OSError as exc:
        print(f"[selfcheck] 报告写不进 {report_path}：{exc}", file=sys.stderr)
    print(text, flush=True)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
