#!/usr/bin/env python
"""#251 D6 真实长片分段陪跑验收驱动器（手动执行，不进 CI、不进 ``check.sh``）。

它驱动**真实 LLM 服务 + 真实 ComfyUI 段视频 + 真实读图 + 真实拼接**跑一条 18 秒长片。
段视频由用户在 ComfyUI 里按每段规划秒数生成——系统不生成视频是既有边界
（``main.py``「项目不会自动运行 ComfyUI」），本驱动器只负责把提示词交出去、把视频收回来。

两阶段（证据落 ``output/accept-d6/<UTC 时间戳>/``）::

    # 阶段 A：起/接真实服务 → 建 18 秒会话 → 阶段 1 → 审查通过 →
    #         FL2VA 交开场帧（frame_submission）→ 阶段 2 → 分段规划 →
    #         写出第 1 段提示词与 handoff.md
    PYTHONIOENCODING=utf-8 PYTHONPATH=src .venv/Scripts/python.exe \\
        scripts/accept_d6.py --prepare

    # 用户把每段视频保存为 segment-NN.mp4 放进视频目录，然后：
    PYTHONIOENCODING=utf-8 PYTHONPATH=src .venv/Scripts/python.exe \\
        scripts/accept_d6.py --submit-dir output/accept-d6/<戳>/videos

段提示词是**逐段增量**写的（第 N+1 段要拿第 N 段尾帧当首帧锚），所以 ``--submit-dir``
每跑一次只推进到「下一段待生成」为止：交一段、等校验、拿到下一段提示词、再交给用户。
全部段通过后服务端自动拼接；此时驱动器断言 ``final.mp4`` 存在并且 content 端点
200/206 可读、会话 ``completed``。

为什么是独立脚本而不是 pytest：它花真实 LLM 的钱、等用户手工生成视频，一条以小时计，
塞进 CI 既违背「真实模型不进 CI」，也没有真实的 ``.env`` 与 ComfyUI 产物。

硬性前置（工程规范「验收驱动器必须断言配置来源」，2026-11-12 事故）：
**起被测程序之前**先打印并断言生效配置——文件绝对路径、``sha256``、端点/模型；
路径不是本仓 ``config/agent.yaml``（或被 ``--expect-*`` 指到别处）立即失败。
这条是为了拦住「把打包内置模板当成用户配置、白跑一轮真实调用」那类错误。
``MINIMAX_FAKE_PIPELINE=1`` 或 ``--fake`` 一律 FAIL：桩管线不是真实验收证据。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import httpx

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_OUTPUT_ROOT = ROOT / "output" / "accept-d6"
FAKE_PIPELINE_ENV = "MINIMAX_FAKE_PIPELINE"
# 票面「2026-11-12 用户裁定」：真实档取 18 秒；段长由规划器在 4–10 秒内决定。
DEFAULT_DURATION = 18.0
DEFAULT_PLOT = (
    "雨夜旧信：老邮差把最后一封信送到拆了一半的旧楼，门口的女人接过信，"
    "门廊的灯一盏一盏暗下去，写实电影感。"
)
DEFAULT_VARIANT = "FL2VA"
DEFAULT_STYLE = "Cinematic"
DEFAULT_LANGUAGE = "Chinese"
# 段视频的命名约定由 handoff 写死（两位序号与段号同号）；大小写不敏感，
# 因为 Windows 资源管理器里另存为大写很常见。
SEGMENT_VIDEO_RE = re.compile(r"^segment-(\d{2})\.mp4$", re.IGNORECASE)
# 服务端的桩开关没有 HTTP 入口，`/health` 也不回报它——外部起的桩服务从协议上
# 无法观测。桩管线的产物正文自带这个标记（`api/fake_pipeline.py` 的固定稿），
# 所以读到产物时核对一次：这是对「外部服务开了桩」唯一可证的防线。
FAKE_TEXT_MARKER = "桩管线"
POLL_INTERVAL_S = 2.0
HEARTBEAT_S = 30.0
STARTUP_TIMEOUT_S = 120.0
EXIT_COMPLETED = 0
EXIT_FAILED = 1
EXIT_SELF_CHECK_FAILED = 2
# 「等用户生成下一段视频」不是失败：prepare 阶段本来就停在这里，submit 阶段也可能
# 只推进一段就交回用户。单独一档，调用方不必从文案里猜。
EXIT_WAITING_USER = 3
# 与本仓 step_machine 的同名状态一致：failed 是「可以点重试」的那种失败。
STATUS_FAILED = "failed"


class AcceptanceError(RuntimeError):
    """一步不达标：记录能记的步骤，最后整体退出码非零。"""


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise AcceptanceError(message)


def _require(value: Any, message: str) -> Any:
    """``_expect`` 的取值版：顺手让类型检查知道后续不再是 None。"""
    if value is None:
        raise AcceptanceError(message)
    return value


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


def _normalize_base_url(value: str) -> str:
    return value.strip().rstrip("/")


def _problem_text(response: httpx.Response) -> str:
    """只取 problem+json 的可读原因；响应体可能带内部信息，截断后进证据。"""
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001 - 不是 JSON 就退回状态码
        return f"HTTP {response.status_code}"
    if isinstance(payload, dict):
        for key in ("detail", "title", "message"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:500]
    return f"HTTP {response.status_code}"


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
        """跑一步并计时；异常原样抛出，但先把失败记进报告。

        每步开始就打印（``flush=True``）：真实 LLM 一轮可达几十分钟，输出重定向时块
        缓冲会让整段静默，用户没法判断是在跑还是卡住（accept_d4 的同一条实测教训）。
        """
        started = time.monotonic()
        print(f"[step] 开始：{name}", flush=True)
        try:
            note = action()
        except Exception as exc:  # noqa: BLE001 - 失败也要落进证据
            elapsed = int((time.monotonic() - started) * 1000)
            self.step(name, "failed", elapsed, f"{type(exc).__name__}: {exc}")
            print(f"[step] 失败：{name}（{elapsed} ms）", flush=True)
            raise
        elapsed = int((time.monotonic() - started) * 1000)
        self.step(name, "ok", elapsed, note)
        print(f"[step] 成功：{name}（{elapsed} ms）", flush=True)
        return note


# ---------------------------------------------------------------- 配置来源

def config_source_facts() -> dict[str, Any]:
    """生效配置的来源事实：绝对路径、sha256、端点/模型（含环境变量覆盖后的值）。

    导入放在函数里：``--self-check`` 也要能跑，而包不可导入时要给出可读的失败，
    不在模块 import 期就炸掉整个脚本。
    """
    from minimax_h3_prompt import config as config_module
    from minimax_h3_prompt import paths

    path = Path(str(config_module.CONFIG_PATH)).resolve()
    runtime = config_module.config
    primary = runtime.primary_model
    vision = runtime.vision_model
    return {
        "config_path": str(path),
        "config_bytes": path.stat().st_size if path.is_file() else 0,
        "config_sha256": _sha256(path) if path.is_file() else "",
        "env_path": str(paths.env_file_path()),
        "primary": {
            "provider": primary.provider,
            "model": primary.model,
            "base_url": primary.base_url or "",
            "api_key_env": primary.api_key_env,
        },
        "vision": {
            "model": vision.model,
            "base_url": vision.base_url or "",
            "api_key_env": vision.api_key_env,
        },
        "missing_keys": list(runtime.missing_api_keys()),
    }


def config_source_problems(
    facts: dict[str, Any], *, expected_path: Path | None,
    expected_sha256: str | None, expected_model: str | None,
    expected_base_url: str | None,
) -> list[str]:
    """配置来源比对：任何一项不符都要点名（不符就停，别让后面的钱白花）。"""
    problems: list[str] = []
    actual_path = str(facts.get("config_path") or "")
    if expected_path is not None and (
            Path(actual_path).resolve() != Path(expected_path).resolve()):
        problems.append(f"配置文件路径不匹配：实际 {actual_path}，期望 {expected_path}")
    actual_sha = str(facts.get("config_sha256") or "")
    if not actual_sha:
        problems.append(f"读不到配置文件的 sha256：{actual_path}")
    elif expected_sha256 and actual_sha != expected_sha256:
        problems.append(
            f"配置文件 sha256 不匹配：实际 {actual_sha}，期望 {expected_sha256}")
    primary = facts.get("primary") or {}
    if expected_model and str(primary.get("model") or "") != expected_model:
        problems.append(
            f"主模型不匹配：实际 {primary.get('model')}，期望 {expected_model}")
    actual_base = _normalize_base_url(str(primary.get("base_url") or ""))
    if expected_base_url and actual_base != _normalize_base_url(expected_base_url):
        problems.append(
            f"模型端点不匹配：实际 {actual_base}，期望 {_normalize_base_url(expected_base_url)}")
    return problems


def health_config_problems(
    config_path: str, health: dict[str, Any],
    expected_base_url: str | None = None,
) -> list[str]:
    """运行中的服务必须读同一份配置，并且密钥已解析——否则这一轮不是真实证据。"""
    problems: list[str] = []
    reported = str(health.get("config_path") or "")
    if not reported:
        problems.append("健康接口没有回报 config_path：无法确认服务用的是哪份配置")
    elif Path(reported).resolve() != Path(config_path).resolve():
        problems.append(
            f"服务生效的 config_path 与本地配置不一致：实际 {reported}，期望 {config_path}")
    missing = [str(name) for name in (health.get("missing_keys") or [])]
    if missing:
        problems.append(f"服务仍缺密钥：{', '.join(missing)}（真实 LLM 跑不了）")
    if expected_base_url:
        actual = _normalize_base_url(str(health.get("base_url") or ""))
        if actual and actual != _normalize_base_url(expected_base_url):
            problems.append(
                f"服务回报的 base_url 与连接地址不一致：实际 {actual}，"
                f"期望 {_normalize_base_url(expected_base_url)}")
    return problems


def _expected_config_path(args: argparse.Namespace) -> Path:
    if args.expect_config:
        return Path(args.expect_config).resolve()
    # 开发形态的默认判据：被测程序读的必须是**本仓**的配置。accept_d5 的事故就是
    # 把打包内置模板（旧端点）当成了用户配置。
    return (ROOT / "config" / "agent.yaml").resolve()


def _assert_config_source(args: argparse.Namespace, report: Report) -> dict[str, Any]:
    facts = config_source_facts()
    report.facts["config_source"] = facts
    primary = facts.get("primary") or {}
    vision = facts.get("vision") or {}
    print("[config] 配置来源：", flush=True)
    print(f"[config]   文件：{facts.get('config_path')}"
          f"（{facts.get('config_bytes')} 字节，sha256 {facts.get('config_sha256')}）",
          flush=True)
    print(f"[config]   密钥文件：{facts.get('env_path')}"
          f"；缺失变量={facts.get('missing_keys')}", flush=True)
    print(f"[config]   主模型：provider={primary.get('provider')} "
          f"model={primary.get('model')} base_url={primary.get('base_url')}", flush=True)
    print(f"[config]   视觉模型：model={vision.get('model')} "
          f"base_url={vision.get('base_url')}", flush=True)
    problems = config_source_problems(
        facts,
        expected_path=_expected_config_path(args),
        expected_sha256=args.expect_config_sha256,
        expected_model=args.expect_model,
        expected_base_url=args.expect_base_url,
    )
    _expect(not problems, "配置来源不符，拒绝启动被测程序：" + "；".join(problems))
    return facts


# ---------------------------------------------------------------- 纯解析

def segment_rows(detail: dict[str, Any]) -> list[dict[str, Any]]:
    """会话详情里的段列表 → 报告用的行（含规划秒数与产物/素材引用）。"""
    rows: list[dict[str, Any]] = []
    for segment in detail.get("segments") or []:
        index = int(segment.get("index") or 0)
        start = int(segment.get("start_s") or 0)
        end = int(segment.get("end_s") or 0)
        rows.append({
            "index": index,
            "start_s": start,
            "end_s": end,
            "planned_seconds": end - start,
            "status": str(segment.get("status") or ""),
            "prompt_artifact_id": str(segment.get("prompt_artifact_id") or ""),
            "video_asset_id": str(segment.get("video_asset_id") or ""),
        })
    return sorted(rows, key=lambda row: row["index"])


def pending_segment_item(detail: dict[str, Any]) -> dict[str, Any] | None:
    for item in detail.get("action_items") or []:
        if (item.get("kind") == "segment_video"
                and item.get("status") == "pending"):
            return item
    return None


def pending_review_item(detail: dict[str, Any]) -> dict[str, Any] | None:
    for item in detail.get("action_items") or []:
        if (item.get("kind") == "stage1_review"
                and item.get("status") == "pending"):
            return item
    return None


def pending_frame_item(detail: dict[str, Any]) -> dict[str, Any] | None:
    """FL2VA 在审查通过后多一道帧提交待办（D4 §9）：交开场帧才继续阶段 2。"""
    for item in detail.get("action_items") or []:
        if (item.get("kind") == "frame_submission"
                and item.get("status") == "pending"):
            return item
    return None


def collect_segment_videos(directory: Path) -> dict[int, Path]:
    """``segment-NN.mp4`` → 段号。名字写错要响亮失败，不能静默当成「缺这一段」。

    ``segment-1.mp4`` 静默忽略的话，报告会说「缺第 1 段」，用户去生成新视频，
    真正的病因（文件名）永远不会被看见。
    """
    _expect(directory.is_dir(), f"段视频目录不存在：{directory}")
    videos: dict[int, Path] = {}
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.suffix.lower() != ".mp4":
            continue
        match = SEGMENT_VIDEO_RE.match(path.name)
        if match is None:
            if path.name.lower().startswith("segment"):
                raise AcceptanceError(
                    f"段视频文件名不合规：{path.name}（应为 segment-NN.mp4，两位序号）")
            continue
        index = int(match.group(1))
        if index in videos:
            raise AcceptanceError(
                f"第 {index} 段有多个视频：{videos[index].name} 与 {path.name}")
        videos[index] = path
    return videos


def latest_job_failure(
    sessions_root: Path, session_id: str, job_id: str | None = None,
) -> dict[str, Any] | None:
    """事件流里的最后一条 ``job.failed``：JobSummary 只带 error_code，detail 在事件里。"""
    path = Path(sessions_root) / Path(session_id) / "events.jsonl"
    if not path.is_file():
        return None
    failure: dict[str, Any] | None = None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("type") != "job.failed":
            continue
        if job_id and str(entry.get("job_id") or "") != job_id:
            continue
        payload = entry.get("payload") or {}
        failure = {
            "code": str(payload.get("code") or ""),
            "detail": str(payload.get("detail") or ""),
        }
    return failure


def _reject_fake_text(text: str, where: str) -> None:
    """产物正文里出现桩标记＝服务端跑的是桩管线，这轮不能当真实证据。"""
    if FAKE_TEXT_MARKER in text:
        raise AcceptanceError(
            f"{where} 出现「{FAKE_TEXT_MARKER}」标记：服务端跑的是桩管线"
            f"（{FAKE_PIPELINE_ENV}），本轮不是真实验收证据")


def gate_issues_for(video: Path, expected_seconds: float) -> list[dict[str, str]]:
    """用服务端同一个技术档函数本地复核。

    服务端只把 ``error`` 回给客户端（``segment_validation``），warning（例如 7–10 秒档
    ``segment_video_duration_unknown_tier``）不会出现在响应里。证据要如实记录这些
    warning，所以在驱动器这一侧用**同一个函数**再跑一遍，不另写一套判据。
    """
    try:
        from minimax_h3_prompt.tools.preflight import check_segment_video

        issues = check_segment_video(video, expected_seconds)
    except Exception as exc:  # noqa: BLE001 - 复核失败只影响证据，不阻断提交
        return [{"severity": "warning", "code": "gate_recheck_failed",
                 "message": f"本地复核失败：{type(exc).__name__}: {exc}"}]
    return [
        {"severity": str(issue.severity), "code": str(issue.code),
         "message": str(issue.message)}
        for issue in issues
    ]


# ---------------------------------------------------------------- 首帧图

def recorded_frame_image(reports_root: Path) -> Path | None:
    """``output/accept-d4`` 历史报告里记过、仍在盘上的真实图片（最近一次优先）。

    D4 验收用过的图就是「真实照片」的现成证据：报告里带着路径与 sha256，
    比重新猜一张更可靠（``accept_d4.find_real_image`` 的口径）。
    """
    for report in sorted(
            Path(reports_root).glob("*/report.json"),
            key=lambda path: path.stat().st_mtime, reverse=True):
        try:
            payload = json.loads(report.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        image = payload.get("image") or {}
        candidate = Path(str(image.get("path") or ""))
        if candidate.is_file():
            return candidate
    return None


def _accept_d4_finder() -> Callable[[Path], Path | None] | None:
    """取回 ``accept_d4.find_real_image``：同一套挑图口径，不另写一份。"""
    import importlib.util
    import sys

    path = ROOT / "scripts" / "accept_d4.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("accept_d4_for_d6", path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    # 必须先登记再执行：accept_d4 里的 @dataclass 要按模块名回查 sys.modules，
    # 漏这一步会在 dataclasses 内部炸 AttributeError（2026-11-13 实测）。
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except (OSError, ImportError):
        return None
    finder = getattr(module, "find_real_image", None)
    return finder if callable(finder) else None


def find_frame_image() -> Path | None:
    """默认首帧图：D4 验收记录 → 沿用 ``accept_d4.find_real_image`` 的口径挑一张。"""
    recorded = recorded_frame_image(DEFAULT_OUTPUT_ROOT.parent / "accept-d4")
    if recorded is not None:
        return recorded
    finder = _accept_d4_finder()
    return finder(ROOT) if finder is not None else None


def _resolve_frame_image(args: argparse.Namespace) -> Path:
    """FL2VA 的首帧图：显式路径优先，否则按默认口径找；找不到就带可操作提示停下。"""
    if args.frame_image:
        path = Path(args.frame_image).expanduser().resolve()
        _expect(path.is_file(), f"--frame-image 指定的图片不存在：{path}")
    else:
        found = find_frame_image()
        _expect(
            found is not None,
            "找不到真实首帧图：FL2VA 的第 1 段要一张开场画面。"
            "用 --frame-image PATH 指定一张真实照片"
            "（例如 output/accept-137-16x9/f_00.05.png），"
            "或先跑一次 D4 验收留下 output/accept-d4 记录")
        path = Path(found).resolve()
    _expect(
        path.stat().st_size >= 8 * 1024,
        f"首帧图太小（{path.stat().st_size} 字节），像测试夹具："
        f"请用 --frame-image 换一张真实照片：{path}")
    return path


# ---------------------------------------------------------------- HTTP

def _headers(token: str) -> dict[str, str]:
    return {"X-Workbench-Token": token}


def _health(client: httpx.Client, base_url: str) -> dict[str, Any]:
    response = client.get(f"{base_url}/api/v1/health")
    _expect(response.status_code == 200, f"/health 返回 {response.status_code}")
    return response.json()


def _probe_health(base_url: str) -> dict[str, Any] | None:
    """自检与「接已有服务还是自己起」共用：不可达返回 None，不抛。"""
    try:
        with httpx.Client(timeout=httpx.Timeout(10.0)) as client:
            return _health(client, _normalize_base_url(base_url))
    except Exception:  # noqa: BLE001 - 不可达就是不可达
        return None


def _session_path(session_id: str) -> str:
    return quote(session_id, safe="/")


def _session_detail(
    client: httpx.Client, base_url: str, token: str, session_id: str,
) -> dict[str, Any]:
    response = client.get(
        f"{base_url}/api/v1/sessions/{_session_path(session_id)}",
        headers=_headers(token),
    )
    _expect(response.status_code == 200,
            f"取会话快照返回 {response.status_code}：{_problem_text(response)}")
    return response.json()


def _session_token(client: httpx.Client, base_url: str) -> str:
    response = client.get(f"{base_url}/api/v1/auth/token")
    # 只记状态码：响应体是凭据（ADR 0033 第 7 条）。
    _expect(response.status_code == 200, f"引导令牌返回 {response.status_code}")
    token = str(response.json().get("token") or "")
    _expect(bool(token), "引导令牌为空")
    return token


def _artifact_content(
    client: httpx.Client, base_url: str, token: str, artifact_id: str,
) -> str:
    response = client.get(
        f"{base_url}/api/v1/artifacts/{quote(artifact_id, safe='/')}/content",
        headers=_headers(token),
    )
    _expect(response.status_code == 200,
            f"读产物正文返回 {response.status_code}：{_problem_text(response)}")
    return response.text


def _wait_job(
    client: httpx.Client, base_url: str, token: str, session_id: str, job_id: str,
    *, timeout: float, sessions_root: Path,
) -> dict[str, Any]:
    """等一个任务收尾；失败时把事件里的 detail 带出来（JobSummary 只有 code）。"""
    started = time.monotonic()
    last_print = started
    while True:
        response = client.get(
            f"{base_url}/api/v1/sessions/{_session_path(session_id)}/jobs/{job_id}",
            headers=_headers(token),
        )
        _expect(response.status_code == 200,
                f"取任务返回 {response.status_code}：{_problem_text(response)}")
        job = response.json()
        if job.get("status") != "running":
            if job.get("status") == "failed":
                failure = latest_job_failure(sessions_root, session_id, job_id)
                raise AcceptanceError(
                    f"任务失败（{job.get('error_code')}）：{failure or '事件里没有 detail'}")
            return job
        now = time.monotonic()
        if now - started > timeout:
            raise AcceptanceError(f"任务 {job_id} 超过 {timeout:.0f}s 仍在跑")
        if now - last_print >= HEARTBEAT_S:
            print(f"[wait] 任务 {job_id} 仍在跑（已等 {int(now - started)}s）", flush=True)
            last_print = now
        time.sleep(POLL_INTERVAL_S)


def _wait_detail(
    client: httpx.Client, base_url: str, token: str, session_id: str,
    predicate: Callable[[dict[str, Any]], bool], *, timeout: float,
    what: str, sessions_root: Path,
) -> dict[str, Any]:
    started = time.monotonic()
    last_print = started
    status = ""
    while True:
        detail = _session_detail(client, base_url, token, session_id)
        if predicate(detail):
            return detail
        status = str(detail.get("status") or "")
        if status in ("failed", "cancelled"):
            failure = latest_job_failure(sessions_root, session_id)
            raise AcceptanceError(f"等待「{what}」时会话进入 {status}：{failure}")
        now = time.monotonic()
        if now - started > timeout:
            raise AcceptanceError(
                f"等待「{what}」超时（{timeout:.0f}s），最后状态 {status}")
        if now - last_print >= HEARTBEAT_S:
            print(f"[wait] 等待「{what}」：状态={status}，已等 {int(now - started)}s",
                  flush=True)
            last_print = now
        time.sleep(POLL_INTERVAL_S)


# ---------------------------------------------------------------- 被测服务

@dataclass
class ServiceHandle:
    base_url: str
    mode: str
    process: subprocess.Popen | None = None
    log_path: Path | None = None
    log_handle: Any = None

    def stop(self) -> None:
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        if self.log_handle is not None:
            try:
                self.log_handle.close()
            except OSError:
                pass


def ensure_service(
    base_url: str, out_dir: Path, args: argparse.Namespace, report: Report,
) -> ServiceHandle:
    """有服务就接、没有就起（``--attach`` / ``--start-server`` 可强制）。

    自己起的时候会**摘掉** ``MINIMAX_FAKE_PIPELINE`` 并扫描启动日志：桩管线开着的
    服务不能产出真实验收证据，而且这条不能只靠操作者记得。
    """
    base_url = _normalize_base_url(base_url)
    health = _probe_health(base_url)
    if health is not None and args.start_server:
        raise AcceptanceError(
            f"--start-server 指定自己起服务，但 {base_url} 已有服务在应答 /health；"
            "先关掉它，或去掉 --start-server 复用")
    if health is not None:
        print(f"[server] 复用已运行的服务：{base_url}", flush=True)
        report.facts["server_mode"] = "attached"
        return ServiceHandle(base_url, "attached")
    if args.attach:
        raise AcceptanceError(
            f"--attach 指定复用已有服务，但 {base_url} 不可达（/health 无响应）")

    env = {key: value for key, value in os.environ.items()
           if key != FAKE_PIPELINE_ENV}
    report.facts["fake_env_removed"] = FAKE_PIPELINE_ENV in os.environ
    log_path = out_dir / "server.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = log_path.open("w", encoding="utf-8")
    print(f"[server] 起真实服务：{sys.executable} -m minimax_h3_prompt.api"
          f"（cwd={ROOT}，日志 {log_path}）", flush=True)
    process = subprocess.Popen(
        [sys.executable, "-m", "minimax_h3_prompt.api"],
        cwd=str(ROOT), env=env, stdout=handle, stderr=subprocess.STDOUT,
        text=True,
    )
    started = time.monotonic()
    while True:
        health = _probe_health(base_url)
        if health is not None:
            break
        if process.poll() is not None:
            handle.flush()
            tail = "\n".join(
                log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-20:])
            handle.close()
            raise AcceptanceError(
                f"服务进程提前退出（退出码 {process.returncode}）：\n{tail}")
        if time.monotonic() - started > STARTUP_TIMEOUT_S:
            process.terminate()
            handle.close()
            raise AcceptanceError(
                f"服务在 {STARTUP_TIMEOUT_S:.0f}s 内没有就绪：{base_url}")
        time.sleep(POLL_INTERVAL_S)
    log_text = log_path.read_text(encoding="utf-8", errors="replace")
    if "[桩管线]" in log_text:
        process.terminate()
        handle.close()
        raise AcceptanceError("服务启动日志显示桩管线已开启：本轮不是真实验收证据")
    report.facts["server_mode"] = "spawned"
    report.facts["server_log"] = str(log_path)
    return ServiceHandle(base_url, "spawned", process=process,
                         log_path=log_path, log_handle=handle)


def _assert_service_health(
    client: httpx.Client, base_url: str, facts: dict[str, Any],
) -> dict[str, Any]:
    health = _health(client, base_url)
    facts["server_version"] = health.get("version")
    facts["server_health"] = {
        key: health.get(key)
        for key in ("version", "config_path", "data_root", "env_path",
                    "missing_keys", "base_url")
    }
    problems = health_config_problems(
        str((facts.get("config_source") or {}).get("config_path") or ""),
        health, expected_base_url=base_url)
    _expect(not problems, "；".join(problems))
    return health


# ---------------------------------------------------------------- 段状态与提示词

def _upsert_segment(facts: dict[str, Any], index: int,
                    **changes: Any) -> dict[str, Any]:
    segments: list[dict[str, Any]] = facts.setdefault("segments", [])
    row = next((item for item in segments if int(item.get("index") or 0) == index), None)
    if row is None:
        row = {"index": index}
        segments.append(row)
        segments.sort(key=lambda item: int(item.get("index") or 0))
    row.update(changes)
    return row


def _merge_segment_rows(facts: dict[str, Any],
                        rows: list[dict[str, Any]]) -> None:
    for row in rows:
        _upsert_segment(
            facts, int(row["index"]),
            start_s=row["start_s"], end_s=row["end_s"],
            planned_seconds=row["planned_seconds"], status=row["status"],
            prompt_artifact_id=row["prompt_artifact_id"],
            video_asset_id=row["video_asset_id"],
        )


def _artifact_id_by_name(detail: dict[str, Any], name: str) -> str:
    return next(
        (str(item.get("artifact_id") or "")
         for item in detail.get("artifacts") or []
         if item.get("name") == name),
        "",
    )


def _write_prompt_files(
    client: httpx.Client, base_url: str, token: str,
    facts: dict[str, Any], out_dir: Path,
) -> list[Path]:
    """把已有提示词的段写进 ``prompts/segment-NN.md``（原文照抄，可直接粘进 ComfyUI）。"""
    prompts_dir = out_dir / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for row in facts.get("segments") or []:
        artifact_id = str(row.get("prompt_artifact_id") or "")
        if not artifact_id:
            continue
        text = _artifact_content(client, base_url, token, artifact_id)
        _expect(bool(text.strip()), f"第 {row['index']} 段提示词正文为空")
        _reject_fake_text(text, f"第 {row['index']} 段提示词")
        path = prompts_dir / f"segment-{int(row['index']):02d}.md"
        path.write_text(text, encoding="utf-8")
        row["prompt_path"] = str(path)
        row["prompt_bytes"] = len(text.encode("utf-8"))
        written.append(path)
    return written


def _current_waiting_segment(facts: dict[str, Any]) -> int | None:
    waiting = [
        int(row["index"]) for row in facts.get("segments") or []
        if str(row.get("status")) == "waiting_video"
    ]
    return min(waiting) if waiting else None


def render_handoff(
    facts: dict[str, Any], pending_index: int | None, out_dir: Path,
) -> str:
    """给用户的操作说明：哪一段、按几秒生成、存成什么名字、怎么交回来。"""
    if pending_index is None:
        final = facts.get("final_film") or {}
        return "\n".join([
            "# D6 真实验收：全部段已通过",
            "",
            f"- 会话：`{facts.get('session_id', '')}`（状态 "
            f"`{facts.get('session_status', '')}`）",
            f"- 成片：`{final.get('path', '（尚未登记）')}`"
            f"（{final.get('bytes', '')} 字节）",
            "",
            "段视频已全部回交并通过技术档与语义闸门，服务端已完成拼接。"
            "证据见同目录 `report.md`。",
            "",
        ])
    rows = [row for row in facts.get("segments") or []
            if int(row.get("index") or 0) == pending_index]
    row = rows[0] if rows else {}
    seconds = row.get("planned_seconds") or "（规划器未给出）"
    prompt_path = row.get("prompt_path") or (
        out_dir / "prompts" / f"segment-{int(pending_index or 1):02d}.md")
    videos_dir = out_dir / "videos"
    lines = [
        "# D6 真实验收：下一段视频请你生成",
        "",
        f"- 会话：`{facts.get('session_id', '')}`",
        f"- 服务：`{facts.get('base_url', '')}`",
        f"- 会话目录：`{facts.get('sessions_root', '')}`",
        f"- 当前待交：**第 {pending_index} 段**（规划 {seconds} 秒）",
        f"- 本段提示词：`{prompt_path}`",
        "",
        "## 你要做的",
        "",
        f"1. 打开 `{prompt_path}`，把正文原样交给 ComfyUI 的 H3 节点。",
        f"2. 按本段规划时长生成：**{seconds} 秒**（H3 节点支持 4–15 秒整数档，"
        "不支持小数）。",
        f"3. 把输出视频保存为 `segment-{int(pending_index or 1):02d}.mp4`，放进 "
        f"`{videos_dir}`（也可以放任意目录，下一步用参数指过去）。",
        "4. 回到终端重跑：",
        "",
        "```bash",
        "PYTHONIOENCODING=utf-8 PYTHONPATH=src .venv/Scripts/python.exe \\",
        f"    scripts/accept_d6.py --submit-dir {videos_dir}",
        "```",
        "",
        "## 交回后会发生什么",
        "",
        "- 上传素材 → resolve `segment_video` 待办 → 同步技术档校验（帧数档/时长/尺寸）；",
        "- 通过后剥尾帧、视觉模型读图判定是否达到本段 `end_hook`（语义闸门）；",
        "- 通过则自动写下一段提示词；全部通过后自动拼接 `final.mp4`。",
        "",
        "## 如实记录的两条边界",
        "",
        "- 7–10 秒档不在 H3 实测帧数表（`H3_TIER_FRAMES` 只有 4/5/6 秒档）内，"
        "技术档闸门只报 `warning`（`segment_video_duration_unknown_tier`），"
        "证据里会原样记录，不当作失败。",
        "- 段数上限（15 段 / 60 秒）由 CI 的桩管线覆盖；这次真实档是 18 秒。",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------- 两阶段

def _make_session(
    client: httpx.Client, base_url: str, token: str, args: argparse.Namespace,
) -> str:
    payload = {
        "mode": "base",
        "variant": args.variant,
        "duration": float(args.duration),
        "style": args.style,
        "language": args.language,
        "plot": args.plot,
    }
    response = client.post(
        f"{base_url}/api/v1/sessions", json=payload, headers=_headers(token))
    _expect(response.status_code == 201,
            f"建会话返回 {response.status_code}：{_problem_text(response)}")
    return str(response.json().get("session_id") or "")


def _start_job(
    client: httpx.Client, base_url: str, token: str, session_id: str,
) -> str:
    response = client.post(
        f"{base_url}/api/v1/sessions/{_session_path(session_id)}/jobs",
        headers=_headers(token))
    _expect(response.status_code == 202,
            f"起任务返回 {response.status_code}：{_problem_text(response)}")
    return str(response.json().get("job_id") or "")


def _resolve(
    client: httpx.Client, base_url: str, token: str, session_id: str,
    item_id: str, payload: dict[str, Any],
) -> dict[str, Any] | None:
    response = client.post(
        f"{base_url}/api/v1/sessions/{_session_path(session_id)}"
        f"/action-items/{item_id}/resolve",
        json=payload, headers=_headers(token))
    if response.status_code == 422:
        raise SegmentRejected(_problem_text(response))
    _expect(response.status_code == 202,
            f"决议待办返回 {response.status_code}：{_problem_text(response)}")
    body = response.json()
    return body if isinstance(body, dict) else None


class SegmentRejected(AcceptanceError):
    """用户提交的段视频不合格（422）：不是系统故障，换一段重交即可。"""


def _upload_video(
    client: httpx.Client, base_url: str, token: str, session_id: str, video: Path,
) -> str:
    with video.open("rb") as handle:
        response = client.post(
            f"{base_url}/api/v1/sessions/{_session_path(session_id)}/assets",
            headers=_headers(token),
            files={"file": (video.name, handle, "video/mp4")},
            timeout=httpx.Timeout(300.0),
        )
    _expect(response.status_code in (200, 201),
            f"上传段视频返回 {response.status_code}：{_problem_text(response)}")
    return str(response.json().get("asset_id") or "")


_IMAGE_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


def _upload_frame_image(
    client: httpx.Client, base_url: str, token: str, session_id: str, image: Path,
) -> str:
    """把开场帧上传成会话素材（FL2VA）；返回 ``asset_id`` 供帧提交引用。"""
    media_type = _IMAGE_MEDIA_TYPES.get(image.suffix.lower(), "image/png")
    with image.open("rb") as handle:
        response = client.post(
            f"{base_url}/api/v1/sessions/{_session_path(session_id)}/assets",
            headers=_headers(token),
            files={"file": (image.name, handle, media_type)},
            timeout=httpx.Timeout(300.0),
        )
    _expect(response.status_code in (200, 201),
            f"上传首帧图返回 {response.status_code}：{_problem_text(response)}")
    asset_id = str(response.json().get("asset_id") or "")
    _expect(bool(asset_id), "上传首帧图没有返回 asset_id")
    return asset_id


def run_prepare(
    client: httpx.Client, base_url: str, token: str, args: argparse.Namespace,
    report: Report,
) -> int:
    facts = report.facts
    report_out_dir = Path(facts["output_dir"])
    sessions_root = Path(facts["sessions_root"])
    variant = str(args.variant).strip().upper()
    facts.update({
        "phase": "prepare",
        "plot": args.plot,
        "variant": args.variant,
        "duration": args.duration,
        "style": args.style,
        "language": args.language,
        "pipeline": (
            "real（真实 LLM；服务端桩开关无法从 HTTP 观测，桩文本标记会被内容检查拦下）"),
        "variant_note": (
            "默认 FL2VA：第 1 段提示词以用户交的开场帧为锚，与真实长片的首帧路线一致。"
            if variant == "FL2VA" else
            "T2VA：第 1 段缺首帧图，提示词会引用不存在的 Picture 1（仅用于对照）。"),
    })
    if variant == "FL2VA":
        frame_image = _resolve_frame_image(args)
        facts["frame_image"] = {
            "path": str(frame_image),
            "bytes": frame_image.stat().st_size,
            "sha256": _sha256(frame_image),
        }
    else:
        frame_image = None

    def create() -> str:
        session_id = _make_session(client, base_url, token, args)
        _expect(bool(session_id), "建会话没有返回 session_id")
        facts["session_id"] = session_id
        facts["session_url"] = (
            f"{base_url}/sessions/{_session_path(session_id)}")
        return session_id

    resume_id = str(args.resume or "").strip()
    if resume_id:
        session_id = resume_id
        facts["session_id"] = session_id
        facts["session_url"] = f"{base_url}/sessions/{_session_path(session_id)}"
        facts["resumed_from"] = resume_id

        def resume() -> str:
            """复用已有会话：阶段 1 的产物已在快照里，绝不重跑。

            处于 ``failed`` 时发一次重试任务——那正是「阶段 2 被 provider 拒掉后
            换模型接着跑」的形态；其它状态不重起，交给后面的待办推进。
            """
            detail = _session_detail(client, base_url, token, session_id)
            status = str(detail.get("status") or "")
            facts["resume_status"] = status
            if status == STATUS_FAILED:
                job_id = _start_job(client, base_url, token, session_id)
                _wait_job(client, base_url, token, session_id, job_id,
                          timeout=args.timeout, sessions_root=sessions_root)
                return "会话处于 failed：重试任务已跑完"
            return f"会话状态 {status}：不重起任务"

        report.run("续跑已有会话（不重建、不重跑阶段 1）", resume)

    def stage1() -> str:
        job_id = _start_job(client, base_url, token, session_id)
        _wait_job(client, base_url, token, session_id, job_id,
                  timeout=args.timeout, sessions_root=sessions_root)
        detail = _wait_detail(
            client, base_url, token, session_id,
            lambda body: pending_review_item(body) is not None,
            timeout=args.timeout, what="阶段 1 审查待办", sessions_root=sessions_root)
        artifacts = [
            {"name": item.get("name"), "bytes": item.get("bytes")}
            for item in detail.get("artifacts") or []
            if item.get("kind") in ("script", "shot_table")
        ]
        facts["stage1_artifacts"] = artifacts
        script_id = _artifact_id_by_name(detail, "script.md")
        _expect(bool(script_id), "阶段 1 完成但没有 script.md 产物")
        _reject_fake_text(
            _artifact_content(client, base_url, token, script_id), "阶段 1 剧本")
        return f"阶段 1 完成；中间产物 {[item['name'] for item in artifacts]}"

    if not resume_id:
        session_id = report.run("建 18 秒会话", create)
        report.run("起阶段 1 并等待审查待办", stage1)

    report.run("起阶段 1 并等待审查待办", stage1)

    def approve() -> str:
        detail = _session_detail(client, base_url, token, session_id)
        item = pending_review_item(detail)
        # 续跑时会话的审查待办早已 resolved：不重复决议（决议一份已过的待办会 422）。
        review_resumed = item is None
        if review_resumed:
            facts["review_resumed"] = True
        else:
            job = _resolve(client, base_url, token, session_id,
                           str(item["item_id"]), {"decision": "approve"})
            if job is not None:
                _wait_job(client, base_url, token, session_id, str(job["job_id"]),
                          timeout=args.timeout, sessions_root=sessions_root)
        frame_note = "T2VA（无首帧图）"
        if variant == "FL2VA":
            assert frame_image is not None  # run_prepare 已在启动前解析过
            if review_resumed:
                # 续跑：帧可能已交（待办已 resolved），也可能还没交——有 pending 才交。
                frame_item = pending_frame_item(detail)
            else:
                frame_detail = _wait_detail(
                    client, base_url, token, session_id,
                    lambda body: pending_frame_item(body) is not None,
                    timeout=args.timeout, what="帧提交待办",
                    sessions_root=sessions_root)
                frame_item = _require(pending_frame_item(frame_detail),
                                      "FL2VA 审查通过后没有 frame_submission 待办")
            if frame_item is None:
                facts["frame_resumed"] = True
            else:
                frame_asset = _upload_frame_image(
                    client, base_url, token, session_id, frame_image)
                frame_job = _resolve(
                    client, base_url, token, session_id,
                    str(frame_item["item_id"]), {"frames": {"first": frame_asset}})
                if frame_job is not None:
                    _wait_job(client, base_url, token, session_id,
                              str(frame_job["job_id"]),
                              timeout=args.timeout, sessions_root=sessions_root)
                facts["frame_asset_id"] = frame_asset
            frame_note = f"FL2VA（首帧 {frame_image.name}）"
        final = _wait_detail(
            client, base_url, token, session_id,
            lambda body: bool(body.get("segments"))
            and pending_segment_item(body) is not None,
            timeout=args.timeout, what="阶段 2 与分段规划", sessions_root=sessions_root)
        _merge_segment_rows(facts, segment_rows(final))
        facts["session_status"] = final.get("status")
        pending = _require(pending_segment_item(final), "规划完成后没有段视频待办")
        return (f"阶段 2 完成（{frame_note}）；规划出 "
                f"{len(final.get('segments') or [])} 段，"
                f"停在等第 {pending['segment_index']} 段视频")

    report.run("通过审查并等待阶段 2 → 分段规划 → 第 1 段提示词", approve)

    def prompts_and_handoff() -> str:
        written = _write_prompt_files(client, base_url, token, facts, report_out_dir)
        pending = _current_waiting_segment(facts)
        facts["waiting_for"] = pending
        facts["coordinates_note"] = (
            "段数与段长由规划器决定（4–10 秒）；真实档 18 秒，未跑到 60 秒上限。")
        (report_out_dir / "videos").mkdir(parents=True, exist_ok=True)
        (report_out_dir / "handoff.md").write_text(
            render_handoff(facts, pending, report_out_dir), encoding="utf-8")
        _persist_state(report_out_dir, facts)
        return (f"写出 {len(written)} 份提示词；下一段待交第 {pending} 段；"
                f"操作说明见 handoff.md")

    report.run("写出段提示词与 handoff", prompts_and_handoff)
    _capture_screenshots(base_url, session_id, report_out_dir, facts, report,
                         args.no_screenshots)
    return EXIT_WAITING_USER


def run_submit(
    client: httpx.Client, base_url: str, token: str, args: argparse.Namespace,
    report: Report,
) -> int:
    facts = report.facts
    sessions_root = Path(facts["sessions_root"])
    out_dir = Path(facts["output_dir"])
    videos = collect_segment_videos(Path(args.submit_dir).expanduser().resolve())
    _expect(bool(videos), f"目录里没有 segment-NN.mp4：{args.submit_dir}")
    facts["submit_dir"] = str(Path(args.submit_dir).resolve())
    facts["submit_files"] = {str(index): str(path) for index, path in videos.items()}
    facts["phase"] = "submit"
    facts["pipeline"] = (
        "real（真实 LLM；服务端桩开关无法从 HTTP 观测，桩文本标记会被内容检查拦下）")
    session_id = str(facts["session_id"])

    while True:
        detail = _session_detail(client, base_url, token, session_id)
        facts["session_status"] = detail.get("status")
        if detail.get("status") == "completed":
            break
        if detail.get("status") in ("failed", "cancelled"):
            failure = latest_job_failure(sessions_root, session_id)
            raise AcceptanceError(
                f"会话进入 {detail.get('status')}：{failure}")
        if detail.get("status") == "running":
            # 上一轮写词/校验/拼接还在跑：等它落到人工等待或终态。
            detail = _wait_detail(
                client, base_url, token, session_id,
                lambda body: body.get("status") != "running",
                timeout=args.timeout, what="上一轮分段任务", sessions_root=sessions_root)
            facts["session_status"] = detail.get("status")
            _merge_segment_rows(facts, segment_rows(detail))
            continue
        _merge_segment_rows(facts, segment_rows(detail))
        item = _require(
            pending_segment_item(detail),
            f"会话在 {detail.get('status')} 但没有任何 pending 的 segment_video 待办")
        index = int(item["segment_index"] or 0)
        row: dict[str, Any] = next(
            (entry for entry in facts.get("segments") or []
             if int(entry.get("index") or 0) == index), {})
        if index not in videos:
            facts["waiting_for"] = index
            (out_dir / "handoff.md").write_text(
                render_handoff(facts, index, out_dir), encoding="utf-8")
            _persist_state(out_dir, facts)
            print(f"[result] 等待第 {index} 段视频：segment-{index:02d}.mp4"
                  f"（放进 {facts.get('submit_dir')} 后重跑 --submit-dir）",
                  flush=True)
            return EXIT_WAITING_USER
        video = videos.pop(index)

        def submit() -> str:
            issues = gate_issues_for(video, float(row.get("planned_seconds") or 0))
            upload_started = time.monotonic()
            asset_id = _upload_video(
                client, base_url, token, session_id, video)
            resolve_started = time.monotonic()
            segment = _upsert_segment(
                facts, index, submitted_at=_utc_now(), video_file=str(video),
                video_asset_id=asset_id, gate_issues=issues,
                submit_bytes=video.stat().st_size,
                submit_sha256=_sha256(video),
                upload_ms=int((resolve_started - upload_started) * 1000))
            segment.pop("rejection", None)
            try:
                job = _resolve(client, base_url, token, session_id,
                               str(item["item_id"]), {"video_asset_id": asset_id})
            except SegmentRejected as exc:
                segment["rejection"] = str(exc)
                horizon = [gate for gate in issues if gate["severity"] == "error"]
                raise SegmentRejected(
                    f"第 {index} 段提交被 422 拒绝：{exc}"
                    + (f"（本地技术档复核：{horizon}）" if horizon else "")
                ) from exc
            validate_started = time.monotonic()
            segment["resolve_ms"] = int((validate_started - resolve_started) * 1000)
            if job is not None:
                _wait_job(client, base_url, token, session_id, str(job["job_id"]),
                          timeout=args.timeout, sessions_root=sessions_root)
            segment["validate_ms"] = int(
                (time.monotonic() - validate_started) * 1000)
            return asset_id

        started = time.monotonic()
        step_name = f"第 {index} 段：上传 → 交回 → 等校验"
        print(f"[step] 开始：{step_name}", flush=True)
        try:
            note = submit()
        except SegmentRejected as exc:
            elapsed = int((time.monotonic() - started) * 1000)
            # 被拒不是系统故障，也不该让「验收未全部通过」——用户换一段视频重交即可。
            report.step(step_name, "rejected", elapsed, str(exc))
            _upsert_segment(facts, index, rejected_at=_utc_now())
            facts["waiting_for"] = index
            (out_dir / "handoff.md").write_text(
                render_handoff(facts, index, out_dir), encoding="utf-8")
            _persist_state(out_dir, facts)
            print(f"[result] 第 {index} 段被拒，需要换一段视频：{exc}", flush=True)
            return EXIT_WAITING_USER
        except Exception as exc:  # noqa: BLE001 - 系统故障要落证据
            elapsed = int((time.monotonic() - started) * 1000)
            report.step(step_name, "failed", elapsed, f"{type(exc).__name__}: {exc}")
            raise
        elapsed_ms = int((time.monotonic() - started) * 1000)
        report.step(step_name, "ok", elapsed_ms, note)
        print(f"[step] 成功：{step_name}（{elapsed_ms} ms）", flush=True)
        _upsert_segment(facts, index, submit_total_ms=elapsed_ms)

        detail = _session_detail(client, base_url, token, session_id)
        facts["session_status"] = detail.get("status")
        _merge_segment_rows(facts, segment_rows(detail))
        current_row: dict[str, Any] = next(
            (entry for entry in facts.get("segments") or []
             if int(entry.get("index") or 0) == index), {})
        if current_row.get("status") != "passed":
            # 语义闸门拒绝：段回到 waiting_video、待办重建（服务端已完成这一步）。
            _upsert_segment(facts, index, rejected_at=_utc_now())
            facts["waiting_for"] = index
            (out_dir / "handoff.md").write_text(
                render_handoff(facts, index, out_dir), encoding="utf-8")
            _persist_state(out_dir, facts)
            print(f"[result] 第 {index} 段未通过语义闸门（end_hook 未达到），"
                  f"换一段视频重跑", flush=True)
            return EXIT_WAITING_USER
        _upsert_segment(facts, index, passed_at=_utc_now())

        # 下一段提示词是惰性写的：等到它出现再写文件、更新 handoff。
        def next_prompt() -> str:
            body = _wait_detail(
                client, base_url, token, session_id,
                lambda current: current.get("status") == "completed"
                or pending_segment_item(current) is not None,
                timeout=args.timeout, what=f"第 {index} 段之后的下一步",
                sessions_root=sessions_root)
            _merge_segment_rows(facts, segment_rows(body))
            facts["session_status"] = body.get("status")
            written = _write_prompt_files(client, base_url, token, facts, out_dir)
            return f"新增提示词 {[path.name for path in written]}"

        next_started = time.monotonic()
        report.run(f"第 {index} 段通过后等待下一段提示词/自动拼接", next_prompt)
        if facts.get("session_status") == "completed":
            facts["assembly_ms"] = int(
                (time.monotonic() - next_started) * 1000)

    _verify_final(client, base_url, token, sessions_root, session_id, facts)
    _capture_screenshots(base_url, session_id, out_dir, facts, report,
                         args.no_screenshots)
    facts["waiting_for"] = None
    (out_dir / "handoff.md").write_text(
        render_handoff(facts, None, out_dir), encoding="utf-8")
    _persist_state(out_dir, facts)
    return EXIT_COMPLETED


def _verify_final(
    client: httpx.Client, base_url: str, token: str, sessions_root: Path,
    session_id: str, facts: dict[str, Any],
) -> None:
    """成片证据：登记表里有 ``final.mp4``、磁盘在、content 端点 200/206 可读。"""
    # 文件名从服务端登记表的同一常量取：抄一份字面量会在改名当天静默失配。
    from minimax_h3_prompt.workbench_store import ARTIFACT_FINAL_FILM

    detail = _session_detail(client, base_url, token, session_id)
    _expect(detail.get("status") == "completed",
            f"全部段通过后会话不是 completed：{detail.get('status')}")
    artifact = _require(
        next((item for item in detail.get("artifacts") or []
              if item.get("name") == ARTIFACT_FINAL_FILM), None),
        "会话 completed 但产物清单里没有 final.mp4")
    artifact_id = str(artifact.get("artifact_id") or "")
    local = sessions_root / artifact_id
    _expect(local.is_file(), f"final.mp4 登记了但磁盘上不存在：{local}")
    url = f"{base_url}/api/v1/artifacts/{quote(artifact_id, safe='/')}/content"
    head = client.head(url, headers=_headers(token))
    _expect(head.status_code == 200, f"成片 HEAD 返回 {head.status_code}")
    ranged = client.get(url, headers={**_headers(token), "Range": "bytes=0-99"})
    _expect(ranged.status_code == 206,
            f"成片 Range 读取返回 {ranged.status_code}（期望 206）")
    _expect(len(ranged.content) == 100,
            f"成片 Range 0-99 返回 {len(ranged.content)} 字节")
    facts["final_film"] = {
        "artifact_id": artifact_id,
        "path": str(local),
        "bytes": local.stat().st_size,
        "head_status": head.status_code,
        "range_status": ranged.status_code,
        "content_type": head.headers.get("content-type", ""),
    }
    facts["session_status"] = "completed"


# ---------------------------------------------------------------- 截图

def _capture_screenshots(
    base_url: str, session_id: str, out_dir: Path, facts: dict[str, Any],
    report: Report, disabled: bool,
) -> None:
    """浏览器截图是证据的一部分，但缺 Playwright / 未构建 dist 不该把验收判成失败。

    页面能打开就拍「分段」与完成态；打不开就如实写进报告，不用一张旧图冒充。
    """
    if disabled:
        facts["screenshots_note"] = "本次用 --no-screenshots 跳过"
        return
    try:
        from playwright.sync_api import sync_playwright
    except Exception:  # noqa: BLE001 - 没装就只影响截图
        facts["screenshots_note"] = "Playwright 未安装，跳过截图（API 证据不受影响）"
        return
    shots_dir = out_dir / "screenshots"
    shots_dir.mkdir(parents=True, exist_ok=True)
    captured: list[str] = []
    try:
        with sync_playwright() as playwright:
            channel = os.environ.get("PW_CHANNEL") or None
            browser = playwright.chromium.launch(headless=True, channel=channel)
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 900})
                response = page.goto(
                    f"{base_url}/sessions/{_session_path(session_id)}",
                    wait_until="domcontentloaded", timeout=30_000)
                if response is not None and response.status >= 400:
                    facts["screenshots_note"] = (
                        f"页面不可用（HTTP {response.status}）：前端构建产物可能未生成"
                        f"（frontend/dist），截图不代表界面")
                page.wait_for_timeout(1500)
                for tab_name, shot_name in (("分段", "segments"),
                                            ("产物", "artifacts")):
                    try:
                        page.get_by_role("tab", name=tab_name).click(timeout=5_000)
                        page.wait_for_timeout(800)
                    except Exception:  # noqa: BLE001 - 分区可能不存在
                        pass
                    path = shots_dir / f"{shot_name}.png"
                    page.screenshot(path=str(path), full_page=True)
                    captured.append(str(path))
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001 - 截图失败只影响截图证据
        facts["screenshots_note"] = f"截图失败：{type(exc).__name__}: {exc}"
    facts["screenshots"] = captured


# ---------------------------------------------------------------- 报告

def render_report(
    facts: dict[str, Any], steps: list[dict[str, Any]],
    failures: list[str], out_dir: Path,
) -> str:
    """证据正文。**不写令牌**：facts 里没有 token 键，这里也不去任何响应体取。"""
    config = facts.get("config_source") or {}
    primary = config.get("primary") or {}
    vision = config.get("vision") or {}
    segments = sorted(facts.get("segments") or [],
                      key=lambda row: int(row.get("index") or 0))
    segment_rows_text = "\n".join(
        f"| {row.get('index')} | {row.get('start_s')}–{row.get('end_s')} "
        f"| {row.get('planned_seconds')} | {row.get('status')} "
        f"| `{row.get('prompt_path') or '（尚未写出）'}` "
        f"| {row.get('upload_ms') if row.get('upload_ms') is not None else '—'} / "
        f"{row.get('resolve_ms') if row.get('resolve_ms') is not None else '—'} / "
        f"{row.get('validate_ms') if row.get('validate_ms') is not None else '—'} "
        f"| {row.get('rejected_at') or '—'} |"
        for row in segments
    )
    gate_lines: list[str] = []
    for row in segments:
        for issue in row.get("gate_issues") or []:
            gate_lines.append(
                f"- 第 {row.get('index')} 段 `{issue.get('severity')}` "
                f"`{issue.get('code')}`：{issue.get('message')}")
    rejections = [row for row in segments if row.get("rejected_at")]
    if rejections:
        gate_outcome = (
            f"闸门在真实数据上拦到过 {len(rejections)} 次："
            f"{[int(row['index']) for row in rejections]}（详见上表「被拒」列）")
    elif segments:
        gate_outcome = f"本次 {len(segments)} 段全部一次通过（技术档与语义闸门均未拦截）"
    else:
        gate_outcome = "尚未开始段循环"
    final = facts.get("final_film") or {}
    screenshots = facts.get("screenshots") or []
    step_rows = "\n".join(
        f"| {step['name']} | {step['status']} | {step['ms']} | {step['note']} |"
        for step in steps
    )
    waiting = facts.get("waiting_for")
    lines = [
        "# D6 真实长片验收证据（#251 / 父 spec #243）",
        "",
        "## 运行环境",
        "",
        f"- 命令：`{facts.get('command', '')}`",
        f"- 阶段：`{facts.get('phase', '')}`",
        f"- base_url：`{facts.get('base_url', '')}`",
        f"- sessions_root：`{facts.get('sessions_root', '')}`",
        f"- 起止（UTC）：{facts.get('started_at', '')} → {facts.get('finished_at', '')}",
        f"- 服务版本：`{facts.get('server_version', '')}`；"
        f"起法：{facts.get('server_mode', '')}"
        f"（{facts.get('server_log', '未另存日志')}）",
        f"- 管线：{facts.get('pipeline', '')}",
        "",
        "## 配置来源核对（起被测程序之前）",
        "",
        f"- 配置：`{config.get('config_path', '')}`"
        f"（{config.get('config_bytes', '')} 字节，sha256 `{config.get('config_sha256', '')}`）",
        f"- 密钥文件：`{config.get('env_path', '')}`；"
        f"缺失变量：{config.get('missing_keys', [])}",
        f"- 主模型：provider=`{primary.get('provider', '')}` "
        f"model=`{primary.get('model', '')}` base_url=`{primary.get('base_url', '')}`",
        f"- 视觉模型：model=`{vision.get('model', '')}` "
        f"base_url=`{vision.get('base_url', '')}`",
        f"- 服务回报：`{facts.get('server_health', {})}`",
        "",
        "## 输入",
        "",
        f"- 会话：`{facts.get('session_id', '')}`",
        f"- 剧情：{facts.get('plot', '')}",
        f"- 变体／时长：{facts.get('variant', '')} / {facts.get('duration', '')} 秒",
        f"- 变体理由：{facts.get('variant_note', '')}",
        f"- 首帧图：`{(facts.get('frame_image') or {}).get('path', '（未使用）')}`"
        f"（{(facts.get('frame_image') or {}).get('bytes', '')} 字节，sha256 "
        f"`{(facts.get('frame_image') or {}).get('sha256', '')}`）",
        "",
        "## 分段与规划时长（由规划器决定，4–10 秒）",
        "",
        "| 段号 | 起止（秒） | 规划秒数 | 状态 | 提示词 | 上传/交回/校验（毫秒） | 被拒时刻 |",
        "|---|---|---|---|---|---|---|",
        segment_rows_text or "| — | — | — | — | — | — | — |",
        "",
        "## 闸门记录",
        "",
        gate_outcome,
        *([""] + gate_lines if gate_lines else []),
        "",
        "## 产物",
        "",
        f"- 成片：`{final.get('path', '（尚未产出）')}`"
        f"（{final.get('bytes', '')} 字节；HEAD {final.get('head_status', '')}、"
        f"Range {final.get('range_status', '')}）",
        f"- 自动拼接等待耗时（最后一段通过 → completed）："
        f"{facts.get('assembly_ms', '—')} ms",
        f"- 会话状态：`{facts.get('session_status', '')}`"
        + (f"；当前等待：第 {waiting} 段" if waiting else ""),
        "",
        "## 截图",
        "",
        "\n".join(f"- `{path}`" for path in screenshots)
        or f"（无）{facts.get('screenshots_note', '')}",
        "",
        "## 步骤与耗时",
        "",
        "| 步骤 | 结果 | 毫秒 | 备注 |",
        "|---|---|---|---|",
        step_rows or "| — | — | — | — |",
        "",
        "## 结论",
        "",
        f"- D6-AC14（18 秒真实长片：真实 LLM 写词 + 真实 ComfyUI 段视频 + 真实回交 + "
        f"真实读图 + 真实拼接）：{'通过' if final else '进行中/未完成'}。",
        "- 段数与段长由规划器决定；**未跑到 60 秒上限，段数上限由 CI 桩管线覆盖**，"
        "这份证据不是 15 段结论。",
        "- 段视频由用户在 ComfyUI 生成（系统不生成视频，`main.py` 的既有边界）。",
        "- 证据中不出现令牌：只记状态码，不记引导端点响应体。",
        "",
    ]
    if facts.get("screenshots_note"):
        lines.insert(-1, f"- 截图说明：{facts['screenshots_note']}")
    if failures:
        lines += ["## 未达标项", "", *[f"- {failure}" for failure in failures], ""]
    return "\n".join(lines)


def _persist_state(out_dir: Path, facts: dict[str, Any]) -> None:
    """跨阶段续跑只靠这份文件：会话标识、段状态与已写出的提示词路径。"""
    state = {
        "session_id": facts.get("session_id"),
        "base_url": facts.get("base_url"),
        "sessions_root": facts.get("sessions_root"),
        "plot": facts.get("plot"),
        "variant": facts.get("variant"),
        "duration": facts.get("duration"),
        "style": facts.get("style"),
        "language": facts.get("language"),
        "variant_note": facts.get("variant_note"),
        "frame_image": facts.get("frame_image"),
        "config_source": facts.get("config_source"),
        "segments": facts.get("segments"),
        "final_film": facts.get("final_film"),
        "updated_at": _utc_now(),
    }
    (out_dir / "state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_report_files(
    report: Report, out_dir: Path, phase: str,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    text = render_report(report.facts, report.steps, report.failures, out_dir)
    runs_dir = out_dir / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    archive = runs_dir / f"{_stamp()}-{phase}.md"
    (out_dir / "report.md").write_text(text, encoding="utf-8")
    archive.write_text(text, encoding="utf-8")
    payload = {
        **report.facts,
        "steps": report.steps,
        "failures": report.failures,
        "report_archive": str(archive),
    }
    (out_dir / "report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (runs_dir / f"{archive.stem}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- 自检

def _ffmpeg_path() -> str | None:
    """与产品同一套定位（``imageio-ffmpeg`` 自带优先，其次 PATH），不另写一份。"""
    try:
        from minimax_h3_prompt.tools.seam_audit import find_ffmpeg

        return str(find_ffmpeg())
    except Exception:  # noqa: BLE001 - 找不到就是找不到
        return shutil.which("ffmpeg")


def _ffmpeg_available() -> bool:
    return _ffmpeg_path() is not None


def _fake_env_enabled() -> bool:
    return os.environ.get(FAKE_PIPELINE_ENV, "").strip().lower() in {
        "1", "true", "yes", "on"}


def self_check(args: argparse.Namespace) -> int:
    """不起服务、不建会话、不调模型：真实验收前的结构与环境检查。"""
    checks: list[tuple[str, bool, str]] = []
    facts: dict[str, Any] = {}
    base_url = args.base_url or DEFAULT_BASE_URL
    try:
        facts = config_source_facts()
        problems = config_source_problems(
            facts,
            expected_path=_expected_config_path(args),
            expected_sha256=args.expect_config_sha256,
            expected_model=args.expect_model,
            expected_base_url=args.expect_base_url,
        )
        note = (
            f"{facts.get('config_path')}（sha256 {facts.get('config_sha256')}；"
            f"model={((facts.get('primary') or {}).get('model'))}，"
            f"base_url={((facts.get('primary') or {}).get('base_url'))}）"
            if not problems else "；".join(problems))
        checks.append(("配置来源", not problems, note))
    except Exception as exc:  # noqa: BLE001 - 包不可导入也是自检结论
        facts = {}
        checks.append(("配置来源", False, f"{type(exc).__name__}: {exc}"))
    out_dir = Path(args.out_dir) if args.out_dir else (
        DEFAULT_OUTPUT_ROOT / _stamp())
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        probe = out_dir / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        checks.append(("输出目录可写", True, str(out_dir)))
    except OSError as exc:  # pragma: no cover - 权限异常的机器
        checks.append(("输出目录可写", False, f"{out_dir}：{exc}"))
    checks.append((
        "ffmpeg 可用（拼成片要用）", _ffmpeg_available(),
        _ffmpeg_path() or "找不到 ffmpeg（`uv sync` 装 imageio-ffmpeg，或放进 PATH）"))
    if str(args.variant).strip().upper() == "FL2VA":
        try:
            image_path = _resolve_frame_image(args)
            checks.append(("真实首帧图", True,
                           f"{image_path}（{image_path.stat().st_size} 字节）"))
        except AcceptanceError as exc:
            checks.append(("真实首帧图", False, str(exc)))
    checks.append((
        "真实模型模式", not args.fake and not _fake_env_enabled(),
        "本轮标为桩管线冒烟" if args.fake or _fake_env_enabled()
        else f"{FAKE_PIPELINE_ENV} 未在这条命令的环境里开启"))
    health = _probe_health(base_url)
    checks.append((
        "服务可达", health is not None,
        f"version={health.get('version')}" if health
        else f"{base_url} 的 /health 不可达（先起服务，或让脚本自己起）"))
    if health is not None and facts:
        problems = health_config_problems(
            str(facts.get("config_path") or ""), health,
            expected_base_url=_normalize_base_url(base_url))
        checks.append(("服务配置一致", not problems,
                       "；".join(problems) or "config_path / 密钥 / base_url 一致"))
    ok = True
    for name, passed, note in checks:
        print(f"[self-check] {'PASS' if passed else 'FAIL'}  {name}：{note}")
        if not passed:
            ok = False
    return 0 if ok else EXIT_SELF_CHECK_FAILED


# ---------------------------------------------------------------- 入口

def _resolve_out_dir(args: argparse.Namespace) -> Path:
    if args.out_dir:
        return Path(args.out_dir).resolve()
    if args.submit_dir:
        # 提交阶段默认接**最近一次** prepare 的目录：用户不必记住时间戳。
        candidates = sorted(
            (path for path in DEFAULT_OUTPUT_ROOT.glob("*")
             if (path / "state.json").is_file()),
            key=lambda path: path.stat().st_mtime, reverse=True,
        )
        _expect(bool(candidates),
                f"{DEFAULT_OUTPUT_ROOT} 下没有 --prepare 产出的 state.json；"
                "请显式传 --out-dir")
        return candidates[0].resolve()
    return (DEFAULT_OUTPUT_ROOT / _stamp()).resolve()


def _load_state(out_dir: Path, args: argparse.Namespace) -> dict[str, Any]:
    path = out_dir / "state.json"
    _expect(path.is_file(), f"找不到 {path}：--submit-dir 要接 --prepare 的 --out-dir")
    state = json.loads(path.read_text(encoding="utf-8"))
    if args.session_id:
        state["session_id"] = args.session_id
    _expect(bool(state.get("session_id")), "state.json 里没有 session_id")
    return state


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="D6 真实长片分段陪跑验收驱动器（#251，手动执行）")
    parser.add_argument("--base-url", default=None,
                        help=f"默认 {DEFAULT_BASE_URL}；--submit-dir 沿用 state.json 记的地址")
    parser.add_argument("--timeout", type=float, default=5400.0,
                        help="单步等待真实模型/用户动作的秒数上限")
    parser.add_argument("--out-dir", default=None,
                        help="证据目录；--submit-dir 默认接最近一次 prepare")
    parser.add_argument("--plot", default=DEFAULT_PLOT)
    parser.add_argument("--duration", type=float, default=DEFAULT_DURATION)
    parser.add_argument("--variant", default=DEFAULT_VARIANT,
                        help="FL2VA（默认，先交开场帧）/ T2VA（无首帧图，仅对照）")
    parser.add_argument("--frame-image", default=None,
                        help="FL2VA 的开场帧图片；默认取 output/accept-d4 记录或"
                             " output/ 下最大的真实图片")
    parser.add_argument("--style", default=DEFAULT_STYLE)
    parser.add_argument("--language", default=DEFAULT_LANGUAGE)
    parser.add_argument("--prepare", action="store_true",
                        help="阶段 A：建会话跑到第 1 段提示词")
    parser.add_argument("--resume", default=None,
                        help="阶段 A 的续跑：复用这个会话标识，不重建、不重跑阶段 1；"
                             "会话处于 failed 时发一次重试任务。用于换模型/排障后接着跑")
    parser.add_argument("--submit-dir", default=None,
                        help="阶段 B：这个目录里的 segment-NN.mp4 按段交回")
    parser.add_argument("--session-id", default=None,
                        help="覆盖 state.json 里的会话标识（排障用）")
    parser.add_argument("--start-server", action="store_true",
                        help="强制自己起服务（端口已有服务时失败）")
    parser.add_argument("--attach", action="store_true",
                        help="强制复用已运行的服务（不可达时失败）")
    parser.add_argument("--no-screenshots", action="store_true",
                        help="不拍浏览器截图")
    parser.add_argument("--fake", action="store_true",
                        help="桩管线冒烟开关：本脚本一律拒绝，保留只为让自检可证")
    parser.add_argument("--self-check", action="store_true",
                        help="只查配置/环境/服务，不建会话、不调模型")
    parser.add_argument("--expect-config", default=None,
                        help="期望的配置文件路径（默认要求是本仓 config/agent.yaml）")
    parser.add_argument("--expect-config-sha256", default=None)
    parser.add_argument("--expect-model", default=None)
    parser.add_argument("--expect-base-url", default=None,
                        help="期望的主模型端点")
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
    if sum(bool(x) for x in (args.prepare, args.submit_dir, args.resume)) != 1:
        print("必须且只能选一种模式：--prepare 或 --resume <会话> 或 --submit-dir <目录>")
        return EXIT_FAILED
    variant = args.variant.strip().upper()
    if variant not in ("FL2VA", "T2VA"):
        print(f"不认识的变体：{args.variant}（本驱动器支持 FL2VA / T2VA）")
        return EXIT_FAILED
    if args.prepare and variant == "T2VA":
        print("[warn] T2VA：第 1 段缺首帧图，提示词会引用不存在的 Picture 1"
              "（「Picture 1 是用户提交的首帧图」）。要按真实首帧验收请用默认 FL2VA。",
              flush=True)
    # 首帧图在**启动被测程序之前**定下来：缺图/夹具图不该先花几十秒起服务再失败。
    if (args.prepare or args.resume) and variant == "FL2VA":
        try:
            args.frame_image = str(_resolve_frame_image(args))
        except AcceptanceError as exc:
            print(f"[fail] {exc}", flush=True)
            return EXIT_FAILED
    if args.fake or _fake_env_enabled():
        print(f"[refuse] 检测到桩管线（--fake 或 {FAKE_PIPELINE_ENV}）："
              "本轮不是真实验收证据，拒绝执行。")
        return EXIT_FAILED

    out_dir = _resolve_out_dir(args)
    out_dir.mkdir(parents=True, exist_ok=True)
    report = Report()
    report.facts["output_dir"] = str(out_dir)
    report.facts["started_at"] = _utc_now()
    report.facts["command"] = args.command or " ".join(sys.argv)
    phase = "prepare" if (args.prepare or args.resume) else "submit"
    code = EXIT_FAILED
    service: ServiceHandle | None = None
    try:
        # 硬性顺序：先断言配置来源，再启动被测程序。
        _assert_config_source(args, report)
        report.facts["sessions_root"] = str(_resolve_sessions_root())
        state: dict[str, Any] = {}
        if args.submit_dir:
            state = _load_state(out_dir, args)
            # 配置来源与 sessions_root 以**本次**断言为准：续跑不能把上一轮的配置事实
            # 当成这一轮的（那正是「配置从哪里来」最容易写错的形态）。
            for key in ("session_id", "plot", "variant", "duration", "style",
                        "language", "variant_note", "frame_image", "segments",
                        "final_film"):
                if state.get(key) is not None:
                    report.facts[key] = state[key]
            previous_root = str(state.get("sessions_root") or "")
            if previous_root and Path(previous_root).resolve() != Path(
                    report.facts["sessions_root"]).resolve():
                print(f"[warn] state.json 记的 sessions_root 与本次配置不一致："
                      f"{previous_root} → {report.facts['sessions_root']}",
                      flush=True)
        base_url = _normalize_base_url(
            args.base_url or str(state.get("base_url") or "") or DEFAULT_BASE_URL)
        report.facts["base_url"] = base_url
        service = ensure_service(base_url, out_dir, args, report)
        with httpx.Client(timeout=httpx.Timeout(60.0)) as client:
            _assert_service_health(client, service.base_url, report.facts)
            token = _session_token(client, service.base_url)
            if args.prepare or args.resume:
                code = run_prepare(client, service.base_url, token, args, report)
            else:
                code = run_submit(client, service.base_url, token, args, report)
    except AcceptanceError as exc:
        if str(exc) not in report.failures:
            report.failures.append(str(exc))
        print(f"[fail] {exc}", flush=True)
    except Exception as exc:  # noqa: BLE001 - 未预期异常也要落一份报告
        report.failures.append(f"未预期异常：{type(exc).__name__}: {exc}")
        print(f"[fail] 未预期异常：{type(exc).__name__}: {exc}", flush=True)
    finally:
        if service is not None:
            service.stop()
        report.facts["finished_at"] = _utc_now()
        try:
            _write_report_files(report, out_dir, phase)
        except Exception as exc:  # noqa: BLE001 - 报告写盘失败至少要说出来
            print(f"[warn] 报告写盘失败：{type(exc).__name__}: {exc}", flush=True)
    if report.failures:
        print("验收未全部通过：")
        for failure in report.failures:
            print(f"- {failure}")
        print(f"报告：{out_dir}")
        return EXIT_FAILED if code == EXIT_COMPLETED else code
    if code == EXIT_WAITING_USER:
        print(f"当前停在第 {report.facts.get('waiting_for')} 段："
              f"按 handoff.md 生成视频后重跑 --submit-dir。报告：{out_dir}")
    else:
        print(f"全部步骤通过：{len(report.steps)} 步；报告在 {out_dir}")
    return code


def _resolve_sessions_root() -> Path:
    try:
        from minimax_h3_prompt.config import config

        return Path(str(config.sessions_root)).resolve()
    except Exception:  # noqa: BLE001 - 配置读不到不该挡住验收本身
        return (ROOT / "output" / "sessions").resolve()


if __name__ == "__main__":
    raise SystemExit(main())
