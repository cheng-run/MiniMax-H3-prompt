#!/usr/bin/env python
"""#234 D4 真实浏览器验收驱动器（手动执行，不进 CI、不进 ``check.sh``）。

它驱动 **Playwright 真实浏览器 + 真实 LLM** 走完整条闭环：填表单 → 自动起跑 →
真实审查/修订 → approve → 上传真实图片作首/尾帧 → 真实读图审计 → 阶段 2 →
复制最终提示词。命令、耗时、截图与产物路径落 ``output/accept-d4/<UTC 时间戳>/``，
报告形态沿用 ``accept_d2.py`` / ``accept_d3.py``。

用法（在**主树**里，服务已另开终端以真实模型启动）::

    python -m minimax_h3_prompt.api          # 终端 A：真实 LLM、真实 .env
    PYTHONIOENCODING=utf-8 PYTHONPATH=src \\
        .venv/Scripts/python.exe scripts/accept_d4.py \\
        --plot "雨夜街口，一位撑黑伞的女性走过积水，霓虹反光" \\
        --image output/accept-d3/xxx/photo.jpg \\
        --sessions-root output/sessions

依赖 Playwright for Python（**安装由用户本人执行**，本仓不把它写进 lock）::

    uv add --dev playwright
    uv run playwright install chromium          # 或设 PW_CHANNEL=chrome 复用系统 Chrome

``--self-check`` 只查环境与结构（图片、可写目录、Playwright 是否可导入、服务
``/health``），不起浏览器、不建会话、不调模型，适合在真实验收前先跑一遍。
``--fake`` 是给桩管线服务（``MINIMAX_FAKE_PIPELINE=1``）做脚本冒烟用的：交互完全
相同，只在报告里标明本轮不是真实模型证据。

为什么是独立脚本而不是 pytest：它打真实浏览器、跑真实模型，一条要几十分钟；
塞进 CI 既违背「真实模型不进 CI」（D4-AC15），也没有真实的图片与 ``.env``。
证据里**不出现令牌**（ADR 0033 第 7 条）：只读引导端点的状态码，不记响应体；
所有 URL 都不带凭据（媒体元素靠 ``wb_session`` Cookie）。

#227 证据评论必须包含：

1. 完整命令与服务启动方式（真实模型，不是桩管线）。
2. 环境：UTC 起止、``health.version``、``base_url``、会话标识。
3. 输入：真实图片路径/字节数/sha256、剧情、变体与时长。
4. 每一步的耗时、截图路径与结果。
5. 产物：``video-prompt.md`` 的 artifact_id、落盘路径、字节数，以及复制/下载
   两份正文的一致性核对。
6. D4-AC14 / D4-AC16 结论；帧读图证据（会话文件里的首/尾描述非空、
   ``frame_images[].target`` 指向的 ``assets/`` 文件存在）；其余 AC 指向 CI 的用例
   （报告里逐条标出来）。
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, unquote

import httpx

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_PLOT = "雨夜街口，一位撑黑伞的女性走过积水，霓虹在积水里反光，写实电影感。"
DEFAULT_REVISION = "结局收束更克制：人物停在旧楼门口，抬手关掉门廊的灯，不要回头。"
REVISION_LAYER = "剧情分镜"
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp"})
PLAYWRIGHT_INSTALL_HINT = "uv add --dev playwright && uv run playwright install chromium"
# 会话快照文件名从包里的唯一声明派生；脚本被单独拷走时退回同值字面量（自检不依赖包）。
try:  # pragma: no cover - 分支只在无包环境走到
    from minimax_h3_prompt.session_store import SESSION_FILENAME as SESSION_SNAPSHOT
except Exception:  # noqa: BLE001
    SESSION_SNAPSHOT = "session-state.json"


class AcceptanceError(RuntimeError):
    """一步不达标：记录能记的步骤，最后整体退出码非零。"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise AcceptanceError(message)


@dataclass
class Report:
    facts: dict[str, Any] = field(default_factory=dict)
    steps: list[dict[str, Any]] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    def step(self, name: str, status: str, ms: int, note: str = "") -> None:
        self.steps.append({"name": name, "status": status, "ms": ms, "note": note})
        if status == "failed":
            self.failures.append(f"{name}：{note}")

    def run(self, name: str, action: Callable[[], str]) -> str:
        """跑一步并计时；异常原样抛出，但先把失败记进报告。"""
        started = time.monotonic()
        try:
            note = action()
        except Exception as exc:  # noqa: BLE001 - 失败也要落进证据
            elapsed = int((time.monotonic() - started) * 1000)
            self.step(name, "failed", elapsed, f"{type(exc).__name__}: {exc}")
            raise
        elapsed = int((time.monotonic() - started) * 1000)
        self.step(name, "ok", elapsed, note)
        return note


def find_real_image(root: Path) -> Path | None:
    """默认挑 ``output/`` 下最大的 PNG/JPEG/WebP：真实照片，不是测试夹具。

    排除 ``output/sessions``（那里是会话素材与 E2E 的 1x1 夹具）与
    ``output/accept-d4``（本脚本自己的产物），避免把假图当成真实输入。
    """
    candidates = [
        path
        for path in (root / "output").rglob("*")
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
        and "accept-d4" not in path.parts and "sessions" not in path.parts
    ]
    return max(candidates, key=lambda path: path.stat().st_size, default=None)


def _resolve_sessions_root(raw: str | None) -> Path:
    if raw is not None:
        path = Path(raw)
        return path.resolve() if path.is_absolute() else (ROOT / path).resolve()
    try:
        from minimax_h3_prompt.config import config

        return Path(str(config.sessions_root)).resolve()
    except Exception:  # noqa: BLE001 - 配置读不到不该挡住验收本身
        return (ROOT / "output" / "sessions").resolve()


def _health(client: httpx.Client, base_url: str) -> dict[str, Any]:
    response = client.get(f"{base_url}/api/v1/health")
    _expect(response.status_code == 200, f"/health 返回 {response.status_code}")
    return response.json()


def _session_detail(
    client: httpx.Client, base_url: str, token: str, session_id: str,
) -> dict[str, Any]:
    """取会话快照（令牌只进请求头，不进证据）。"""
    response = client.get(
        f"{base_url}/api/v1/sessions/{quote(session_id, safe='/')}",
        headers={"X-Workbench-Token": token},
    )
    _expect(response.status_code == 200, f"取会话快照返回 {response.status_code}")
    return response.json()


def _frame_read_evidence(sessions_root: Path, session_id: str) -> dict[str, Any]:
    """完成态核对「帧真的进了读图」：会话文件里的描述与帧图路径。

    只在提交了帧的路径上断言（D4 的 FL2VA 闭环总是交首/尾两张图）。判据落在
    会话快照的真实字段上——浏览器页面看起来「完成了」不代表读图发生过。
    """
    snapshot = sessions_root / session_id / SESSION_SNAPSHOT
    _expect(snapshot.is_file(), f"会话文件不存在：{snapshot}")
    raw = json.loads(snapshot.read_text(encoding="utf-8"))
    state = raw.get("stage_state") or {}
    descriptions = [
        entry for entry in state.get("fl2va_frame_descriptions") or []
        if isinstance(entry, dict)
    ]
    by_role = {str(entry.get("role")): entry for entry in descriptions}
    summary: dict[str, str] = {}
    for role in ("first", "last"):
        entry = by_role.get(role)
        _expect(entry is not None, f"读图结果缺少 {role} 帧")
        text = str(entry.get("description") or "").strip()
        _expect(bool(text), f"{role} 帧的读图描述为空")
        summary[role] = text[:120]
    images = [
        entry for entry in state.get("frame_images") or []
        if isinstance(entry, dict)
    ]
    targets: list[str] = []
    for entry in images:
        target = Path(str(entry.get("target") or ""))
        _expect(target.is_file(), f"帧图路径不存在：{target}")
        targets.append(str(target))
    _expect(bool(targets), "会话里没有 frame_images")
    return {"descriptions": summary, "frame_images": targets}


def _shot(page: Any, out_dir: Path, name: str, facts: dict[str, Any]) -> str:
    path = out_dir / f"{name}.png"
    page.screenshot(path=str(path), full_page=True)
    facts.setdefault("screenshots", []).append(str(path))
    return str(path)


def _wait_text(page: Any, text: str, timeout_ms: int, exact: bool = False) -> None:
    page.get_by_text(text, exact=exact).first.wait_for(state="visible", timeout=timeout_ms)


def run_real(args: argparse.Namespace, report: Report, image: Path) -> None:
    """真实闭环：真实 LLM、真实读图、真实浏览器。"""
    from playwright.sync_api import sync_playwright

    base_url = args.base_url.rstrip("/")
    timeout_ms = int(args.timeout * 1000)
    out_dir = Path(report.facts["output_dir"])
    sessions_root = Path(report.facts["sessions_root"])
    last_image = Path(args.image_last).resolve() if args.image_last else image

    report.facts.update({
        "base_url": base_url,
        "plot": args.plot,
        "variant": "FL2VA",
        "duration": args.duration,
        "pipeline": "fake（桩管线冒烟，不是真实模型证据）" if args.fake else "real（真实 LLM）",
        "image": {
            "path": str(image),
            "bytes": image.stat().st_size,
            "sha256": _sha256(image),
        },
    })
    if last_image != image:
        report.facts["image_last"] = {
            "path": str(last_image),
            "bytes": last_image.stat().st_size,
            "sha256": _sha256(last_image),
        }

    with httpx.Client(timeout=httpx.Timeout(60.0)) as client:
        health = _health(client, base_url)
        report.facts["server_version"] = health.get("version")
        token_response = client.get(f"{base_url}/api/v1/auth/token")
        # 只记状态码：响应体是凭据（ADR 0033 第 7 条）。
        _expect(token_response.status_code == 200,
                f"引导令牌返回 {token_response.status_code}")
        token = str(token_response.json().get("token") or "")
        _expect(bool(token), "引导令牌为空")

        with sync_playwright() as playwright:
            channel = os.environ.get("PW_CHANNEL") or None
            browser = playwright.chromium.launch(headless=not args.headed, channel=channel)
            try:
                context = browser.new_context(
                    accept_downloads=True,
                    viewport={"width": 1280, "height": 900},
                )
                context.grant_permissions(
                    ["clipboard-read", "clipboard-write"], origin=base_url,
                )
                page = context.new_page()
                page.set_default_timeout(30_000)

                def create_and_start() -> str:
                    page.goto(f"{base_url}/")
                    page.get_by_role("button", name="新建会话").click()
                    page.locator('textarea[name="plot"]').fill(args.plot)
                    page.locator('select[name="variant"]').select_option("FL2VA")
                    page.locator('input[name="duration"]').fill(str(args.duration))
                    page.get_by_role("button", name="创建并开始").click()
                    page.wait_for_url(re.compile(r"/sessions/.+"), timeout=timeout_ms)
                    _wait_text(page, "等待人工", timeout_ms)
                    return page.url

                session_url = report.run("创建并自动起跑（真实阶段 1 完成）", create_and_start)
                session_id = _session_id_from_url(session_url)
                report.facts["session_id"] = session_id
                report.facts["session_url"] = session_url
                _shot(page, out_dir, "01-awaiting-review", report.facts)

                def review_and_revise() -> str:
                    page.get_by_role("tab", name="审查与待办").click()
                    page.get_by_role("heading", name="阶段 1 中间产物").wait_for(
                        state="visible", timeout=timeout_ms)
                    texts = page.locator(".file-text").all_inner_texts()
                    _expect(len(texts) >= 2 and all(text.strip() for text in texts),
                            "阶段 1 中间产物不足两件或正文为空")
                    _shot(page, out_dir, "02-stage1-review", report.facts)
                    page.locator('select[name="revision-layer"]').select_option(
                        label=REVISION_LAYER)
                    page.locator('textarea[name="revision-text"]').fill(args.revision)
                    page.get_by_role("button", name="提交意见").click()
                    # 修订历史出现＝旧待办已决议；随后新待办回闸门（重跑完成）。
                    history = page.get_by_role("region", name="修订历史")
                    history.get_by_text(args.revision).wait_for(
                        state="visible", timeout=timeout_ms)
                    page.get_by_role("button", name="通过").wait_for(
                        state="visible", timeout=timeout_ms)
                    _shot(page, out_dir, "03-revised-draft", report.facts)
                    return f"修订层次={REVISION_LAYER}；重跑后新待办已回闸门"

                report.run("审查 + 提交一轮真实修订并等待重跑", review_and_revise)

                def approve_and_wait_frames() -> str:
                    page.get_by_role("button", name="通过").click()
                    page.get_by_role("heading", name="提交关键帧").wait_for(
                        state="visible", timeout=timeout_ms)
                    _shot(page, out_dir, "04-frame-submission", report.facts)
                    return "approve 后出现 frame_submission 待办"

                report.run("通过阶段 1（帧链进入帧提交待办）", approve_and_wait_frames)

                def upload_frames() -> str:
                    page.get_by_role("tab", name="素材").click()
                    upload = page.locator("#asset-upload")
                    upload.set_input_files(str(image))
                    page.locator(".asset__meta strong").filter(
                        has_text=image.name).first.wait_for(
                            state="visible", timeout=timeout_ms)
                    if last_image != image:
                        upload.set_input_files(str(last_image))
                        page.locator(".asset__meta strong").filter(
                            has_text=last_image.name).first.wait_for(
                                state="visible", timeout=timeout_ms)
                    _shot(page, out_dir, "05-assets", report.facts)

                    page.get_by_role("tab", name="审查与待办").click()
                    page.get_by_role("heading", name="提交关键帧").wait_for(
                        state="visible", timeout=timeout_ms)
                    page.locator('select[name="first-frame"]').select_option(
                        label=image.name)
                    page.locator('select[name="last-frame"]').select_option(
                        label=last_image.name)
                    page.get_by_role("button", name="提交帧并继续").click()
                    _wait_text(page, "已完成", timeout_ms)
                    _shot(page, out_dir, "06-completed", report.facts)
                    return f"首帧={image.name}；尾帧={last_image.name}；状态=completed"

                report.run("上传真实图片作首/尾帧并跑完真实阶段 2", upload_frames)

                def deliver() -> str:
                    page.get_by_role("tab", name="产物").click()
                    # 判据不依赖正文语言：真实阶段 2 的提示词是英文 H3 格式，桩产出的
                    # 中文标题只是夹具。分组与复制按钮才是两侧共有的结构。
                    deliverable = page.locator('[data-group="deliverable"]')
                    deliverable.get_by_role("heading", name="交付文本").wait_for(
                        state="visible", timeout=timeout_ms)
                    copy_button = deliverable.locator('[data-copy="video-prompt.md"]')
                    copy_button.wait_for(state="visible", timeout=timeout_ms)
                    _shot(page, out_dir, "07-delivery", report.facts)

                    copy_button.click()
                    page.get_by_text("已复制", exact=True).first.wait_for(
                        state="visible", timeout=30_000)
                    clipboard = page.evaluate("navigator.clipboard.readText()")
                    _expect(isinstance(clipboard, str) and len(clipboard.strip()) > 100,
                            "剪贴板里的最终提示词为空或过短")

                    with page.expect_download(timeout=timeout_ms) as download_info:
                        page.locator('[data-download="video-prompt.md"]').click()
                    download = download_info.value
                    saved = out_dir / "video-prompt.md"
                    download.save_as(str(saved))
                    _expect(saved.is_file() and saved.stat().st_size > 0,
                            "下载的产物落盘为空")
                    downloaded = saved.read_text(encoding="utf-8")
                    _expect(
                        downloaded.strip() == clipboard.strip(),
                        "剪贴板正文与下载落盘正文不一致",
                    )
                    (out_dir / "final-prompt-clipboard.md").write_text(
                        clipboard + "\n", encoding="utf-8")
                    return (
                        f"复制 {len(clipboard)} 字符；下载 {saved.stat().st_size} 字节；"
                        "两份正文一致"
                    )

                report.run("打开产物分区：复制并下载最终提示词", deliver)

                detail = _session_detail(client, base_url, token, session_id)
                artifacts = detail.get("artifacts") or []
                artifact = next(
                    (item for item in artifacts
                     if item.get("name") == "video-prompt.md"), None,
                )
                _expect(artifact is not None, "会话快照里没有 video-prompt.md 产物")
                artifact_id = str(artifact.get("artifact_id") or "")
                artifact_path = sessions_root / artifact_id
                report.facts["artifact"] = {
                    "artifact_id": artifact_id,
                    "path": str(artifact_path),
                    "exists": artifact_path.is_file(),
                    "bytes": artifact.get("bytes"),
                }
                _expect(artifact_path.is_file(), "产物登记路径在磁盘上不存在")
                _expect(detail.get("status") == "completed",
                        f"会话终态不是 completed：{detail.get('status')}")
                # 帧证据只在提交了帧的路径断言：首/尾描述非空、frame_images 指向的
                # 素材文件仍在盘上——这是「读图真的发生过」的会话侧物证。
                report.facts["frame_read_evidence"] = _frame_read_evidence(
                    sessions_root, session_id)

                context.close()
            finally:
                browser.close()


def _session_id_from_url(url: str) -> str:
    """浏览器地址里是逐段百分号编码的深链，解回会话标识（含斜杠）。"""
    path = httpx.URL(url).path
    raw = path[len("/sessions/"):] if path.startswith("/sessions/") else path
    return "/".join(unquote(part) for part in raw.split("/"))


def self_check(args: argparse.Namespace) -> int:
    """不起浏览器、不调模型的结构检查：真实验收前先跑一遍。"""
    checks: list[tuple[str, bool, str]] = []
    image = Path(args.image).resolve() if args.image else find_real_image(ROOT)
    image_ok = image is not None and image.is_file()
    image_note = str(image) if image is not None else "output/ 下没有 PNG/JPEG/WebP"
    # 真实读图审计要拿真照片：1x1 的 E2E 夹具能过接口但不构成视觉证据。
    if image_ok and not args.fake and image is not None and image.stat().st_size < 8192:
        image_ok = False
        image_note += "（小于 8 KiB，像测试夹具；请用 --image 指定真实照片）"
    checks.append(("真实图片", image_ok, image_note))
    out_dir = Path(args.out_dir) if args.out_dir else (
        ROOT / "output" / "accept-d4" / _stamp())
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        probe = out_dir / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        writable, note = True, str(out_dir)
    except OSError as exc:  # pragma: no cover - 权限异常的机器
        writable, note = False, f"{out_dir}：{exc}"
    checks.append(("输出目录可写", writable, note))
    checks.append((
        "Playwright for Python",
        importlib.util.find_spec("playwright") is not None,
        PLAYWRIGHT_INSTALL_HINT,
    ))
    try:
        with httpx.Client(timeout=httpx.Timeout(10.0)) as client:
            health = _health(client, args.base_url.rstrip("/"))
        reachable, note = True, f"version={health.get('version')}"
    except Exception as exc:  # noqa: BLE001 - 服务没起也是自检结论
        reachable, note = False, f"{args.base_url}：{exc}"
    checks.append(("服务可达", reachable, note))
    # 服务端是否开桩只能由启动方保证（没有 API 暴露它）；这里至少拦住本进程
    # 与 `--fake` 两条明显的误标。
    fake_env = os.environ.get("MINIMAX_FAKE_PIPELINE", "").strip().lower() in {
        "1", "true", "yes", "on",
    }
    checks.append((
        "真实模型模式",
        not args.fake and not fake_env,
        "本轮标为桩管线冒烟" if args.fake or fake_env
        else "MINIMAX_FAKE_PIPELINE 未在这条命令的环境里开启",
    ))
    ok = True
    for name, passed, note in checks:
        print(f"[self-check] {'PASS' if passed else 'FAIL'}  {name}：{note}")
        # 真实模型模式也参与退出码：桩模式自检通过等于把「非真实证据」放行，
        # 真实验收的前置检查必须在这里失败。
        if not passed:
            ok = False
    return 0 if ok else 2


def _write_report(report: Report, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    facts = report.facts
    step_rows = "\n".join(
        f"| {step['name']} | {step['status']} | {step['ms']} | {step['note']} |"
        for step in report.steps
    )
    screenshots = "\n".join(f"- `{path}`" for path in facts.get("screenshots", []))
    artifact = facts.get("artifact") or {}
    image = facts.get("image") or {}
    frame_evidence = facts.get("frame_read_evidence") or {}
    frame_descriptions = frame_evidence.get("descriptions") or {}
    frame_images = frame_evidence.get("frame_images") or []
    lines = [
        "# D4 真实验收证据（#234 / 父 spec #227）",
        "",
        "## 运行环境",
        "",
        f"- 命令：`{facts.get('command', '')}`",
        f"- base_url：`{facts.get('base_url', '')}`",
        f"- sessions_root：`{facts.get('sessions_root', '')}`",
        f"- 起止（UTC）：{facts.get('started_at', '')} → {facts.get('finished_at', '')}",
        f"- 服务版本：`{facts.get('server_version', '')}`",
        f"- 管线：{facts.get('pipeline', '')}",
        "",
        "## 输入",
        "",
        f"- 剧情：{facts.get('plot', '')}",
        f"- 变体／时长：{facts.get('variant', '')} / {facts.get('duration', '')} 秒",
        f"- 真实图片：`{image.get('path', '')}`（{image.get('bytes', '')} 字节，"
        f"sha256 `{image.get('sha256', '')}`）",
        "",
        "## 产物",
        "",
        f"- 会话：`{facts.get('session_id', '')}`",
        f"- `video-prompt.md`：artifact_id `{artifact.get('artifact_id', '')}`",
        f"- 落盘路径：`{artifact.get('path', '')}`（{artifact.get('bytes', '')} 字节）",
        f"- 复制/下载副本：`{out_dir / 'final-prompt-clipboard.md'}` / "
        f"`{out_dir / 'video-prompt.md'}`",
        "",
        "## 帧读图证据（会话文件）",
        "",
        f"- 首帧描述：{frame_descriptions.get('first', '（缺）')}",
        f"- 尾帧描述：{frame_descriptions.get('last', '（缺）')}",
        "- 帧图：",
        *([f"  - `{path}`" for path in frame_images] or ["  （无）"]),
        "",
        "## 截图",
        "",
        screenshots or "（无）",
        "",
        "## 步骤与耗时",
        "",
        "| 步骤 | 结果 | 毫秒 | 备注 |",
        "|---|---|---|---|",
        step_rows,
        "",
        "## 结论",
        "",
        "D4-AC14（产物查看/复制/下载、完成态无确认闸门）："
        + ("通过。" if not report.failures else "未通过。"),
        "D4-AC16（真实浏览器 + 真实 LLM 完整闭环）："
        + ("通过。" if not report.failures else "未通过。"),
        "",
        "其余 AC 的证据在 CI：桩管线 Playwright 全闭环（`frontend/e2e/delivery.spec.ts`）、"
        "SSE 重连（`frontend/e2e/sessions.spec.ts`）、组件与契约用例；真实 LLM 不进 CI。",
    ]
    if report.failures:
        lines += ["", "未达标项："]
        lines += [f"- {failure}" for failure in report.failures]
    lines.append("")
    (out_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")
    (out_dir / "report.json").write_text(
        json.dumps(
            {**facts, "steps": report.steps, "failures": report.failures},
            ensure_ascii=False, indent=2,
        ) + "\n",
        encoding="utf-8",
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="D4 真实浏览器验收驱动器（#234）")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--timeout", type=float, default=1800.0,
                        help="单步等待真实模型的秒数上限")
    parser.add_argument("--out-dir", default=None)
    parser.add_argument("--plot", default=DEFAULT_PLOT)
    parser.add_argument("--duration", type=int, default=5)
    parser.add_argument("--revision", default=DEFAULT_REVISION)
    parser.add_argument("--image", default=None, help="真实图片（默认挑 output/ 下最大的一张）")
    parser.add_argument("--image-last", default=None,
                        help="尾帧图片（默认与 --image 同一张）")
    parser.add_argument("--sessions-root", default=None,
                        help="默认取项目配置的 sessions_root，读不到时用 output/sessions")
    parser.add_argument("--headed", action="store_true", help="显示浏览器窗口")
    parser.add_argument("--fake", action="store_true",
                        help="对着 MINIMAX_FAKE_PIPELINE=1 的服务做脚本冒烟（非真实证据）")
    parser.add_argument("--self-check", action="store_true",
                        help="只查结构与环境，不起浏览器、不调模型")
    parser.add_argument("--command", default="", help="报告里记录的实际命令行")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass
    args = parse_args(argv)
    if args.self_check:
        code = self_check(args)
        print(f"[self-check] {'PASS' if code == 0 else 'FAIL'}")
        return code

    out_dir = (Path(args.out_dir) if args.out_dir
               else ROOT / "output" / "accept-d4" / _stamp())
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    report = Report()
    report.facts["output_dir"] = str(out_dir)
    report.facts["sessions_root"] = str(_resolve_sessions_root(args.sessions_root))
    report.facts["started_at"] = _utc_now()
    report.facts["command"] = args.command or " ".join(sys.argv)
    try:
        image = Path(args.image).resolve() if args.image else find_real_image(ROOT)
        _expect(image is not None and image.is_file(),
                f"找不到真实图片；用 --image 指定（安装提示：{PLAYWRIGHT_INSTALL_HINT}）")
        _expect(importlib.util.find_spec("playwright") is not None,
                f"Playwright for Python 未安装：{PLAYWRIGHT_INSTALL_HINT}")
        run_real(args, report, image)
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
