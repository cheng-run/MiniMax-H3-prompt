#!/usr/bin/env python
"""#242 D5-7 干净环境验收驱动器（手动执行，不进 CI、不进 ``check.sh``）。

它把 spec #235 §11/§12/§17 的验收按四段跑完，**对着打包产物**而不是源码：

- **A 首次运行（缺密钥形态）**：``APPDATA`` 指向空目录 + ``env -i`` 起 EXE →
  自动生成 ``data/config/agent.yaml``、``/health`` 报缺密钥、概览页给出密钥文件
  绝对路径与「复制路径」按钮；关窗后进程以 0 退出。
- **B 真实闭环**：真实 LLM、真实窗口。建会话 → 审查 → 修订一轮 → approve →
  上传真实图片作首/尾帧 → 阶段 2 → 产物复制/下载。用 Playwright
  ``connect_over_cdp`` 驱动 **pywebview 的真实窗口**（spec §12 的 CDP 主路径）。
- **C 生命周期与浏览器并存**：窗口开着时外部 Chromium 能列出会话并打开详情、
  未带凭据的 API 仍是 401；同一 ``data/`` 的第二个实例被拒并提示；显式端口被占用
  时报错退出并说明；关窗退出码 0；重启后会话与产物仍在。
- **D 诊断包**：窗口里打开 ``/diagnostics`` → 读导出前清单 → 一键导出 → 落盘 zip →
  用脚本断言条目齐全、不含密钥值、不含素材媒体、不含 ``.env``。

命令、耗时、截图与产物路径落 ``output/accept-d5/<UTC 时间戳>/``，报告形态沿用
``accept_d4.py``。证据里**不出现令牌，也不出现密钥值**（只记命中数与变量名）。

用法（在 ticket-242 工作树里，EXE 由 ``scripts/build_exe.py`` 在集成分支构建）::

    PYTHONIOENCODING=utf-8 PYTHONPATH=src \\
        .venv/Scripts/python.exe scripts/accept_d5.py \\
        --zip worktree/integration-d5/build-exe/MiniMax-H3-prompt.zip \\
        --env-file .env \\
        --config <你的仓库根>/config/agent.yaml \\
        --image output/issue126-evidence/frames/a1-ref.png \\
        --image-last output/issue126-evidence/frames/sheet-1026.png \\
        --out-dir output/accept-d5/<UTC>

``--config`` **默认不传**：不覆盖 ``<data-root>/config/agent.yaml``，直接用 EXE 首次
启动生成的内置模板（模板换机器/换网关都只改命令行，不把本机路径写进仓库）。真实模型
闭环通常要显式传主树的 ``config/agent.yaml``；覆盖发生在起 EXE 之前，启动后核对日志里
不得出现旧端点 ``api.minimax.cn``。

``--rehearse`` 给驱动本身做冒烟：**只在子进程**里打开桩管线
（``MINIMAX_FAKE_PIPELINE=1``），交互完全相同、几秒跑完，报告会标明「不是真实
模型证据」。``--self-check`` 只查环境与结构（zip、图片、``.env``、Playwright、
端口可用），不解压、不起进程、不调模型。

为什么是独立脚本而不是 pytest：它起真实窗口、跑真实模型，一轮要几十分钟，还要
本机的 ``.env`` 与真实图片——real LLM 不进 CI（#204 §10 / spec §12）。
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes
import hashlib
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import quote, unquote

import httpx

ROOT = Path(__file__).resolve().parents[1]
# 脱敏口径只有 ``logging_setup`` 一份（服务端日志、诊断包与验收扫描共用）：脚本自己
# 再抄后缀表与排序，会出现「脚本判过、服务端日志判不过」的假阴性（#243 两轴复审）。
# 直接跑脚本时不要求外面已设 ``PYTHONPATH``：把本仓 ``src`` 补到最前面即可。
_SRC = ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from minimax_h3_prompt.logging_setup import secret_values  # noqa: E402  先补路径再导入

EXE_NAME = "MiniMax-H3-prompt.exe"
PORTABLE_NAME = "MiniMax-H3-prompt"
APP_TITLE = "MiniMax H3 Prompt"
DEFAULT_DEBUG_PORT = 9601
DEFAULT_PLOT = "雨夜街口，一位撑黑伞的女性走过积水，霓虹在积水里反光，写实电影感。"
DEFAULT_REVISION = "结局收束更克制：人物停在旧楼门口，抬手关掉门廊的灯，不要回头。"
REVISION_LAYER = "剧情分镜"
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp"})
MEDIA_SUFFIXES = frozenset({
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".mp4", ".mov", ".webm",
    ".mkv", ".avi", ".mp3", ".wav", ".m4a", ".flac", ".ogg",
})
# ``env -i`` 的清单：spike 实测漏了 SystemDrive 时，WebView2 会把 ``%SystemDrive%``
# 当字面量拼进路径、在当前工作目录造缓存目录（#236 第 5 节）。别名拼写都留着。
ENV_ALLOWED = (
    "SYSTEMROOT", "SystemRoot", "SYSTEMDRIVE", "SystemDrive",
    "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "windir",
    "LOCALAPPDATA", "USERPROFILE", "TEMP", "TMP",
    "USERNAME", "USERDOMAIN", "COMPUTERNAME",
    "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS",
)
SYSTEM_PATH = r"C:\Windows\system32;C:\Windows;C:\Windows\System32\Wbem"
STARTUP_RE = re.compile(
    r"\[启动\] 地址=(http://127\.0\.0\.1:(\d+)/)；数据根=(.+?)；形态=([^；\s]+)")
WINDOW_PORT_RE = re.compile(r"（端口 (\d+)）")
BUNDLE_URL_PART = "/api/v1/diagnostics/bundle"
PLAYWRIGHT_INSTALL_HINT = "uv sync && uv run playwright install chromium（或设 PW_CHANNEL=chrome）"

# 真实配置由 ``--config`` **显式**传入：默认不覆盖 ``data/config/agent.yaml``，
# 用 EXE 首次启动自己生成的内置模板（换机器/换配置都只改命令行，不把本机路径写进仓库）。
# 旧端点：日志里一旦出现就是「用了错误配置」，立即失败（不靠人看 402 文案猜）。
FORBIDDEN_MODEL_HOST = "api.minimax.cn"
EXPECTED_CONFIG_MARK = "dashscope"

WM_CLOSE = 0x0010



class AcceptanceError(RuntimeError):
    """一步不达标：记录能记的步骤，最后整体退出码非零。"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _human_bytes(count: int) -> str:
    value = float(count)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{count} B"


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise AcceptanceError(message)


def _normalize_newlines(text: str) -> str:
    """Windows 剪贴板会把换行规范成 CRLF，下载与会话产物是 LF（#234 实测）。"""
    return text.replace("\r\n", "\n").replace("\r", "\n")


# --------------------------------------------------------------- 报告与 AC 表

@dataclass
class Report:
    facts: dict[str, Any] = field(default_factory=dict)
    steps: list[dict[str, Any]] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    ac: dict[str, dict[str, str]] = field(default_factory=dict)

    def step(self, name: str, status: str, ms: int, note: str = "") -> None:
        self.steps.append({"name": name, "status": status, "ms": ms, "note": note})
        if status == "failed":
            self.failures.append(f"{name}：{note}")

    def run(self, name: str, action: Callable[[], str]) -> str:
        """跑一步并计时；异常原样抛出，但先把失败记进报告。

        每步开始就打印（``flush=True``）：真实模型一轮可达几十分钟，输出重定向时
        块缓冲会让整段静默（#227 实测跑 20 分钟日志仍是 0 字节）。
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

    def ac_set(self, key: str, status: str, evidence: str) -> None:
        self.ac[key] = {"status": status, "evidence": evidence}


AC_KEYS = tuple(f"D5-AC{index}" for index in range(1, 17))
AC_TITLES = {
    "D5-AC1": "干净 Windows 11 上解压 → 双击 EXE → 窗口打开、概览页可用",
    "D5-AC2": "冻结形态随机端口；日志、窗口标题、概览页三处可得；--port 可覆盖；占用时报错退出",
    "D5-AC3": "同一 data/ 的第二个实例被拒并提示，只有一个服务在跑",
    "D5-AC4": "关窗后进程退出；重启后会话列表、产物、已生成内容仍在",
    "D5-AC5": "窗口开着时系统浏览器能访问同一地址；鉴权未放宽",
    "D5-AC6": "只读资源来自 EXE 内部，sessions/output/logs/config 落在 EXE 同级 data/",
    "D5-AC7": "首次运行自动生成 data/config/agent.yaml；缺 key 提示含路径与复制入口，不崩不白屏",
    "D5-AC8": "密钥只从 %APPDATA%\\MiniMax-H3-prompt\\.env 读；data/ 不放密钥；日志脱敏",
    "D5-AC9": "改配置后重启生效；界面提示与「不热重载」一致",
    "D5-AC10": "日志落 data/logs/ 按日滚动；诊断包含 §8.2 清单且不含密钥与媒体；自检通过",
    "D5-AC11": "诊断包可从 /diagnostics 一键导出并下载成功",
    "D5-AC12": "EXE 内真实跑通 base 短视频闭环（真实 LLM），含审查修订、帧提交、阶段 2、产物复制",
    "D5-AC13": "CI 新增 exe-build（windows-latest）产出 artifact；失败会红",
    "D5-AC14": "CDP 驱动真实窗口完成闭环；不可行则退兜底并标注",
    "D5-AC15": "无安装器、无自动更新、无托盘、无设置页、未签名、不改向导",
    "D5-AC16": "ADR 0035 入库、docs/agents/ 补冻结形态说明；无新领域词条",
}


def render_ac_rows(ac: Mapping[str, Mapping[str, str]]) -> list[str]:
    """AC 表（Markdown 行）；没登记的条目写「未验」，不静默漏行。"""
    rows = ["| AC | 结论 | 依据 |", "|---|---|---|"]
    for key in AC_KEYS:
        entry = ac.get(key) or {}
        status = str(entry.get("status") or "未验")
        evidence = str(entry.get("evidence") or "（本次运行没有留下证据）")
        rows.append(f"| {key} {AC_TITLES[key]} | {status} | {evidence} |")
    return rows


# --------------------------------------------------------------- .env 与子进程环境

def parse_env_file(path: Path) -> dict[str, str]:
    """极简 ``KEY=VALUE`` 解析（只为取密钥值做脱敏扫描，不替代 dotenv）。

    支持 ``export KEY=…``、引号与行内 ``#`` 注释；值不写进报告。
    """
    values: dict[str, str] = {}
    if not Path(path).is_file():
        return values
    for raw in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value[:1] in {'"', "'"}:
            # 引号值：取到配对的引号为止，引号后的 ``# 注释`` 丢掉。
            quote_char = value[0]
            end = value.find(quote_char, 1)
            value = value[1:end] if end != -1 else value.strip(quote_char)
        else:
            value = value.split(" #", 1)[0].strip()
        if key:
            values[key] = value
    return values


def restricted_env(
    apdata: Path, base_environ: Mapping[str, str] | None = None,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """``env -i`` 的等价物：只保留 Windows/WebView2 必需的变量。

    ``PATH`` 只给 ``system32``/``Windows``，``APPDATA`` 指向本次验收的目录——
    这样「有没有装 Python／Node」「有没有别的 .env」都影响不到被测进程。
    """
    base = dict(os.environ if base_environ is None else base_environ)
    env = {name: base[name] for name in ENV_ALLOWED if name in base}
    extra = dict(extra or {})
    env.update(extra)
    env["APPDATA"] = str(apdata)
    env["PATH"] = SYSTEM_PATH
    for alias in ("SYSTEMROOT", "SystemRoot"):
        env.setdefault(alias, base.get("SYSTEMROOT") or base.get("SystemRoot") or r"C:\Windows")
    for alias in ("SYSTEMDRIVE", "SystemDrive"):
        env.setdefault(alias, base.get("SYSTEMDRIVE") or base.get("SystemDrive") or "C:")
    for alias in ("TEMP", "TMP"):
        env.setdefault(alias, base.get(alias) or tempfile.gettempdir())
    return env


def env_summary(env: Mapping[str, str]) -> dict[str, str]:
    """报告用：只列变量名；``APPDATA``/``PATH`` 记值（不是密钥）。"""
    return {
        "variables": sorted(env),
        "APPDATA": env.get("APPDATA", ""),
        "PATH": env.get("PATH", ""),
    }


# ------------------------------------------------------------ 启动日志与地址

def parse_startup_lines(text: str) -> list[dict[str, Any]]:
    """从日志文本里取启动行（``StartupInfo.startup_line`` 的字段名就是契约）。"""
    return [
        {"base_url": match.group(1), "port": int(match.group(2)),
         "data_root": match.group(3), "form": match.group(4)}
        for match in STARTUP_RE.finditer(text)
    ]


def snapshot_logs(log_dir: Path) -> dict[str, int]:
    """启动前记下每个日志文件的字节数：之后只认**新追加**的启动行。"""
    return {
        path.name: path.stat().st_size
        for path in Path(log_dir).glob("app-*.log")
        if path.is_file()
    }


def latest_startup(log_dir: Path, baseline: Mapping[str, int]) -> tuple[dict[str, Any], Path] | None:
    """读日志里新追加的启动行；同时返回它所在的文件（证据路径）。"""
    found: tuple[dict[str, Any], Path] | None = None
    for path in sorted(Path(log_dir).glob("app-*.log"), key=lambda item: item.stat().st_mtime):
        try:
            offset = int(baseline.get(path.name, 0))
            with path.open("rb") as handle:
                handle.seek(offset)
                text = handle.read().decode("utf-8", errors="replace")
        except OSError:
            continue
        lines = parse_startup_lines(text)
        if lines:
            found = (lines[-1], path)
    return found


def wait_for_startup(
    log_dir: Path, baseline: Mapping[str, int], proc: subprocess.Popen,
    timeout: float,
) -> tuple[dict[str, Any], Path]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = latest_startup(log_dir, baseline)
        if found is not None:
            return found
        if proc.poll() is not None:
            raise AcceptanceError(
                f"进程在写出启动行之前退出了（退出码 {proc.returncode}）；"
                f"见 {log_dir} 与运行日志")
        time.sleep(0.2)
    raise AcceptanceError(f"{timeout:g}s 内没有等到启动行（{log_dir}）")


# ------------------------------------------------------- Win32 窗口（标题/关窗/提示框）

def _user32():
    return ctypes.windll.user32


def window_info(pid: int) -> list[tuple[int, str]]:
    """该进程的可见顶层窗口 ``(hwnd, 标题)``。

    用来验证 **窗口标题里的实际端口**（spec §5.2）与读取启动失败时的原生消息框
    ——这两样都不在网页 DOM 里，CDP 看不到。
    """
    results: list[tuple[int, str]] = []
    user32 = _user32()
    callback_type = ctypes.WINFUNCTYPE(ctypes.wintypes.BOOL, ctypes.c_void_p, ctypes.c_void_p)

    def callback(hwnd, _lparam):
        window_pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
        if window_pid.value != pid:
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        if buffer.value:
            results.append((int(hwnd), buffer.value))
        return True

    user32.EnumWindows(callback_type(callback), 0)
    return results


def child_texts(hwnd: int) -> list[str]:
    """窗口内子控件的文本（消息框的正文在 Static 子控件里）。"""
    results: list[str] = []
    user32 = _user32()
    callback_type = ctypes.WINFUNCTYPE(ctypes.wintypes.BOOL, ctypes.c_void_p, ctypes.c_void_p)

    def callback(child, _lparam):
        length = user32.GetWindowTextLengthW(child)
        if length > 0:
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(child, buffer, length + 1)
            if buffer.value:
                results.append(buffer.value)
        return True

    user32.EnumChildWindows(hwnd, callback_type(callback), 0)
    return results


def window_title_with_port(pid: int, timeout: float) -> tuple[int, str] | None:
    """等到出现「（端口 N）」的窗口标题，返回 ``(hwnd, 标题)``。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for hwnd, title in window_info(pid):
            if WINDOW_PORT_RE.search(title):
                return hwnd, title
        time.sleep(0.2)
    return None


def focus_window(hwnd: int) -> None:
    """把窗口提到前台（best-effort）：剪贴板与键盘焦点在后台窗口上不可靠。"""
    try:
        user32 = _user32()
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
    except Exception:  # noqa: BLE001 - 前台锁拒绝时无解，不影响后续断言
        pass


def post_close(hwnd: int) -> bool:
    """等价于点窗口右上角的 X：给窗口投一条 ``WM_CLOSE``。"""
    return bool(_user32().PostMessageW(hwnd, WM_CLOSE, 0, 0))


def read_clipboard_text() -> str | None:
    """读 Windows 剪贴板（CF_UNICODETEXT）；WebView2 里剪贴板权限被拒时的兜底。"""
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    CF_UNICODETEXT = 13
    user32.GetClipboardData.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    if not user32.OpenClipboard(None):
        return None
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return None
        try:
            return ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


# --------------------------------------------------------------- 进程与端口

def port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


@dataclass
class Instance:
    """一次 EXE 运行：进程、实际地址、数据根与启动日志。"""

    name: str
    proc: subprocess.Popen
    port: int
    base_url: str
    data_root: Path
    log_path: Path
    stdout_path: Path
    stderr_path: Path

    @property
    def pid(self) -> int:
        return int(self.proc.pid)

    def facts(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "pid": self.pid,
            "port": self.port,
            "base_url": self.base_url,
            "data_root": str(self.data_root),
            "log_path": str(self.log_path),
            "stdout": str(self.stdout_path),
            "stderr": str(self.stderr_path),
        }


def wait_exit(proc: subprocess.Popen, timeout: float) -> int | None:
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return None


def kill_tree(proc: subprocess.Popen, timeout: float = 10.0) -> int | None:
    """先礼后兵：terminate → kill；返回拿到的退出码（拿不到就 None）。"""
    if proc.poll() is None:
        proc.terminate()
        code = wait_exit(proc, timeout)
        if code is None:
            proc.kill()
            code = wait_exit(proc, timeout)
        return code
    return proc.returncode


# ------------------------------------------------------------------ HTTP 小工具

def health(client: httpx.Client, base_url: str) -> dict[str, Any]:
    response = client.get(f"{base_url.rstrip('/')}/api/v1/health")
    _expect(response.status_code == 200, f"/health 返回 {response.status_code}")
    return response.json()


def fetch_token(client: httpx.Client, base_url: str) -> str:
    """引导令牌：只记状态码，不记响应体（ADR 0033 第 7 条）。"""
    response = client.get(f"{base_url.rstrip('/')}/api/v1/auth/token")
    _expect(response.status_code == 200, f"引导令牌返回 {response.status_code}")
    token = str(response.json().get("token") or "")
    _expect(bool(token), "引导令牌为空")
    return token


def session_detail(
    client: httpx.Client, base_url: str, token: str, session_id: str,
) -> dict[str, Any]:
    response = client.get(
        f"{base_url.rstrip('/')}/api/v1/sessions/{quote(session_id, safe='/')}",
        headers={"X-Workbench-Token": token},
    )
    _expect(response.status_code == 200, f"取会话快照返回 {response.status_code}")
    return response.json()


def wait_health(client: httpx.Client, base_url: str, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return health(client, base_url)
        except Exception as exc:  # noqa: BLE001 - 服务还没起来
            last = exc
            time.sleep(0.2)
    raise AcceptanceError(f"{base_url} 在 {timeout:g}s 内没有返回健康响应：{last}")


# ------------------------------------------------------------------ 驱动上下文

@dataclass
class Context:
    """一次验收运行的共享状态（参数、目录、报告、Playwright）。"""

    args: argparse.Namespace
    report: Report
    out_dir: Path
    zip_path: Path
    portable: Path
    work_dir: Path
    apdata_empty: Path
    extra_env: dict[str, str] = field(default_factory=dict)
    playwright: Any = None
    instances: list[Instance] = field(default_factory=list)

    @property
    def exe(self) -> Path:
        return self.portable / EXE_NAME

    @property
    def data_root(self) -> Path:
        return self.portable / "data"

    def start(
        self, name: str, apdata: Path, *, extra_env: Mapping[str, str] | None = None,
        exe_args: Sequence[str] | None = None, wait: float = 90.0,
    ) -> Instance:
        """以 ``env -i`` 起 EXE，等它写出启动行（实际端口从日志读）。

        默认带上 ``--debug-port``：CDP 驱动真实窗口靠它（spec §12 主路径）。
        """
        if exe_args is None:
            exe_args = ("--port", "0", "--debug-port", str(self.args.debug_port))
        out = self.out_dir / "runs"
        out.mkdir(parents=True, exist_ok=True)
        stdout_path = out / f"{name}.stdout.log"
        stderr_path = out / f"{name}.stderr.log"
        env = restricted_env(
            apdata, extra={**self.extra_env, **(extra_env or {})})
        baseline = snapshot_logs(self.data_root / "logs")
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            proc = subprocess.Popen(
                [str(self.exe), *exe_args], cwd=str(self.portable), env=env,
                stdout=stdout, stderr=stderr, stdin=subprocess.DEVNULL,
            )
        try:
            info, log_path = wait_for_startup(self.data_root / "logs", baseline, proc, wait)
        except Exception:
            kill_tree(proc)
            raise
        instance = Instance(
            name=name, proc=proc, port=int(info["port"]), base_url=str(info["base_url"]),
            data_root=Path(info["data_root"]), log_path=log_path,
            stdout_path=stdout_path, stderr_path=stderr_path,
        )
        self.instances.append(instance)
        self.report.facts.setdefault("instances", []).append(instance.facts())
        return instance

    def launch_expect_failure(
        self, name: str, apdata: Path, exe_args: Sequence[str],
        *, extra_env: Mapping[str, str] | None = None, timeout: float = 30.0,
    ) -> dict[str, Any]:
        """起一个**预期失败**的实例，读它的原生消息框（标题 + 正文），然后收掉进程。"""
        out = self.out_dir / "runs"
        out.mkdir(parents=True, exist_ok=True)
        stdout_path = out / f"{name}.stdout.log"
        stderr_path = out / f"{name}.stderr.log"
        env = restricted_env(
            apdata, extra={**self.extra_env, **(extra_env or {})})
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            proc = subprocess.Popen(
                [str(self.exe), *exe_args], cwd=str(self.portable), env=env,
                stdout=stdout, stderr=stderr, stdin=subprocess.DEVNULL,
            )
        deadline = time.monotonic() + timeout
        best_title = ""
        best_body = ""
        while time.monotonic() < deadline:
            for hwnd, title in window_info(int(proc.pid)):
                if title != APP_TITLE:
                    continue
                bodies = child_texts(hwnd)
                if not bodies:
                    continue
                body = "\n".join(bodies)
                # 消息框是先建按钮再填正文的：只拿到「确定」时继续等，别提前返回。
                if len(body) > len(best_body):
                    best_title, best_body = title, body
            if len(best_body) > 40:
                break
            time.sleep(0.2)
        dialog = (
            {"title": best_title, "body": best_body} if best_body else None)
        code = kill_tree(proc)
        return {
            "name": name,
            "pid": int(proc.pid),
            "dialog": dialog,
            "exit_code_after_kill": code,
            "stdout": str(stdout_path),
            "stderr": str(stderr_path),
        }

    def connect(self, instance: Instance, timeout: float = 30.0):
        """``connect_over_cdp`` 连上真实窗口，返回 ``(browser, page)``。"""
        _expect(self.args.debug_port is not None, "没有给 --debug-port，无法用 CDP")
        deadline = time.monotonic() + timeout
        last: Exception | None = None
        while time.monotonic() < deadline:
            try:
                browser = self.playwright.chromium.connect_over_cdp(
                    f"http://127.0.0.1:{self.args.debug_port}")
            except Exception as exc:  # noqa: BLE001 - 调试端口还没开
                last = exc
                time.sleep(0.5)
                continue
            pages = [page for context in browser.contexts for page in context.pages]
            page = next(
                (item for item in pages if item.url.startswith(instance.base_url)),
                pages[0] if pages else None,
            )
            if page is None:
                try:
                    browser.close()
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(0.5)
                continue
            return browser, page
        raise AcceptanceError(f"CDP 连不上真实窗口（127.0.0.1:{self.args.debug_port}）：{last}")

    def close_window(self, instance: Instance, page: Any | None) -> dict[str, Any]:
        """关窗 → 等进程退出：先 WM_CLOSE（等价于点 X），再 CDP page.close()，最后强杀。

        判据是「进程以 0 退出」；用了哪种关法也进证据。
        """
        tried: list[str] = []
        code: int | None = None
        title = window_title_with_port(instance.pid, 5.0)
        if title is not None and post_close(title[0]):
            tried.append("wm-close")
            code = wait_exit(instance.proc, 30.0)
        if code is None and page is not None:
            try:
                page.close()
                tried.append("cdp-page-close")
            except Exception:  # noqa: BLE001 - 连接已断也算一条
                tried.append("cdp-page-close-failed")
            code = wait_exit(instance.proc, 30.0)
        if code is None:
            tried.append("force-kill")
            code = kill_tree(instance.proc)
        else:
            # 进程已退出：把残留句柄收干净（幂等）。
            instance.proc.poll()
        return {"methods": tried, "exit_code": code}


# ------------------------------------------------------------------ 截图与等待

def shot(page: Any, out_dir: Path, name: str, facts: dict[str, Any]) -> str:
    """截图是**证据**，不是判据：拍不到就记一笔继续走，不能把整轮验收判失败。

    2026-11-12 实测：真实修订重跑后整页截图在 WebView2 上卡满 30s（`waiting for
    fonts` 之后不返回），把已经成功的 B1/B2 全带红了。先拍视口（便宜、稳），
    再退整页；两次都失败只记 ``screenshot_failures``。
    """
    path = out_dir / "screenshots" / f"{name}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    for options in ({"full_page": False}, {"full_page": True}):
        try:
            page.screenshot(path=str(path), timeout=20_000, **options)
            facts.setdefault("screenshots", []).append(str(path))
            return str(path)
        except Exception as exc:  # noqa: BLE001 - 证据缺失不该中断验收
            errors.append(f"{type(exc).__name__}: {exc}")
            print(f"[shot] 截图失败（{name}，{options}）：{errors[-1]}", flush=True)
    facts.setdefault("screenshot_failures", []).append(
        {"name": name, "errors": errors})
    return ""


def wait_text(page: Any, text: str, timeout_ms: int, exact: bool = False) -> None:
    page.get_by_text(text, exact=exact).first.wait_for(state="visible", timeout=timeout_ms)


def wait_review_gate(page: Any, timeout_ms: int, *, poll_ms: int = 500) -> str:
    """等阶段 1 的人工闸门；会话变「失败」就立刻停下，不白等整步超时。

    真实模型的服务端错误（额度不足、网关 5xx）只体现在会话状态与日志里：
    页面停在「失败」+「重试」，永远等不到「等待人工」（2026-11-12 实测 402）。
    """
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if page.get_by_text("等待人工").count():
            return "awaiting-review"
        if page.get_by_text("失败", exact=True).count():
            raise AcceptanceError(
                "阶段 1 执行失败（会话状态=失败）。服务端错误正文在 "
                "data/logs 与 session-state.json 的 jobs[].error_code；"
                "常见原因：模型额度不足或网关报错。")
        page.wait_for_timeout(poll_ms)
    raise AcceptanceError(
        f"等了 {timeout_ms / 1000:g}s 仍没有出现阶段 1 人工闸门")


def element_texts(page: Any, selector: str) -> list[str]:
    return [text.strip() for text in page.locator(selector).all_inner_texts()]


def navigate(page: Any, url: str, timeout_ms: int, attempts: int = 3) -> None:
    """带重试的导航。

    真实窗口刚被 CDP 接上时，应用自己可能还在完成首次加载/重定向，``goto`` 会拿到
    ``net::ERR_ABORTED``（2026-11-12 演练实测）；重试即可，别把它当成产品失败。
    """
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            return
        except Exception as exc:  # noqa: BLE001 - 首次加载被打断
            last = exc
            page.wait_for_timeout(500)
    raise AcceptanceError(f"导航到 {url} 失败（重试 {attempts} 次）：{last}")


# ------------------------------------------------------------------ accept_d4 复用

def load_accept_d4():
    """复用 D4 驱动器的证据函数（会话快照、帧读图、真实图片挑选）。"""
    path = ROOT / "scripts" / "accept_d4.py"
    spec = importlib.util.spec_from_file_location("accept_d5_accept_d4", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------- 解压与结构检查

def extract_zip(zip_path: Path, target_root: Path) -> Path:
    """解压 zip 到 ``target_root``，返回便携目录。

    不调用 ``extractall`` 直接落盘：先校验成员路径都在目标根之内（zip slip），
    再把 ``MiniMax-H3-prompt/`` 这一层剥掉之前的目录原样保留。
    """
    target_root.mkdir(parents=True, exist_ok=True)
    resolved_root = target_root.resolve()
    with zipfile.ZipFile(zip_path) as archive:
        for member in archive.infolist():
            destination = (target_root / member.filename).resolve()
            if not str(destination).startswith(str(resolved_root)):
                raise AcceptanceError(f"zip 成员越界：{member.filename}")
        archive.extractall(target_root)
    portable = target_root / PORTABLE_NAME
    _expect((portable / EXE_NAME).is_file(), f"解压后没有 {EXE_NAME}：{portable}")
    _expect((portable / "_internal").is_dir(), f"解压后没有 _internal/：{portable}")
    _expect((portable / "README.txt").is_file(), f"解压后没有 README.txt：{portable}")
    return portable


def zip_facts(zip_path: Path) -> dict[str, Any]:
    with zipfile.ZipFile(zip_path) as archive:
        names = archive.namelist()
    top_level = sorted({name.split("/")[1] for name in names if name.count("/") >= 1})
    media = [name for name in names if Path(name).suffix.lower() in MEDIA_SUFFIXES]
    env_entries = [name for name in names if Path(name).name == ".env"]
    installers = [
        name for name in names
        if Path(name).suffix.lower() in {".msi", ".msix", ".appx"}
        # 只认交付目录顶层的 exe（``_internal`` 里的 ffmpeg 等运行时组件不是安装器）。
        or (Path(name).suffix.lower() == ".exe"
            and len(Path(name).parts) == 2
            and Path(name).name != EXE_NAME)
    ]
    return {
        "path": str(zip_path),
        "bytes": zip_path.stat().st_size,
        "sha256": _sha256(zip_path),
        "entries": len(names),
        "top_level": top_level,
        "media_entries": media[:10],
        "env_entries": env_entries[:10],
        "installer_entries": installers[:10],
    }


def signature_status(exe: Path) -> str:
    """Authenticode 状态（best-effort）：未签名是 spec §4 的既定形态，不是缺陷。"""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             f"(Get-AuthenticodeSignature -LiteralPath '{exe}').Status"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"不可判定：{exc}"
    output = (result.stdout or "").strip()
    return output or f"不可判定（退出码 {result.returncode}）"


def structure_facts(repo_root: Path) -> dict[str, Any]:
    """AC15/AC16 的结构证据：不新增安装器/托盘/设置页，文档齐备。"""
    def read(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    router = read(repo_root / "frontend" / "src" / "router" / "index.ts")
    routes = re.findall(r"path: '([^']+)'", router)
    sources = "\n".join(
        read(path) for path in (repo_root / "src").rglob("*.py")
    )
    build_script = read(repo_root / "scripts" / "build_exe.py")
    adr = sorted((repo_root / "docs" / "adr").glob("0035-*.md"))
    frontend_doc = read(repo_root / "docs" / "agents" / "frontend.md")
    workbench_doc = read(repo_root / "docs" / "agents" / "workbench.md")
    # 「不改向导」：与 main 比，向导与 CLI 入口不该出现在改动清单里。
    try:
        diff = subprocess.run(
            ["git", "-C", str(repo_root), "diff", "--name-only", "main", "--",
             "src/minimax_h3_prompt/ui/wizard.py", "src/minimax_h3_prompt/launch.py"],
            capture_output=True, text=True, timeout=30, check=False)
        wizard_changed = [line for line in (diff.stdout or "").splitlines() if line.strip()]
    except (OSError, subprocess.SubprocessError):
        wizard_changed = ["（git 不可用，未验证）"]
    return {
        "routes": routes,
        "has_settings_page": any("setting" in route for route in routes),
        "tray_mentions": len(re.findall(r"pystray|systray|托盘", sources)),
        "updater_mentions": len(re.findall(r"auto[_-]?update|updater", sources, re.IGNORECASE)),
        "build_signs": bool(re.search(r"signtool|signtool\.exe|signing", build_script, re.IGNORECASE)),
        "adr_0035": [str(path) for path in adr],
        "frontend_doc_exe_section": "EXE 打包" in frontend_doc,
        "workbench_freeze_section": "冻结形态（EXE）" in workbench_doc,
        "wizard_changed_vs_main": wizard_changed,
    }


# ------------------------------------------------------------------ 步骤 A

def step_a(ctx: Context) -> None:
    """首次运行（缺密钥形态）：自动生成配置、缺密钥提示、关窗退出 0。"""
    report = ctx.report
    args = ctx.args
    timeout_ms = int(args.timeout * 1000)
    _expect(port_is_free(args.debug_port), f"调试端口 {args.debug_port} 已被占用")

    ctx.apdata_empty.mkdir(parents=True, exist_ok=True)
    # 空的 APPDATA：模拟「这台机器还没配过密钥」。
    instance = ctx.start("A-first-run", ctx.apdata_empty)
    report.facts["A"] = {"instance": instance.facts()}

    def assert_config_generated() -> str:
        config = ctx.data_root / "config" / "agent.yaml"
        _expect(config.is_file(), f"首次启动没有生成 {config}")
        template = ctx.portable / "_internal" / "config" / "agent.yaml"
        _expect(template.is_file(), f"包里没有内置模板：{template}")
        _expect(
            config.read_bytes() == template.read_bytes(),
            "生成的 data/config/agent.yaml 与内置模板不是逐字节一致",
        )
        others = sorted(path.name for path in (ctx.data_root / "config").glob("*.yaml"))
        logs = sorted(path.name for path in (ctx.data_root / "logs").glob("app-*.log"))
        _expect(bool(logs), f"data/logs 下没有日志文件：{ctx.data_root / 'logs'}")
        # 密钥不进作品目录（spec §4/§7）：data/ 下不许出现 .env。
        stray_env = sorted(path for path in ctx.data_root.rglob(".env") if path.is_file())
        _expect(not stray_env, f"data/ 下出现了 .env：{stray_env}")
        report.facts["A"]["config"] = {
            "path": str(config), "bytes": config.stat().st_size,
            "templates": others, "logs": logs, "stray_env": [],
        }
        return f"agent.yaml={config.stat().st_size} 字节，与内置模板逐字节一致；配置 {len(others)} 份；日志 {logs[0]}"

    report.run("A1 首次启动自动生成 data/config/agent.yaml", assert_config_generated)

    def assert_health() -> str:
        with httpx.Client(timeout=httpx.Timeout(30.0)) as client:
            payload = wait_health(client, instance.base_url, 30.0)
        missing = list(payload.get("missing_keys") or [])
        env_path = str(payload.get("env_path") or "")
        config_path = str(payload.get("config_path") or "")
        report.facts["A"]["health"] = dict(payload)
        _expect(missing, "缺密钥形态下 /health 的 missing_keys 是空的")
        _expect(Path(env_path).is_absolute(), f"env_path 不是绝对路径：{env_path!r}")
        _expect(
            Path(env_path).is_relative_to(ctx.apdata_empty.resolve()),
            f"env_path 不在本次的空 APPDATA 下：{env_path}",
        )
        _expect(
            Path(config_path).is_relative_to(ctx.data_root.resolve()),
            f"config_path 不在 EXE 同级的 data/ 下：{config_path}",
        )
        _expect(payload.get("status") == "ok", f"/health 状态异常：{payload.get('status')}")
        return f"missing_keys={missing}；env_path={env_path}；data_root={payload.get('data_root')}"

    report.run("A2 /health 报缺密钥与密钥文件绝对路径", assert_health)

    def assert_overview_hint() -> str:
        browser, page = ctx.connect(instance)
        try:
            page.set_default_timeout(30_000)
            page.get_by_role("heading", name="会话").wait_for(state="visible", timeout=timeout_ms)
            # D5-AC2 的「概览页那一处」：本地地址必须显示，且与 /health.base_url 一致。
            addresses = element_texts(page, ".local-address__line code")
            _expect(len(addresses) == 1,
                    f"概览页没有唯一的「本地地址」条：{addresses}")
            health_base_url = str(report.facts["A"]["health"].get("base_url") or "")
            _expect(bool(health_base_url),
                    "/health 的 base_url 为空，无法核对概览页地址")
            _expect(
                addresses[0].strip().rstrip("/") == health_base_url.strip().rstrip("/"),
                f"概览页地址 {addresses[0]!r} 与 /health.base_url {health_base_url!r} 不一致")
            address_button = page.get_by_role("button", name="复制", exact=True)
            _expect(address_button.is_visible(), "本地地址没有「复制」按钮")
            address_copied = False
            address_clipboard_ok = False
            try:
                address_button.click()
                page.get_by_text("已复制", exact=True).first.wait_for(
                    state="visible", timeout=15_000)
                address_copied = True
                clipboard = read_clipboard_text() or ""
                address_clipboard_ok = (
                    clipboard.strip().rstrip("/")
                    == addresses[0].strip().rstrip("/"))
            except Exception:  # noqa: BLE001 - 剪贴板权限/焦点不可控
                address_copied = False

            page.get_by_role("heading", name="还没有配置模型密钥").wait_for(
                state="visible", timeout=timeout_ms)
            paths = element_texts(page, ".config-hint__path code")
            _expect(len(paths) >= 2, f"缺密钥提示里的路径条目不足：{paths}")
            env_path = str(report.facts["A"]["health"].get("env_path"))
            _expect(
                any(Path(item) == Path(env_path) for item in paths),
                f"提示文案里没有密钥文件的绝对路径 {env_path}：{paths}",
            )
            env_button = page.locator(".config-hint").get_by_role("button")
            _expect(env_button.is_visible(), "没有「复制路径」按钮")
            # 点击是产品行为本身；剪贴板读不到不算失败（读法另存证据）。
            copied = False
            try:
                env_button.click()
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if "已复制" in (env_button.inner_text() or ""):
                        copied = True
                        break
                    page.wait_for_timeout(200)
            except Exception:  # noqa: BLE001 - 剪贴板权限/焦点不可控
                copied = False
            clipboard_ok = False
            try:
                clipboard = read_clipboard_text()
                clipboard_ok = bool(clipboard and Path(clipboard.strip()) == Path(env_path))
            except Exception:  # noqa: BLE001
                clipboard_ok = False
            shot(page, ctx.out_dir, "A-03-overview-missing-keys", report.facts)
            # 概览页可用：新建会话入口与空列表文案都在。
            _expect(page.get_by_role("button", name="新建会话").is_visible(),
                    "概览页没有「新建会话」入口")
            report.facts["A"]["overview"] = {
                "address": addresses[0],
                "address_matches_health": True,
                "address_copied_state": address_copied,
                "address_clipboard_matches": address_clipboard_ok,
                "hint_paths": paths,
                "copied_state": copied,
                "clipboard_matches_env_path": clipboard_ok,
            }
            return (
                f"本地地址 {addresses[0]}（与 /health 一致；复制="
                f"{'成功' if address_copied else '未显示已复制'}）；"
                f"提示路径 {len(paths)} 条（含密钥文件绝对路径）；"
                f"复制路径={('成功' if copied else '未显示已复制')}；"
                f"剪贴板={'一致' if clipboard_ok else '未取到'}"
            )
        finally:
            browser.close()

    report.run("A3 概览页：本地地址 + 缺密钥提示（绝对路径 + 复制）", assert_overview_hint)

    def assert_close_exit() -> str:
        result = ctx.close_window(instance, None)
        report.facts["A"]["close"] = result
        _expect(result["exit_code"] == 0,
                f"关窗后进程退出码不是 0：{result}")
        return f"关法={result['methods']}；退出码=0"

    report.run("A4 关窗后进程退出（退出码 0）", assert_close_exit)


# ------------------------------------------------------------------ 步骤 B

def step_b(ctx: Context, image: Path, image_last: Path) -> None:
    """真实闭环：真实 LLM、真实窗口、真实图片。"""
    report = ctx.report
    args = ctx.args
    timeout_ms = int(args.timeout * 1000)
    sessions_root = ctx.data_root / "sessions"
    _expect(port_is_free(args.debug_port), f"调试端口 {args.debug_port} 已被占用")

    # 显式 --config 时才要求「起 EXE 前就已经是 dashscope」；不传就用 EXE 生成的模板。
    explicit_config = bool(args.config)
    report.facts["B"] = {}
    if explicit_config:
        config_target = ctx.data_root / "config" / "agent.yaml"
        _expect(config_target.is_file(),
                "起 B 段前没有 data/config/agent.yaml：配置覆盖没有发生")
        config_text = config_target.read_text(encoding="utf-8", errors="replace")
        _expect(EXPECTED_CONFIG_MARK in config_text.lower(),
                f"起 EXE 前 data/config/agent.yaml 不是 {EXPECTED_CONFIG_MARK} 配置："
                f"会打到旧端点（{FORBIDDEN_MODEL_HOST}），不启动。")

    instance = ctx.start("B-real-loop", Path(args.real_appdata))
    report.facts["B"]["instance"] = instance.facts()
    # 启动后核对**实际生效**的配置（bootstrap 不会覆盖已存在的文件）。
    source_label = str(
        (report.facts.get("config_source") or {}).get("source")
        or TEMPLATE_CONFIG_LABEL)
    effective = config_target_facts(ctx.data_root, source_label)
    effective["explicit"] = explicit_config
    report.facts["B"]["config"] = effective
    report.facts["config_source"] = {
        **(report.facts.get("config_source") or {}), **effective}
    if explicit_config:
        _expect(
            effective["target_sha256"]
            == str(report.facts["config_source"].get("source_sha256")),
            "启动后生效的配置与 --config 源文件不一致（被 bootstrap 覆盖了？）")
        _expect(bool(effective.get("contains_dashscope")),
                f"生效配置不是 {EXPECTED_CONFIG_MARK}：会打到旧端点")
    # 启动后再查一次：日志里一旦出现旧端点，立刻停（别等 402 文案再猜）。
    hits = forbidden_host_hits([instance.log_path, instance.stderr_path])
    report.facts["B"]["forbidden_host_hits_before"] = hits
    _expect(not hits,
            f"模型请求打到了 {FORBIDDEN_MODEL_HOST}：用了错误配置（--config 应指向"
            f"dashscope 配置）。命中文件：{hits}")

    with httpx.Client(timeout=httpx.Timeout(60.0)) as client:
        payload = wait_health(client, instance.base_url, 30.0)
        report.facts["B"]["health"] = dict(payload)
        _expect(not payload.get("missing_keys"),
                f"配好密钥后 /health 仍报缺密钥：{payload.get('missing_keys')}")
        token = fetch_token(client, instance.base_url)

        browser, page = ctx.connect(instance)
        try:
            page.set_default_timeout(30_000)
            try:
                page.context.grant_permissions(
                    ["clipboard-read", "clipboard-write"], origin=instance.base_url)
            except Exception:  # noqa: BLE001 - CDP 连接的上下文可能不允许
                pass
            title = window_title_with_port(instance.pid, 10.0)
            _expect(title is not None, "没有找到带端口号的窗口标题")
            report.facts["B"]["window_title"] = title[1]
            _expect(WINDOW_PORT_RE.search(title[1]).group(1) == str(instance.port),
                    f"窗口标题里的端口与启动日志不一致：{title[1]} vs {instance.port}")
            focus_window(title[0])

            def create_and_start() -> str:
                # 窗口本身就打开在 base_url；已在就不重载，避免打断应用自己的首次加载。
                if not page.url.startswith(instance.base_url):
                    navigate(page, instance.base_url, timeout_ms)
                page.get_by_role("button", name="新建会话").click()
                page.locator('textarea[name="plot"]').fill(args.plot)
                page.locator('select[name="variant"]').select_option("FL2VA")
                page.locator('input[name="duration"]').fill(str(args.duration))
                page.get_by_role("button", name="创建并开始").click()
                page.wait_for_url(re.compile(r"/sessions/.+"), timeout=timeout_ms)
                try:
                    wait_review_gate(page, timeout_ms)
                except AcceptanceError:
                    hits = forbidden_host_hits([instance.log_path, instance.stderr_path])
                    if hits:
                        raise AcceptanceError(
                            f"用了错误配置：模型请求打到 {FORBIDDEN_MODEL_HOST}"
                            f"（应为 {EXPECTED_CONFIG_MARK}）。命中文件：{hits}")
                    raise
                hits = forbidden_host_hits([instance.log_path, instance.stderr_path])
                _expect(not hits,
                        f"日志里出现 {FORBIDDEN_MODEL_HOST}：用了错误配置。命中文件：{hits}")
                report.facts["B"]["forbidden_host_hits_after"] = hits
                return page.url

            session_url = report.run("B1 建会话并跑完阶段 1（真实 LLM）", create_and_start)
            session_id = _session_id_from_url(session_url)
            report.facts["B"]["session_id"] = session_id
            report.facts["B"]["session_url"] = session_url
            shot(page, ctx.out_dir, "B-01-awaiting-review", report.facts)

            def review_and_revise() -> str:
                page.get_by_role("tab", name="审查与待办").click()
                page.get_by_role("heading", name="阶段 1 中间产物").wait_for(
                    state="visible", timeout=timeout_ms)
                texts = page.locator(".file-text").all_inner_texts()
                _expect(len(texts) >= 2 and all(text.strip() for text in texts),
                        "阶段 1 中间产物不足两件或正文为空")
                shot(page, ctx.out_dir, "B-02-stage1-review", report.facts)
                page.locator('select[name="revision-layer"]').select_option(
                    label=REVISION_LAYER)
                page.locator('textarea[name="revision-text"]').fill(args.revision)
                page.get_by_role("button", name="提交意见").click()
                history = page.get_by_role("region", name="修订历史")
                history.get_by_text(args.revision).wait_for(
                    state="visible", timeout=timeout_ms)
                page.get_by_role("button", name="通过").wait_for(
                    state="visible", timeout=timeout_ms)
                shot(page, ctx.out_dir, "B-03-revised-draft", report.facts)
                return f"修订层次={REVISION_LAYER}；重跑后新待办已回闸门"

            report.run("B2 审查 + 提交一轮真实修订并等待重跑", review_and_revise)

            def approve_and_wait_frames() -> str:
                page.get_by_role("button", name="通过").click()
                page.get_by_role("heading", name="提交关键帧").wait_for(
                    state="visible", timeout=timeout_ms)
                shot(page, ctx.out_dir, "B-04-frame-submission", report.facts)
                return "approve 后出现 frame_submission 待办"

            report.run("B3 通过阶段 1（帧链进入帧提交待办）", approve_and_wait_frames)

            def upload_frames() -> str:
                page.get_by_role("tab", name="素材").click()
                upload = page.locator("#asset-upload")
                upload.set_input_files(str(image))
                page.locator(".asset__meta strong").filter(
                    has_text=image.name).first.wait_for(
                        state="visible", timeout=timeout_ms)
                if image_last != image:
                    upload.set_input_files(str(image_last))
                    page.locator(".asset__meta strong").filter(
                        has_text=image_last.name).first.wait_for(
                            state="visible", timeout=timeout_ms)
                shot(page, ctx.out_dir, "B-05-assets", report.facts)

                page.get_by_role("tab", name="审查与待办").click()
                page.get_by_role("heading", name="提交关键帧").wait_for(
                    state="visible", timeout=timeout_ms)
                page.locator('select[name="first-frame"]').select_option(label=image.name)
                page.locator('select[name="last-frame"]').select_option(label=image_last.name)
                page.get_by_role("button", name="提交帧并继续").click()
                wait_text(page, "已完成", timeout_ms)
                shot(page, ctx.out_dir, "B-06-completed", report.facts)
                return f"首帧={image.name}；尾帧={image_last.name}；状态=completed"

            report.run("B4 上传真实图片作首/尾帧并跑完真实阶段 2", upload_frames)

            # 完成态证据立刻采（交付步失败时报告仍要能说清阶段 2 与读图发生过）。
            detail = session_detail(client, instance.base_url, token, session_id)
            _expect(detail.get("status") == "completed",
                    f"会话终态不是 completed：{detail.get('status')}")
            artifacts = detail.get("artifacts") or []
            artifact = next(
                (item for item in artifacts if item.get("name") == "video-prompt.md"), None)
            _expect(artifact is not None, "会话快照里没有 video-prompt.md 产物")
            artifact_id = str(artifact.get("artifact_id") or "")
            artifact_path = sessions_root / artifact_id
            _expect(artifact_path.is_file(), f"产物登记路径不在盘上：{artifact_path}")
            report.facts["B"]["artifact"] = {
                "artifact_id": artifact_id,
                "path": str(artifact_path),
                "bytes": artifact_path.stat().st_size,
                "sha256": _sha256(artifact_path),
            }
            d4 = load_accept_d4()
            report.facts["B"]["frame_read_evidence"] = d4._frame_read_evidence(
                sessions_root, session_id)

            def deliver() -> str:
                page.get_by_role("tab", name="产物").click()
                deliverable = page.locator('[data-group="deliverable"]')
                deliverable.get_by_role("heading", name="交付文本").wait_for(
                    state="visible", timeout=timeout_ms)
                copy_button = deliverable.locator('[data-copy="video-prompt.md"]')
                copy_button.wait_for(state="visible", timeout=timeout_ms)
                shot(page, ctx.out_dir, "B-07-delivery", report.facts)
                copy_button.click()
                print("[B5] 复制按钮已点击，等「已复制」", flush=True)
                page.get_by_text("已复制", exact=True).first.wait_for(
                    state="visible", timeout=30_000)
                print("[B5] 界面已显示「已复制」", flush=True)
                # 先读系统剪贴板（Win32）：WebView2 里 ``navigator.clipboard.readText()``
                # 可能一直挂着等权限（2026-11-12 演练实测 2.4 分钟才返回），不能当主路径。
                clipboard = read_clipboard_text() or ""
                read_method = "win32-clipboard" if clipboard.strip() else ""
                if len(clipboard.strip()) <= 100:
                    try:
                        # 页面内做 5 秒竞速：WebView2 的剪贴板读可能一直等权限。
                        clipboard = str(page.evaluate(
                            """async () => {
                                const timeout = new Promise((resolve) =>
                                    setTimeout(() => resolve(''), 5000));
                                const read = navigator.clipboard.readText()
                                    .catch(() => '');
                                return await Promise.race([read, timeout]);
                            }""") or "")
                        read_method = "page-evaluate"
                    except Exception:  # noqa: BLE001 - 权限/焦点不可控
                        pass
                if clipboard.strip():
                    (ctx.out_dir / "final-prompt-clipboard.md").write_text(
                        _normalize_newlines(clipboard) + "\n", encoding="utf-8")
                print(f"[B5] 剪贴板读到 {len(clipboard)} 字符（{read_method}）", flush=True)

                content_url = (
                    f"{instance.base_url}api/v1/artifacts/"
                    f"{quote(artifact_id, safe='/')}/content")
                # 下载链接的 DOM 形态先钉住：href 指向 content 端点、带 download 属性。
                anchor = page.locator('[data-download="video-prompt.md"]')
                href = str(anchor.get_attribute("href") or "")
                download_attr = anchor.get_attribute("download")
                _expect(content_url in href or href.endswith("/content"),
                        f"下载链接的 href 不是 content 端点：{href}")
                _expect(download_attr == "video-prompt.md",
                        f"下载链接的 download 属性不对：{download_attr!r}")
                body, capture = capture_browser_bytes(
                    page, content_url, lambda: anchor.click(), timeout_ms,
                    save_path=ctx.out_dir / "video-prompt.md")
                try:
                    page.get_by_text("已开始下载").first.wait_for(
                        state="visible", timeout=5_000)
                except Exception:  # noqa: BLE001 - 提示条不是判据，正文才是
                    pass
                print(f"[B5] 下载正文取到 {len(body)} 字节（{capture}）", flush=True)
                saved = ctx.out_dir / "video-prompt.md"
                saved.write_bytes(body)
                downloaded = saved.read_bytes().decode("utf-8", errors="replace")
                _expect(len(downloaded.strip()) > 100,
                        f"下载的产物正文为空或过短（{len(downloaded)} 字符）")
                if len(clipboard.strip()) > 100:
                    _expect(
                        _normalize_newlines(downloaded).strip()
                        == _normalize_newlines(clipboard).strip(),
                        "剪贴板正文与下载落盘正文不一致",
                    )
                report.facts["B"]["delivery"] = {
                    "clipboard_chars": len(clipboard),
                    "clipboard_read_method": read_method or "未读到（以界面「已复制」为证据）",
                    "clipboard_matches_download": bool(
                        len(clipboard.strip()) > 100
                        and _normalize_newlines(downloaded).strip()
                        == _normalize_newlines(clipboard).strip()),
                    "download_bytes": saved.stat().st_size,
                    "download_capture": capture,
                }
                return (
                    f"复制 {len(clipboard)} 字符（{read_method or '未读到'}）；"
                    f"下载 {saved.stat().st_size} 字节；捕获方式={capture}")

            report.run("B5 产物分区：复制并下载最终提示词", deliver)
        finally:
            # 端口与窗口留给步骤 C/D 用：这里只断开 CDP 连接。
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass


def capture_browser_bytes(
    page: Any, url: str, trigger: Callable[[], None], timeout_ms: int,
    *, save_path: Path | None = None,
) -> tuple[bytes, str]:
    """点下载/导出后，从浏览器拿响应体。

    三种取法按可靠性排序：CDP 下载事件（``save_as``）→ CDP 响应体 →
    页面内 ``fetch``（带 Cookie，与下载同一份凭据）。CDP 连接的上下文里
    ``<a download>`` 不一定被 Playwright 接住，所以不能只赌一种。
    """
    captured: dict[str, Any] = {}

    def on_response(response):
        if url.rstrip("/") == response.url.rstrip("/"):
            captured["response"] = response

    def on_download(download):
        if url.rstrip("/") == download.url.rstrip("/"):
            captured.setdefault("download", download)

    page.on("response", on_response)
    page.on("download", on_download)
    try:
        # 点一个「会触发下载」的链接时，Playwright 的 click 可能一直等到默认超时；
        # 这里压到 10s：阻塞与否都不能把整步拖成十几分钟。
        page.set_default_timeout(10_000)
        try:
            trigger()
        finally:
            page.set_default_timeout(30_000)
    except Exception as exc:  # noqa: BLE001 - 下载被拦也要给出原因
        captured["trigger_error"] = f"{type(exc).__name__}: {exc}"
    # 下载/响应都该在几秒内出现；等满整步超时只会把失败拖成「卡住」。
    deadline = time.monotonic() + min(timeout_ms / 1000, 20.0)
    while ("response" not in captured and "download" not in captured
           and time.monotonic() < deadline):
        page.wait_for_timeout(100)
    download = captured.get("download")
    if download is not None and save_path is not None:
        try:
            download.save_as(str(save_path))
            return Path(save_path).read_bytes(), "cdp-download"
        except Exception:  # noqa: BLE001 - 保存失败就退回响应体/页面 fetch
            pass
    response = captured.get("response")
    if response is not None:
        _expect(response.status == 200, f"下载端点返回 {response.status}")
        try:
            return response.body(), "cdp-response-body"
        except Exception:  # noqa: BLE001 - 下载型响应没有可读 body
            pass
    if response is None and download is None:
        reason = captured.get("trigger_error")
        if reason:
            print(f"[capture] 下载事件没进 CDP（{reason}），改用页面 fetch", flush=True)
    else:
        print("[capture] CDP 拿不到响应体，改用页面 fetch", flush=True)
    values = page.evaluate(
        """async (target) => {
            const response = await fetch(target, { credentials: 'include' });
            const buffer = await response.arrayBuffer();
            return Array.from(new Uint8Array(buffer));
        }""",
        url,
    )
    return bytes(int(value) for value in values), "page-fetch"


def _session_id_from_url(url: str) -> str:
    """浏览器地址里是逐段百分号编码的深链，解回会话标识（含斜杠）。"""
    path = httpx.URL(url).path
    raw = path[len("/sessions/"):] if path.startswith("/sessions/") else path
    return "/".join(unquote(part) for part in raw.split("/"))


# ------------------------------------------------------------------ 步骤 C

def step_c(ctx: Context) -> None:
    """浏览器并存、第二实例、显式端口占用、关窗退出与重启持久化。"""
    report = ctx.report
    args = ctx.args
    timeout_ms = int(args.timeout * 1000)
    instance = next((item for item in ctx.instances if item.name == "B-real-loop"), None)
    _expect(instance is not None, "步骤 C 需要 B 的实例（窗口）还活着")
    session_id = str((report.facts.get("B") or {}).get("session_id") or "")
    _expect(session_id, "步骤 C 需要 B 留下的会话标识")

    def external_browser() -> str:
        # 复用主循环里的 Playwright：CDP 连接与外部浏览器实例共用一个驱动，
        # 避免同线程里再起第二个 sync 实例。
        channel = os.environ.get("PW_CHANNEL") or None
        browser = ctx.playwright.chromium.launch(headless=True, channel=channel)
        try:
            context = browser.new_context()
            page = context.new_page()
            page.set_default_timeout(30_000)
            navigate(page, instance.base_url, timeout_ms)
            page.get_by_role("heading", name="会话").wait_for(
                state="visible", timeout=timeout_ms)
            plot = str((report.facts.get("B") or {}).get("plot") or args.plot)
            page.get_by_text(plot, exact=False).first.wait_for(
                state="visible", timeout=timeout_ms)
            page.get_by_text(plot, exact=False).first.click()
            page.wait_for_url(re.compile(r"/sessions/.+"), timeout=timeout_ms)
            page.get_by_role("tab", name="审查与待办").wait_for(
                state="visible", timeout=timeout_ms)
            shot_path = ctx.out_dir / "screenshots" / "C-01-external-browser.png"
            shot_path.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(shot_path), full_page=True)
            report.facts.setdefault("screenshots", []).append(str(shot_path))
            cookies = context.cookies(instance.base_url)
            names = sorted(cookie["name"] for cookie in cookies)
            report.facts["C"] = {
                "external_browser_url": page.url,
                "cookies": names,
                "screenshot": str(shot_path),
            }
            context.close()
            return f"外部 Chromium 列出会话并打开详情；Cookie={names}"
        finally:
            browser.close()

    report.run("C1 窗口开着时外部浏览器列出会话并打开详情", external_browser)

    def auth_not_relaxed() -> str:
        with httpx.Client(timeout=httpx.Timeout(15.0)) as client:
            response = client.get(f"{instance.base_url}api/v1/sessions")
            _expect(response.status_code == 401,
                    f"未带凭据访问 /api/v1/sessions 返回 {response.status_code}（应为 401）")
            body = response.text
            _expect("auth_required" in body, f"401 响应不是 auth_required：{body[:120]}")
        report.facts["C"]["unauthenticated_status"] = 401
        return "未带令牌/Cookie 的 GET /api/v1/sessions = 401 auth_required"

    report.run("C2 鉴权未放宽（未带凭据仍 401）", auth_not_relaxed)

    def external_download_bundle() -> str:
        """外部 Chromium 里走一次**真实下载**：诊断包一键导出。

        窗口里的 `<a download>` 在 CDP 下看不到响应体（WebView2 原生下载），
        所以「下载成功」这条用 Playwright 自控的 Chromium 补一份真凭实据；
        窗口侧仍会点同一个按钮并由页面 fetch 取到同一份 zip（步骤 D）。
        """
        channel = os.environ.get("PW_CHANNEL") or None
        browser = ctx.playwright.chromium.launch(headless=True, channel=channel)
        try:
            context = browser.new_context(accept_downloads=True)
            page = context.new_page()
            page.set_default_timeout(30_000)
            navigate(page, instance.base_url + "diagnostics", timeout_ms)
            page.get_by_role("heading", name="诊断", exact=True).wait_for(
                state="visible", timeout=timeout_ms)
            with page.expect_download(timeout=30_000) as info:
                page.get_by_role("button", name="导出诊断包").click()
            download = info.value
            saved = ctx.out_dir / "diagnostics-bundle-external-chromium.zip"
            download.save_as(str(saved))
            _expect(saved.stat().st_size > 1024, "外部 Chromium 下载的诊断包过小")
            report.facts.setdefault("C", {})["external_bundle_download"] = {
                "path": str(saved),
                "bytes": saved.stat().st_size,
                "sha256": _sha256(saved),
                "suggested_filename": download.suggested_filename,
            }
            context.close()
            return (
                f"外部 Chromium 一键导出并落盘 {saved.stat().st_size} 字节；"
                f"建议文件名 `{download.suggested_filename}`")
        finally:
            browser.close()

    report.run("C3 外部 Chromium 一键导出诊断包（真实下载）", external_download_bundle)

    def second_instance_rejected() -> str:
        result = ctx.launch_expect_failure(
            "C-second-instance", Path(args.real_appdata), ("--port", "0"))
        report.facts["C"]["second_instance"] = result
        dialog = result.get("dialog") or {}
        body = str(dialog.get("body") or "")
        _expect(bool(dialog), "第二个实例没有给出任何提示窗口（消息框）")
        _expect(
            ("已经有一个服务在运行" in body) or ("同一个数据目录" in body),
            f"第二实例的提示文案不含预期内容：{body!r}",
        )
        with httpx.Client(timeout=httpx.Timeout(15.0)) as client:
            payload = health(client, instance.base_url)
        _expect(payload.get("status") == "ok", "第二实例启动后第一个实例的健康检查失败")
        return "第二实例被拒并弹出提示；第一个实例仍健康"

    report.run("C4 同一 data/ 的第二个实例被拒并提示", second_instance_rejected)

    def occupied_port_rejected() -> str:
        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        port = holder.getsockname()[1]
        try:
            result = ctx.launch_expect_failure(
                "C-occupied-port", Path(args.real_appdata),
                ("--port", str(port), "--data-root", str(ctx.work_dir / "occupied-data")))
        finally:
            holder.close()
        report.facts["C"]["occupied_port"] = result
        dialog = result.get("dialog") or {}
        body = str(dialog.get("body") or "")
        _expect(bool(dialog), "显式端口被占用时没有给出提示窗口")
        _expect(f"端口 {port}" in body and "已被占用" in body,
                f"占用端口的提示没有点名端口：{body!r}")
        return f"--port {port}（被占用）→ 提示「端口 {port} 已被占用」"

    report.run("C5 显式端口被占用：报错退出并说明", occupied_port_rejected)

    def close_window() -> str:
        result = ctx.close_window(instance, None)
        report.facts["C"]["close"] = result
        _expect(result["exit_code"] == 0, f"关窗后进程退出码不是 0：{result}")
        return f"关法={result['methods']}；退出码=0"

    report.run("C6 关窗后进程退出（退出码 0）", close_window)

    def restart_and_verify() -> str:
        restarted = ctx.start("C-restart", Path(args.real_appdata))
        report.facts["C"]["restart"] = restarted.facts()
        with httpx.Client(timeout=httpx.Timeout(30.0)) as client:
            wait_health(client, restarted.base_url, 30.0)
            token = fetch_token(client, restarted.base_url)
            detail = session_detail(client, restarted.base_url, token, session_id)
        _expect(detail.get("status") == "completed",
                f"重启后会话终态不是 completed：{detail.get('status')}")
        names = {item.get("name") for item in detail.get("artifacts") or []}
        _expect("video-prompt.md" in names,
                f"重启后产物清单里没有 video-prompt.md：{sorted(names)}")
        artifact = next(
            (item for item in detail.get("artifacts") or []
             if item.get("name") == "video-prompt.md"), None)
        artifact_path = ctx.data_root / "sessions" / str(artifact.get("artifact_id"))
        _expect(artifact_path.is_file(), f"重启后产物文件不在盘上：{artifact_path}")
        browser, page = ctx.connect(restarted)
        try:
            page.set_default_timeout(30_000)
            navigate(page, restarted.base_url, timeout_ms)
            page.get_by_text(args.plot, exact=False).first.wait_for(
                state="visible", timeout=timeout_ms)
            shot(page, ctx.out_dir, "C-05-restarted-session-list", report.facts)
        finally:
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass
        report.facts["C"]["restart"]["session_status"] = detail.get("status")
        report.facts["C"]["restart"]["artifact_path"] = str(artifact_path)
        report.facts["C"]["restart"]["artifact_bytes"] = artifact_path.stat().st_size
        return (
            f"重启后同一会话仍在（completed），产物 {artifact_path.stat().st_size} 字节；"
            "会话列表可见")

    report.run("C7 重启（同一 data/）后会话与产物仍在", restart_and_verify)


# ------------------------------------------------------------------ 步骤 D

def step_d(ctx: Context) -> None:
    """诊断包：导出前清单、一键导出、包内容自检（密钥值命中必须为 0）。"""
    report = ctx.report
    args = ctx.args
    timeout_ms = int(args.timeout * 1000)
    instance = next(
        (item for item in reversed(ctx.instances) if item.name == "C-restart"), None)
    _expect(instance is not None, "步骤 D 需要 C 重启后的实例（窗口）")
    session_id = str((report.facts.get("B") or {}).get("session_id") or "")

    browser, page = ctx.connect(instance)
    try:
        page.set_default_timeout(30_000)

        def before_export() -> str:
            navigate(page, instance.base_url + "diagnostics", timeout_ms)
            page.get_by_role("heading", name="诊断", exact=True).wait_for(
                state="visible", timeout=timeout_ms)
            items = element_texts(page, ".bundle-items li")
            _expect(len(items) >= 6, f"导出前清单条目过少：{items}")
            note = page.locator(".bundle-note").inner_text()
            report.facts["D"] = {"before_export_items": items, "note": note}
            shot(page, ctx.out_dir, "D-01-diagnostics", report.facts)
            return f"导出前清单 {len(items)} 条；说明：{note}"

        report.run("D1 /diagnostics 读出导出前条目清单", before_export)

        def export_bundle() -> str:
            bundle = ctx.out_dir / "diagnostics-bundle.zip"
            body, capture = capture_browser_bytes(
                page, f"{instance.base_url}{BUNDLE_URL_PART.lstrip('/')}",
                lambda: page.get_by_role("button", name="导出诊断包").click(),
                timeout_ms, save_path=bundle)
            bundle.write_bytes(body)
            _expect(bundle.stat().st_size > 1024, "诊断包小于 1 KiB，像是错误页")
            report.facts["D"]["bundle"] = {
                "path": str(bundle),
                "bytes": bundle.stat().st_size,
                "sha256": _sha256(bundle),
                "capture": capture,
            }
            return f"诊断包 {bundle.stat().st_size} 字节（捕获方式={capture}）"

        report.run("D2 一键导出诊断包并落盘", export_bundle)

        def verify_bundle() -> str:
            bundle = ctx.out_dir / "diagnostics-bundle.zip"
            env_map = parse_env_file(Path(args.env_file))
            secrets = secret_values(env_map)
            _expect(bool(secrets), f"{args.env_file} 里没有可扫描的密钥值（*_API_KEY 等）")
            entries = read_zip_entries(bundle)
            problems = bundle_problems(entries, session_id=session_id or None)
            _expect(not problems, "诊断包内容不符合预期：" + "；".join(problems))
            hits = secret_hits(entries, secrets)
            _expect(not hits,
                    "诊断包里出现了密钥值（条目：" + "、".join(hits) + "）")
            report.facts["D"]["entries"] = sorted(entries)
            report.facts["D"]["checks"] = {
                "entry_count": len(entries),
                "secret_values_scanned": len(secrets),
                "secret_hits": len(hits),
                "media_entries": [
                    name for name in entries if Path(name).suffix.lower() in MEDIA_SUFFIXES
                ],
                "env_entries": [name for name in entries if Path(name).name == ".env"],
            }
            # manifest 的形态与端口：冻结 + 实际端口（AC2 的「诊断包」侧的补充证据）。
            manifest = json.loads(entries["manifest.json"].decode("utf-8"))
            report.facts["D"]["manifest"] = manifest
            _expect(manifest.get("form") == "冻结",
                    f"诊断包 manifest 的形态不是「冻结」：{manifest.get('form')}")
            _expect(manifest.get("port") == instance.port,
                    f"诊断包 manifest 的端口与实例不一致：{manifest.get('port')}")
            # 与外部 Chromium 真下载的那一份对账：同一时刻的两个导出，条目集合应一致
            # （日志窗口按天滚动，同一进程同一份 logs 目录，不该有差异）。
            external = ctx.out_dir / "diagnostics-bundle-external-chromium.zip"
            if external.is_file():
                external_entries = read_zip_entries(external)
                _expect(
                    sorted(external_entries) == sorted(entries),
                    "窗口导出与外部浏览器下载的诊断包条目不一致",
                )
                _expect(not secret_hits(external_entries, secrets),
                        "外部浏览器下载的诊断包里出现了密钥值")
                report.facts["D"]["external_bundle_matches"] = True
            return (
                f"条目 {len(entries)} 个齐全；扫描 {len(secrets)} 个密钥值，命中 0；"
                "无媒体、无 .env")


        report.run("D3 诊断包自检：条目齐全、无密钥值、无媒体、无 .env", verify_bundle)
    finally:
        # 窗口在 D 之后关掉；即使 D 中途失败也走同一条收尾（cleanup 兜底）。
        pass
    result = ctx.close_window(instance, page)
    ctx.report.facts["D"]["close"] = result
    try:
        browser.close()
    except Exception:  # noqa: BLE001
        pass
    _expect(result["exit_code"] == 0, f"D 步骤关窗后退出码不是 0：{result}")
    ctx.report.step("D4 关窗后进程退出（退出码 0）", "ok", 0,
                    f"关法={result['methods']}；退出码=0")


def read_zip_entries(bundle: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(bundle) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def bundle_problems(
    entries: Mapping[str, bytes], *, session_id: str | None = None,
) -> list[str]:
    """§8.2 清单与排除面的机械判据（纯函数，测试直接用假条目咬）。

    - 必备：manifest/environment/config + 至少一份日志 + 会话状态与事件流 + 文本产物；
    - 不得有媒体图片/视频、不得有 ``.env``。
    """
    names = list(entries)
    problems: list[str] = []
    for required in ("manifest.json", "environment.txt", "config.json"):
        if required not in names:
            problems.append(f"缺少 {required}")
    if not any(name.startswith("logs/") and name.endswith(".log") for name in names):
        problems.append("缺少 logs/*.log")
    if session_id is not None:
        prefix = f"sessions/{session_id}/"
        if f"{prefix}session-state.json" not in names:
            problems.append(f"缺少 {prefix}session-state.json")
        if f"{prefix}events.jsonl" not in names:
            problems.append(f"缺少 {prefix}events.jsonl")
        if not any(
            name.startswith(f"artifacts/{session_id}/") and name.endswith(".md")
            for name in names
        ):
            problems.append(f"缺少 artifacts/{session_id}/*.md")
    media = [name for name in names if Path(name).suffix.lower() in MEDIA_SUFFIXES]
    if media:
        problems.append("含媒体文件：" + "、".join(sorted(media)[:5]))
    env_files = [name for name in names if Path(name).name == ".env"]
    if env_files:
        problems.append("含 .env：" + "、".join(sorted(env_files)[:5]))
    return problems


def secret_hits(
    entries: Mapping[str, bytes], secrets: Iterable[str],
) -> list[str]:
    """包内任何条目正文含密钥值即命中；只返回条目名，绝不返回值本身。"""
    values = [value.encode("utf-8") for value in secrets if value]
    hits: list[str] = []
    for name, payload in entries.items():
        if any(value in payload for value in values):
            hits.append(name)
    return hits


def zip_media_or_env(entries: Iterable[str]) -> list[str]:
    """交付 zip 里不该有的媒体/密钥文件（AC15 的结构判据）。"""
    found: list[str] = []
    for name in entries:
        if Path(name).suffix.lower() in MEDIA_SUFFIXES or Path(name).name == ".env":
            found.append(name)
    return found


# ------------------------------------------------------------------ 报告渲染
def report_lines(report: Report) -> list[str]:
    facts = report.facts
    step_rows = [
        f"| {step['name']} | {step['status']} | {step['ms']} | {step['note']} |"
        for step in report.steps
    ]
    screenshots = [f"- `{path}`" for path in facts.get("screenshots", [])]
    bundle = facts.get("B") or {}
    delivery = bundle.get("delivery") or {}
    artifact = bundle.get("artifact") or {}
    frames = (bundle.get("frame_read_evidence") or {})
    descriptions = frames.get("descriptions") or {}
    frame_images = frames.get("frame_images") or []
    env_info = facts.get("environment") or {}
    build = facts.get("build") or {}
    bundle_facts = (facts.get("D") or {}).get("bundle") or {}
    checks = (facts.get("D") or {}).get("checks") or {}
    config_source = facts.get("config_source") or {}
    before_hits = len(bundle.get("forbidden_host_hits_before") or [])
    after_hits = len(bundle.get("forbidden_host_hits_after") or [])
    lines = [
        "# D5 干净环境验收证据（#242 / 父 spec #235）",
        "",
        "## 干净环境声明",
        "",
        f"- **非全新系统**：本机 Windows 11（`{env_info.get('os', '')}`），未使用回滚快照/虚拟机；"
        "按 spec §11 的 **R 降级**形态验收（`env -i` 清空环境变量与 PATH，不改名 `.venv`）。",
        f"- 子进程环境变量（仅这些）：`{', '.join(env_info.get('variables') or [])}`",
        f"- `APPDATA`（A 段）= `{env_info.get('apdata_empty', '')}`（空目录）",
        f"- `APPDATA`（B/C/D 段）= `{env_info.get('apdata_real', '')}`",
        f"- `PATH` = `{env_info.get('path', '')}`（没有 Python/Node/项目依赖）",
        "",
        "## 配置来源核对",
        "",
        f"- 源配置：`{config_source.get('source', '')}`"
        + (f"（{config_source.get('source_bytes', '')} 字节，"
           f"sha256 `{config_source.get('source_sha256', '')}`）"
           if config_source.get("source_bytes") else ""),
        f"- 覆盖方式：{'`--config` 显式传入并在起 EXE 前覆盖' if config_source.get('explicit') else '未覆盖（用 EXE 首次启动生成的内置模板）'}",
        f"- 生效配置：`{config_source.get('target', '')}`（{config_source.get('target_bytes', '')} 字节，"
        f"sha256 `{config_source.get('target_sha256', '')}`）",
        f"- provider / model / base_url：`{config_source.get('providers', [])}` / "
        f"`{config_source.get('models', [])}` / `{config_source.get('base_urls', [])}`",
        f"- 旧端点 `{FORBIDDEN_MODEL_HOST}` 命中（日志/stderr）：前置 {before_hits} 处、"
        f"阶段 1 后 {after_hits} 处（必须为 0）",
        "",
        "## 命令与计时",
        "",
        f"- 命令：`{facts.get('command', '')}`",
        f"- 起止（UTC）：{facts.get('started_at', '')} → {facts.get('finished_at', '')}",
        f"- 报告目录：`{facts.get('out_dir', '')}`",
        f"- 管线：{facts.get('pipeline', '')}",
        "",
        "## 构建与解压",
        "",
        f"- 构建 zip：`{build.get('zip', '')}`（{build.get('zip_bytes_human', '')}，"
        f"sha256 `{build.get('zip_sha256', '')}`）",
        f"- zip 顶层：`{', '.join(build.get('top_level') or [])}`；条目 {build.get('entries', '')}",
        f"- 安装器条目：{build.get('installer_entries') or '无'}；媒体条目："
        f"{build.get('media_entries') or '无'}；`.env` 条目：{build.get('env_entries') or '无'}",
        f"- 解压目录：`{facts.get('portable', '')}`",
        f"- EXE：{build.get('exe_bytes', '')}，签名状态 `{build.get('signature', '')}`",
        f"- 解压耗时：{build.get('extract_ms', '')} ms",
        "",
        "## 运行实例",
        "",
        "| 实例 | PID | 端口 | 数据根 | 启动日志 |",
        "|---|---|---|---|---|",
        *[
            f"| {item.get('name')} | {item.get('pid')} | {item.get('port')} | "
            f"`{item.get('data_root')}` | `{item.get('log_path')}` |"
            for item in facts.get("instances", [])
        ],
        "",
        "## 真实闭环（B）",
        "",
        f"- 会话标识：`{bundle.get('session_id', '')}`",
        f"- 窗口标题：`{bundle.get('window_title', '')}`",
        f"- 产物 `video-prompt.md`：artifact_id `{artifact.get('artifact_id', '')}`，"
        f"`{artifact.get('path', '')}`（{artifact.get('bytes', '')} 字节）",
        f"- 复制 {delivery.get('clipboard_chars', '')} 字符"
        f"（{delivery.get('clipboard_read_method', '')}）；"
        f"下载 {delivery.get('download_bytes', '')} 字节；捕获方式 `{delivery.get('download_capture', '')}`",
        f"- 剪贴板与下载正文一致：{delivery.get('clipboard_matches_download', '')}",
        f"- 首帧读图描述：{descriptions.get('first', '（缺）')}",
        f"- 尾帧读图描述：{descriptions.get('last', '（缺）')}",
        "- 帧图：",
        *([f"  - `{path}`" for path in frame_images] or ["  （无）"]),
        "",
        "## 诊断包（D）",
        "",
        f"- 文件：`{bundle_facts.get('path', '')}`（{bundle_facts.get('bytes', '')} 字节，"
        f"sha256 `{bundle_facts.get('sha256', '')}`）",
        f"- 条目 {checks.get('entry_count', '')} 个；扫描密钥值 {checks.get('secret_values_scanned', '')} 个，"
        f"**命中 {checks.get('secret_hits', '')} 次**",
        f"- 媒体条目：{checks.get('media_entries') or '无'}；`.env` 条目：{checks.get('env_entries') or '无'}",
        "",
        "## 截图",
        "",
        *(screenshots or ["（无）"]),
        "",
        "## 步骤与耗时",
        "",
        "| 步骤 | 结果 | 毫秒 | 备注 |",
        "|---|---|---|---|",
        *step_rows,
        "",
        "## AC 逐条",
        "",
        *render_ac_rows(report.ac),
        "",
    ]
    if report.failures:
        lines += ["## 未达标项", ""] + [f"- {failure}" for failure in report.failures] + [""]
    return lines


def write_report(report: Report, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.md").write_text(
        "\n".join(report_lines(report)), encoding="utf-8")
    (out_dir / "report.json").write_text(
        json.dumps(
            {"facts": report.facts, "steps": report.steps,
             "failures": report.failures, "ac": report.ac},
            ensure_ascii=False, indent=2, default=str,
        ) + "\n",
        encoding="utf-8",
    )


# ------------------------------------------------------------------ 证据拷贝

def copy_session_evidence(ctx: Context) -> list[str]:
    """把会话与产物抄进证据目录（便携目录随后会被清理）。"""
    session_id = str(((ctx.report.facts.get("B") or {}).get("session_id")) or "")
    copied: list[str] = []
    if not session_id:
        return copied
    source = ctx.data_root / "sessions" / session_id
    target = ctx.out_dir / "session"
    target.mkdir(parents=True, exist_ok=True)
    for name in ("session-state.json", "events.jsonl"):
        path = source / name
        if path.is_file():
            shutil.copyfile(path, target / name)
            copied.append(str(target / name))
    artifact = (ctx.report.facts.get("B") or {}).get("artifact") or {}
    if artifact.get("path") and Path(artifact["path"]).is_file():
        shutil.copyfile(artifact["path"], target / "video-prompt.md")
        copied.append(str(target / "video-prompt.md"))
    for log in sorted((ctx.data_root / "logs").glob("app-*.log")):
        destination = ctx.out_dir / "logs" / log.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(log, destination)
        copied.append(str(destination))
    config = ctx.data_root / "config" / "agent.yaml"
    if config.is_file():
        shutil.copyfile(config, ctx.out_dir / "agent.yaml")
        copied.append(str(ctx.out_dir / "agent.yaml"))
    readme = ctx.portable / "README.txt"
    if readme.is_file():
        shutil.copyfile(readme, ctx.out_dir / "README.txt")
        copied.append(str(ctx.out_dir / "README.txt"))
    ctx.report.facts["copied_evidence"] = copied
    return copied


def cleanup(ctx: Context) -> dict[str, Any]:
    """收掉所有进程与临时目录；只留证据目录。"""
    summary: dict[str, Any] = {"processes": [], "removed": [], "env_file": ""}
    for instance in ctx.instances:
        if instance.proc.poll() is None:
            summary["processes"].append(
                {"name": instance.name, "pid": instance.pid,
                 "exit_code": kill_tree(instance.proc)})
        else:
            summary["processes"].append(
                {"name": instance.name, "pid": instance.pid,
                 "exit_code": instance.proc.returncode})
    # 兜底：只**点名**同名残留进程，不主动杀——用户可能自己开着这个程序。
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {EXE_NAME}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=30)
        leftovers = [
            line.strip() for line in (result.stdout or "").splitlines()
            if line.strip() and EXE_NAME.lower() in line.lower()
        ]
        summary["leftover_processes"] = leftovers
    except (OSError, subprocess.SubprocessError):
        summary["leftover_processes"] = ["未能枚举"]
    for path in (ctx.work_dir, ctx.apdata_empty):
        try:
            if Path(path).exists():
                shutil.rmtree(path, ignore_errors=True)
                summary["removed"].append(str(path))
        except OSError as exc:  # pragma: no cover - 文件占用
            summary["removed"].append(f"{path}（失败：{exc}）")
    env_target = Path(ctx.args.real_appdata) / "MiniMax-H3-prompt" / ".env"
    backup = env_target.with_name(".env.accept-d5-backup")
    if backup.is_file():
        shutil.move(str(backup), str(env_target))
        summary["env_file"] = f"恢复备份 {env_target}"
    elif env_target.is_file() and ctx.args.remove_env_file:
        env_target.unlink()
        summary["env_file"] = f"已删除 {env_target}"
        parent = env_target.parent
        if parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            summary["env_file"] += "（空目录一并删除）"
    else:
        summary["env_file"] = "保留（--keep-env-file 或未创建）"
    return summary


# ------------------------------------------------------------------ 自检

def self_check(args: argparse.Namespace) -> int:
    """不起进程、不调模型：zip/图片/密钥/Playwright/端口/输出目录。"""
    checks: list[tuple[str, bool, str]] = []
    zip_path = Path(args.zip).resolve()
    checks.append(("交付 zip", zip_path.is_file(), str(zip_path)))
    image = Path(args.image).resolve() if args.image else None
    image_ok = image is not None and image.is_file()
    if image_ok and image.stat().st_size < 8192:
        image_ok = False
    checks.append(("真实图片", image_ok,
                   str(image) if image is not None else "用 --image 指定一张真实照片"))
    env_path = Path(args.env_file).resolve()
    env_map = parse_env_file(env_path)
    secrets = secret_values(env_map)
    checks.append(("密钥文件", bool(secrets),
                   f"{env_path}（{len(secrets)} 个 *_API_KEY 等值）"))
    if args.config:
        config_path = Path(args.config).resolve()
        config_ok = config_path.is_file()
        config_note = str(config_path)
        if config_ok:
            raw = config_path.read_bytes()
            facts = config_facts(raw.decode("utf-8", errors="replace"))
            config_ok = bool(facts["contains_dashscope"])
            config_note += (
                f"（{len(raw)} 字节，sha256 {hashlib.sha256(raw).hexdigest()[:16]}…，"
                f"model={facts['models']}，base_url={facts['base_urls']}）"
                if config_ok else "：不含 dashscope（会打到旧端点）")
        checks.append(("agent.yaml（--config）", config_ok, config_note))
    else:
        checks.append((
            "agent.yaml（未指定 --config）", True,
            "将使用 EXE 首次启动生成的内置模板；要指定真实配置请显式传 "
            "--config <path>（真实模型闭环必须传）"))
    out_dir = Path(args.out_dir) if args.out_dir else (
        ROOT / "output" / "accept-d5" / _stamp())
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        probe = out_dir / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        writable, note = True, str(out_dir)
    except OSError as exc:  # pragma: no cover
        writable, note = False, f"{out_dir}：{exc}"
    checks.append(("输出目录可写", writable, note))
    checks.append(("Playwright for Python",
                   importlib.util.find_spec("playwright") is not None,
                   PLAYWRIGHT_INSTALL_HINT))
    free = port_is_free(args.debug_port)
    checks.append(("调试端口空闲", free, f"127.0.0.1:{args.debug_port}"))
    apdata = Path(args.real_appdata)
    checks.append(("真实 APPDATA 可写", apdata.is_dir(), str(apdata)))
    fake_env = os.environ.get("MINIMAX_FAKE_PIPELINE", "").strip().lower() in {
        "1", "true", "yes", "on"}
    checks.append(("真实模型模式", not args.rehearse and not fake_env,
                   "本轮标为桩管线冒烟" if args.rehearse or fake_env
                   else "MINIMAX_FAKE_PIPELINE 未在这条命令的环境里开启"))
    ok = True
    for name, passed, note in checks:
        print(f"[self-check] {'PASS' if passed else 'FAIL'}  {name}：{note}")
        if not passed:
            ok = False
    print(f"[self-check] {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 2


# ------------------------------------------------------------------ AC 结论落表

def fill_ac(report: Report, ctx: Context, failed: bool) -> None:
    """AC 结论：只登记**本次运行留下证据**的条目，其余留「未验」。"""
    facts = report.facts
    a = facts.get("A") or {}
    c = facts.get("C") or {}
    d = facts.get("D") or {}
    b = facts.get("B") or {}
    build = facts.get("build") or {}
    structure = facts.get("structure") or {}
    real = (facts.get("pipeline") or "").startswith("real")
    screenshots = facts.get("screenshots") or []
    a_close = (a.get("close") or {}).get("exit_code")
    c_close = (c.get("close") or {}).get("exit_code")
    restart = c.get("restart") or {}
    bundle = (d.get("bundle") or {})
    checks = d.get("checks") or {}

    def first_shot(prefix: str) -> str:
        return next((path for path in screenshots if Path(path).name.startswith(prefix)), "")

    if a.get("overview"):
        report.ac_set(
            "D5-AC1", "降级",
            "解压 EXE 起窗、概览页可用成立；环境为 R 降级（非全新系统，见「干净环境声明」）；"
            f"截图 `{first_shot('A-') or '（本轮未留下）'}`")
    elif a:
        report.ac_set("D5-AC1", "未验", "A 段没有走到概览页断言（见步骤失败项）")
    if a.get("health") and b.get("window_title"):
        log = a.get("instance", {}).get("log_path")
        occupied = ((c.get("occupied_port") or {}).get("dialog") or {}).get("body", "")
        overview = a.get("overview") or {}
        address = str(overview.get("address") or "")
        parts = [f"日志 `{log}` 有地址", f"窗口标题 `{b.get('window_title')}`"]
        parts.append(
            f"概览页「本地地址」`{address}`（与 /health.base_url 一致）"
            if address else "**概览页没有显示实际地址**")
        parts.append(f"显式端口占用提示：`{occupied}`" if occupied
                     else "显式端口占用本轮未验")
        status = "成立" if (address and occupied) else "部分成立"
        report.ac_set("D5-AC2", status, "；".join(parts))
    elif a.get("health"):
        report.ac_set(
            "D5-AC2", "部分成立",
            "日志有地址；显式端口占用报错有提示；概览页没有显示实际地址")
    if c.get("second_instance"):
        report.ac_set(
            "D5-AC3", "成立",
            f"第二实例提示：`{((c.get('second_instance') or {}).get('dialog') or {}).get('body', '')}`")
    if restart:
        report.ac_set(
            "D5-AC4", "成立",
            f"关窗退出码 A={a_close} C={c_close}；重启后会话状态 "
            f"`{restart.get('session_status')}`，产物 `{restart.get('artifact_path')}`")
    elif a_close == 0 or c_close == 0:
        report.ac_set(
            "D5-AC4", "部分成立",
            f"关窗后进程退出成立（退出码 A={a_close} C={c_close}）；"
            "重启后会话/产物仍在未验（没有重启实例）")
    if c.get("external_browser_url"):
        report.ac_set(
            "D5-AC5", "成立",
            f"外部 Chromium 打开 `{c.get('external_browser_url')}`；"
            f"未带凭据仍 401；Cookie={c.get('cookies')}")
    if build.get("top_level"):
        report.ac_set(
            "D5-AC6", "成立",
            f"zip 顶层 `{build.get('top_level')}`；会话/配置/日志落在 `{a.get('instance', {}).get('data_root')}`")
    if a.get("config") and a.get("overview"):
        report.ac_set(
            "D5-AC7", "成立",
            f"`{a['config']['path']}` 与内置模板逐字节一致；缺密钥提示见截图 `{first_shot('A-03') or '（本轮未留下）'}`")
    elif a.get("config"):
        report.ac_set(
            "D5-AC7", "部分成立",
            f"`{a['config']['path']}` 自动生成且与模板一致；概览页提示未验证")
    if a.get("health") and d.get("checks"):
        report.ac_set(
            "D5-AC8", "成立",
            f"env_path=`{a['health'].get('env_path')}`；data/ 无 .env（zip 与目录都没有）；"
            f"诊断包（含日志与会话副本）扫描 {checks.get('secret_values_scanned')} 个密钥值，"
            f"命中 {checks.get('secret_hits')}")
    if b.get("health"):
        report.ac_set(
            "D5-AC9", "成立" if real else "降级",
            ("A 段模板 → 覆盖 `data/config/agent.yaml` 并重启后，阶段 1 走真实模型；"
             if real else
             "A 段模板 → 覆盖 `data/config/agent.yaml` 并重启后，阶段 1 走的是桩管线（不是真实模型）；")
            + "概览页提示「改完配置需重启程序才生效」")
    if d.get("checks"):
        report.ac_set(
            "D5-AC10", "成立",
            f"`data/logs` 按日滚动；诊断包 {checks.get('entry_count')} 条目齐全、"
            f"密钥值命中 {checks.get('secret_hits')}、无媒体无 .env")
    if bundle:
        external = (c.get("external_bundle_download") or {})
        report.ac_set(
            "D5-AC11", "成立" if external else "部分成立",
            f"窗口内一键导出 `{bundle.get('path')}`（{bundle.get('bytes')} 字节，"
            f"捕获方式 `{bundle.get('capture')}`）；"
            + (f"外部 Chromium 真实下载 `{external.get('path')}`"
               f"（{external.get('bytes')} 字节，文件名 `{external.get('suggested_filename')}`）"
               if external else "外部浏览器真下载未验证"))
    if b.get("artifact"):
        report.ac_set(
            "D5-AC12", "成立" if real else "降级",
            ("真实 LLM：" if real else "**桩管线**（不是真实模型证据）：")
            + f"会话 `{b.get('session_id')}`，产物 {b['artifact'].get('bytes')} 字节；"
            f"复制/下载一致（`{b.get('delivery', {}).get('download_capture')}`）")
    report.ac_set(
        "D5-AC13", "未验（工作流已定义）",
        "`.github/workflows/ci.yml` 的 `exe-build`（windows-latest）已入库；本轮未触发 CI")
    if b.get("artifact"):
        report.ac_set(
            "D5-AC14", "成立",
            f"CDP `connect_over_cdp` 驱动真实窗口（标题 `{b.get('window_title')}`），"
            "A–D 全程用同一窗口；窗口被自动验证")
    elif b.get("window_title"):
        report.ac_set(
            "D5-AC14", "部分成立",
            f"CDP 已驱动真实窗口（标题 `{b.get('window_title')}`），"
            "真实阶段 1/修订已走；闭环（帧提交/阶段 2/C/D）未完成，见失败项")
    else:
        report.ac_set(
            "D5-AC14", "未验",
            ("A 段已用 CDP 连上真实窗口（A3 概览页断言通过）；"
             "但 B/C/D 没有跑，窗口自动化闭环未验"
             if a.get("overview") else "本轮没有连上真实窗口（见步骤失败项）"))
    if structure:
        report.ac_set(
            "D5-AC15", "成立",
            f"zip 无安装器条目（{build.get('installer_entries') or '无'}）；"
            f"路由 {structure.get('routes')}（无设置页）；"
            f"托盘 {structure.get('tray_mentions')} 处、自动更新 {structure.get('updater_mentions')} 处；"
            f"签名状态 `{build.get('signature')}`；"
            f"向导改动 `{structure.get('wizard_changed_vs_main') or '无'}`")
    report.ac_set(
        "D5-AC16",
        "成立" if (
            structure.get("adr_0035")
            and structure.get("frontend_doc_exe_section")
            and structure.get("workbench_freeze_section")
        ) else "部分成立",
        f"ADR 0035={structure.get('adr_0035') or '缺失'}；"
        f"frontend.md EXE 小节={structure.get('frontend_doc_exe_section')}；"
        f"workbench.md 冻结形态节={structure.get('workbench_freeze_section')}；"
        "CONTEXT.md 无新增词条")
    if failed:
        for key, entry in list(report.ac.items()):
            if entry["status"] == "成立" and not report.steps:
                entry["status"] = "未验"


# ------------------------------------------------------------------ 命令行

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="D5 干净环境验收驱动器（#242）")
    parser.add_argument("--zip", default=str(ROOT / "build-exe" / f"{PORTABLE_NAME}.zip"),
                        help="交付 zip（默认 <repo>/build-exe/MiniMax-H3-prompt.zip）")
    parser.add_argument("--out-dir", default=None,
                        help="证据目录（默认 <repo>/output/accept-d5/<UTC>）")
    parser.add_argument("--work-dir", default=None,
                        help="解压临时目录（默认 %%TEMP%%/accept-d5/<UTC>）")
    parser.add_argument("--env-file", default=str(ROOT / ".env"),
                        help="仓库 .env（取值做脱敏扫描，并复制到 %%APPDATA%%）")
    parser.add_argument(
        "--config", "--agent-config", dest="config", default=None,
        help="可选：覆盖进 <data-root>/config/agent.yaml 的配置（真实模型闭环通常需要"
             "显式传主树的 config/agent.yaml）。不传 = 不覆盖，用 EXE 首次启动生成的"
             "内置模板。")
    parser.add_argument("--real-appdata", default=os.environ.get("APPDATA", ""),
                        help="B/C/D 段用的真实 %%APPDATA%%")
    parser.add_argument("--image", default=None, help="首帧真实图片")
    parser.add_argument("--image-last", default=None, help="尾帧真实图片（默认同一张）")
    parser.add_argument("--plot", default=DEFAULT_PLOT)
    parser.add_argument("--revision", default=DEFAULT_REVISION)
    parser.add_argument("--duration", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=1800.0,
                        help="单步等待真实模型的秒数上限")
    parser.add_argument("--debug-port", type=int, default=DEFAULT_DEBUG_PORT)
    parser.add_argument("--steps", default="ABCD", help="要跑哪几段（默认 ABCD）")
    parser.add_argument("--rehearse", action="store_true",
                        help="子进程开桩管线（MINIMAX_FAKE_PIPELINE=1），不是真实证据")
    parser.add_argument("--keep-env-file", action="store_true",
                        help="收尾时不删除写进 %%APPDATA%% 的 .env")
    parser.add_argument("--self-check", action="store_true",
                        help="只查环境与结构，不解压、不起进程、不调模型")
    parser.add_argument("--command", default="", help="报告里记录的实际命令行")
    args = parser.parse_args(argv)
    args.remove_env_file = not args.keep_env_file
    return args


def prepare_apdata(ctx: Context) -> Path:
    """把仓库 .env 复制到 ``%APPDATA%\\MiniMax-H3-prompt\\.env``（先备份已有的）。"""
    source = Path(ctx.args.env_file).resolve()
    secrets = secret_values(parse_env_file(source))
    _expect(bool(secrets), f"{source} 里没有可用的密钥值（*_API_KEY 等）")
    target_dir = Path(ctx.args.real_appdata) / "MiniMax-H3-prompt"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / ".env"
    backup = target.with_name(".env.accept-d5-backup")
    if target.is_file() and not backup.exists():
        shutil.move(str(target), str(backup))
    shutil.copyfile(source, target)
    return target


# ---------------------------------------------------------------- 配置来源核对

def config_facts(text: str) -> dict[str, Any]:
    """从 agent.yaml 文本里抽端点/模型事实（只读文本，不引 yaml 依赖）。"""
    return {
        "models": re.findall(r"^\s*model:\s*(\S+)", text, re.MULTILINE),
        "base_urls": re.findall(r"^\s*base_url:\s*(\S+)", text, re.MULTILINE),
        "providers": re.findall(r"^\s*provider:\s*(\S+)", text, re.MULTILINE),
        "contains_dashscope": EXPECTED_CONFIG_MARK in text.lower(),
        "contains_forbidden_host": FORBIDDEN_MODEL_HOST in text.lower(),
    }


def install_config_file(source: Path, data_root: Path) -> dict[str, Any]:
    """把真实配置覆盖进 ``<data-root>/config/agent.yaml`` —— **必须在起 EXE 之前**。

    ``config = Config()`` 是 import 期单例：进程起来后再改文件不生效。这里先落盘，
    启动时的 bootstrap 见文件已存在就一个字节都不动。
    """
    source = Path(source).resolve()
    _expect(source.is_file(), f"--config 指向的文件不存在：{source}")
    raw = source.read_bytes()
    text = raw.decode("utf-8", errors="replace")
    _expect(
        EXPECTED_CONFIG_MARK in text.lower(),
        f"{source} 里没有 {EXPECTED_CONFIG_MARK}：这不是主树里用户改过的配置，"
        f"用它起 B 段会打到旧端点（{FORBIDDEN_MODEL_HOST}）。",
    )
    target = Path(data_root) / "config" / "agent.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)
    _expect(target.read_bytes() == raw, f"覆盖后内容不一致：{target}")
    return {
        "source": str(source),
        "source_bytes": len(raw),
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "target": str(target),
        "target_bytes": target.stat().st_size,
        "target_sha256": _sha256(target),
        "match": True,
        **config_facts(text),
    }


TEMPLATE_CONFIG_LABEL = "EXE 生成的模板（未覆盖）"


def prepare_config(config: str | None, data_root: Path) -> dict[str, Any]:
    """起 EXE 之前的配置处置：显式 ``--config`` 才覆盖；否则什么都不写。"""
    if config:
        facts = install_config_file(Path(config), data_root)
        facts["explicit"] = True
        return facts
    return {
        "source": TEMPLATE_CONFIG_LABEL,
        "explicit": False,
        "target": str(Path(data_root) / "config" / "agent.yaml"),
    }


def config_target_facts(data_root: Path, source_label: str) -> dict[str, Any]:
    """起 EXE 之后核对**实际生效**的 ``data/config/agent.yaml``。"""
    target = Path(data_root) / "config" / "agent.yaml"
    _expect(target.is_file(), f"起 EXE 后没有找到 {target}")
    raw = target.read_bytes()
    return {
        "source": source_label,
        "target": str(target),
        "target_bytes": len(raw),
        "target_sha256": hashlib.sha256(raw).hexdigest(),
        **config_facts(raw.decode("utf-8", errors="replace")),
    }


def forbidden_host_hits(paths: Iterable[str | Path]) -> list[str]:
    """日志里出现旧端点即命中（返回命中的文件路径，不返回正文）。"""
    hits: list[str] = []
    for path in paths:
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if FORBIDDEN_MODEL_HOST in text.lower():
            hits.append(str(path))
    return hits


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
        return self_check(args)

    stamp = _stamp() + ("-rehearse" if args.rehearse else "")
    out_dir = (Path(args.out_dir) if args.out_dir
               else ROOT / "output" / "accept-d5" / stamp).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    work_dir = (Path(args.work_dir) if args.work_dir
                else Path(tempfile.gettempdir()) / "accept-d5" / stamp).resolve()
    zip_path = Path(args.zip).resolve()
    report = Report()
    report.facts.update({
        "out_dir": str(out_dir),
        "started_at": _utc_now(),
        "command": args.command or " ".join(sys.argv),
        "pipeline": "rehearse（桩管线，不是真实模型证据）" if args.rehearse else "real（真实 LLM）",
    })
    _expect(zip_path.is_file(), f"交付 zip 不存在：{zip_path}")
    _expect(Path(args.real_appdata).is_dir(), f"APPDATA 不存在：{args.real_appdata}")
    image = Path(args.image).resolve() if args.image else None
    image_last = Path(args.image_last).resolve() if args.image_last else image
    _expect(image is not None and image.is_file(),
            "用 --image 指定一张真实图片（不要用测试夹具）")
    _expect(image_last is not None and image_last.is_file(), "--image-last 不存在")
    report.facts["image"] = {
        "path": str(image), "bytes": image.stat().st_size, "sha256": _sha256(image)}
    report.facts["image_last"] = {
        "path": str(image_last), "bytes": image_last.stat().st_size,
        "sha256": _sha256(image_last)}
    report.facts["plot"] = args.plot
    report.facts["revision"] = args.revision
    report.facts["variant"] = "FL2VA"
    report.facts["duration"] = args.duration

    from playwright.sync_api import sync_playwright

    started = time.monotonic()
    ctx: Context | None = None
    failed = False
    try:
        extract_started = time.monotonic()
        portable = extract_zip(zip_path, work_dir)
        extract_ms = int((time.monotonic() - extract_started) * 1000)
        apdata_empty = work_dir / "appdata-empty"
        apdata_empty.mkdir(parents=True, exist_ok=True)
        ctx = Context(
            args=args, report=report, out_dir=out_dir, zip_path=zip_path,
            portable=portable, work_dir=work_dir, apdata_empty=apdata_empty,
            extra_env={"MINIMAX_FAKE_PIPELINE": "1"} if args.rehearse else {},
        )
        report.facts["portable"] = str(portable)
        report.facts["build"] = {
            **zip_facts(zip_path),
            "zip": str(zip_path),
            "zip_bytes_human": _human_bytes(zip_path.stat().st_size),
            "zip_sha256": _sha256(zip_path),
            "extract_ms": extract_ms,
            "exe_bytes": _human_bytes(ctx.exe.stat().st_size),
            "signature": signature_status(ctx.exe),
            "entries": len(zipfile.ZipFile(zip_path).namelist()),
        }
        report.facts["structure"] = structure_facts(ROOT)
        report.facts["environment"] = {
            "os": platform_text(),
            "variables": list(restricted_env(apdata_empty)),
            "path": SYSTEM_PATH,
            "apdata_empty": str(apdata_empty),
            "apdata_real": str(Path(args.real_appdata)),
        }

        steps = args.steps.upper()
        with sync_playwright() as playwright:
            # Playwright 必须早于 A 段就位：A 也要用 CDP 读真实窗口。
            ctx.playwright = playwright
            if "A" in steps:
                step_a(ctx)
            if "B" in steps:
                prepare_apdata(ctx)
                # 显式 --config 才覆盖；必须在 step_b 的 ctx.start 之前落盘。
                report.facts["config_source"] = prepare_config(
                    args.config, ctx.data_root)
                print(
                    f"[accept] 配置来源：{report.facts['config_source'].get('source')}"
                    f"（explicit={report.facts['config_source'].get('explicit')}）",
                    flush=True)
                step_b(ctx, image, image_last)
                if "C" in steps:
                    step_c(ctx)
                if "D" in steps:
                    step_d(ctx)
            elif "C" in steps or "D" in steps:
                raise AcceptanceError("C/D 需要 B 留下的会话与窗口：请把 B 一起选上")
        failed = bool(report.failures)
    except Exception as exc:  # noqa: BLE001 - 失败也要落一份报告
        failed = True
        report.failures.append(f"{type(exc).__name__}: {exc}")
        print(f"[accept] 未预期失败：{type(exc).__name__}: {exc}", flush=True)
    finally:
        report.facts["finished_at"] = _utc_now()
        if ctx is not None:
            try:
                copied = copy_session_evidence(ctx)
                print(f"[accept] 证据拷贝 {len(copied)} 份", flush=True)
            except Exception as exc:  # noqa: BLE001
                report.facts["copied_evidence_error"] = f"{type(exc).__name__}: {exc}"
            summary = cleanup(ctx)
            report.facts["cleanup"] = summary
            report.facts["elapsed_s"] = round(time.monotonic() - started, 1)
        if ctx is not None:
            fill_ac(report, ctx, failed)
        write_report(report, out_dir)
    if report.failures:
        print("验收未全部通过：", flush=True)
        for failure in report.failures:
            print(f"- {failure}", flush=True)
        print(f"报告：{out_dir}", flush=True)
        return 1
    print(f"全部步骤通过：{len(report.steps)} 步；报告在 {out_dir}", flush=True)
    return 0


def platform_text() -> str:
    import platform

    return f"{platform.platform()} / {platform.machine()}"


if __name__ == "__main__":
    raise SystemExit(main())
