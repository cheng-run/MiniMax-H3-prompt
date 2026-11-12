"""诊断包组装（#239 / spec #235 §8.2）：一个 zip，含清单、环境、脱敏配置、日志、
会话状态、事件流与文本产物。

为什么住在包根而不是 ``api/``：组装是纯文件/字典操作，不依赖 FastAPI；路由只做
「读 ``app.state`` → 调这里 → 包成响应」。把它放进 api 会让「诊断包能不能组装」
这种事实必须起一个 HTTP 服务才能验证。

三条边界（spec §8.2 的排除面）：

1. **不含密钥值**：配置快照按「键名像密钥」与「值等于某个环境密钥值」双重脱敏，
   其余文本（日志、会话、产物）再统一过一次值替换。自检用例用假 key 咬。
2. **不含 ``%APPDATA%`` 的 .env**：配置快照只取 YAML，不读环境文件、不写它的路径。
3. **不含素材图片/视频**：只收登记表里的**文本**产物（``*.md``）；``assets/``、
   ``final.mp4`` 一概不进包。

组装全在内存（``io.BytesIO``）：诊断包是一次性下载物，落临时文件只多一个要清理的
失败面（spec §8.2 的「不要临时文件」）。
"""
from __future__ import annotations

import io
import json
import os
import platform
import sys
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import logging_setup, paths
from .workbench_store import EVENTS_FILENAME, SESSION_FILENAME, WorkbenchError

MANIFEST_NAME = "manifest.json"
ENVIRONMENT_NAME = "environment.txt"
CONFIG_NAME = "config.json"
LOGS_PREFIX = "logs/"
SESSIONS_PREFIX = "sessions/"
ARTIFACTS_PREFIX = "artifacts/"

# 这些键名按「密钥」处理，值一律 ``<redacted>``。按后缀/全等认，不列死名单：
# ``api_key_env`` 是**键名**不是密钥值，故意不命中（排障要看它）。
_SENSITIVE_KEY_NAMES = frozenset({"key", "token", "secret", "password", "credential"})
_SENSITIVE_KEY_SUFFIXES = ("_key", "_token", "_secret", "_password", "_credential")

# WebView2 运行时在注册表里的客户端 GUID（微软官方 Evergreen 文档的固定值）。
_WEBVIEW2_CLIENT_GUID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
_WEBVIEW2_UNREADABLE = "不可读"


def _is_sensitive_key(key: object) -> bool:
    name = str(key).strip().lower()
    return name in _SENSITIVE_KEY_NAMES or name.endswith(_SENSITIVE_KEY_SUFFIXES)


def sanitize_config(raw: Any, *, secrets: Iterable[str] = ()) -> Any:
    """配置快照：只留键名与非敏感值；密钥类键与密钥值换成占位符。

    递归处理嵌套映射与列表——配置的敏感项可能藏在 ``models.primary`` 这种节里，
    只扫顶层等于给密钥留后门。
    """
    values = tuple(secrets)
    if isinstance(raw, Mapping):
        result: dict[str, Any] = {}
        for key, value in raw.items():
            if _is_sensitive_key(key):
                result[str(key)] = logging_setup.REDACTION_PLACEHOLDER
            else:
                result[str(key)] = sanitize_config(value, secrets=values)
        return result
    if isinstance(raw, (list, tuple)):
        return [sanitize_config(item, secrets=values) for item in raw]
    if isinstance(raw, str):
        return logging_setup.redact_text(raw, values)
    return raw


def webview2_version() -> str:
    """WebView2 运行时版本；读不到（非 Windows / 未安装 / 注册表被锁）返回「不可读」。

    这是环境信息里唯一需要**读系统**的一项，失败不该让整个诊断包导不出来。
    """
    if os.name != "nt":
        return _WEBVIEW2_UNREADABLE
    try:
        import winreg
    except ImportError:  # pragma: no cover - Windows 上一定有
        return _WEBVIEW2_UNREADABLE
    key_path = rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{_WEBVIEW2_CLIENT_GUID}"
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for view in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
            try:
                with winreg.OpenKey(hive, key_path, 0, winreg.KEY_READ | view) as handle:
                    value, _kind = winreg.QueryValueEx(handle, "pv")
            except OSError:
                continue
            if value:
                return str(value)
    return _WEBVIEW2_UNREADABLE


def environment_text() -> str:
    """``environment.txt`` 的内容：OS、Python、WebView2、架构（spec §8.2 的四项）。"""
    return "\n".join((
        f"OS: {platform.platform()}",
        f"OS 版本: {platform.version()}",
        f"Python: {platform.python_version()}（{sys.version.splitlines()[0]}）",
        f"架构: {platform.machine()}",
        f"WebView2: {webview2_version()}",
    )) + "\n"


def bundle_filename(version: str, when: datetime | None = None) -> str:
    """下载文件名：版本 + 生成时刻（同一分钟导出两份也不会互相覆盖文件名）。"""
    stamp = (when or datetime.now()).strftime("%Y%m%d-%H%M%S")
    safe_version = (version or "unknown").strip() or "unknown"
    return f"minimax-h3-diagnostics-{safe_version}-{stamp}.zip"


def _config_raw() -> dict[str, Any]:
    """当前生效的 YAML 快照。配置坏了也要能导出诊断包——那正是要诊断的现场。"""
    try:
        from .config import config
    except Exception:  # noqa: BLE001 - 诊断包的职责是「拿得到现场」，不是拦住现场
        return {}
    raw = getattr(config, "raw", None)
    return dict(raw) if isinstance(raw, Mapping) else {}


def _session_dirs(store: Any) -> list[tuple[str, Path]]:
    """``sessions_root`` 下所有会话目录（``(session_id, 目录)``），按标识排序。"""
    root_value = getattr(store, "root", None)
    # 空值不是「当前目录」：``Path("")`` 会变成 ``.``，那会去扫进程的工作目录并把
    # 别处的会话/产物收进诊断包。拿不到根就当作「没有会话」。
    if root_value is None or not str(root_value).strip():
        return []
    root = Path(root_value)
    if not root.is_dir():
        return []
    root_resolved = root.resolve()
    found: list[tuple[str, Path]] = []
    for state_file in sorted(root.glob(f"**/{SESSION_FILENAME}")):
        directory = state_file.parent
        try:
            session_id = directory.resolve().relative_to(root_resolved).as_posix()
        except ValueError:
            continue  # 解析到根外（符号链接）：宁可少一条，不越界
        found.append((session_id, directory))
    return found


def _text_artifacts(store: Any, session_id: str) -> list[tuple[str, str]]:
    """该会话登记表里的**文本**产物（``*.md``），返回 ``(名字, 正文)``。

    读单份失败不拦整包：大产物（>1 MiB）与坏文件都跳过，诊断包里少一件比导不出来强。
    """
    try:
        detail = store.get_session(session_id)
    except (WorkbenchError, ValueError, OSError):
        return []
    items: list[tuple[str, str]] = []
    for info in getattr(detail, "artifacts", []) or []:
        name = str(getattr(info, "name", ""))
        if not name.endswith(".md") or getattr(info, "kind", "") == "video":
            continue
        try:
            # 存储层返回 ``(ArtifactInfo, 正文)``（与 content 端点同一份实现）。
            _info, text = store.read_artifact_text(f"{session_id}/{name}")
        except (WorkbenchError, ValueError, OSError):
            continue
        items.append((name, text))
    return items


def _add_text(archive: zipfile.ZipFile, name: str, text: str,
              secrets: tuple[str, ...]) -> None:
    # 每个条目都过一遍值替换：日志本来就在写入前脱敏，但会话/产物是用户内容，
    # 不能假设它们没带过密钥（比如用户把 key 贴进了剧情里）。
    archive.writestr(name, logging_setup.redact_text(text, secrets))


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def build_bundle(
    store: Any, *,
    version: str = "",
    startup: Any = None,
    log_dir: str | Path | None = None,
    retention_days: int | None = None,
    config_raw: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> tuple[bytes, str]:
    """组装诊断包，返回 ``(zip 字节, 下载文件名)``。

    ``startup`` 是 ``api.__main__.StartupInfo`` 那样的鸭子类型；缺省（测试 app 没挂
    ``state.startup``）时按 ``paths.is_frozen()`` 兜底，端口留 ``null``，不抛异常。
    """
    generated = now or datetime.now()
    retention = int(
        logging_setup.DEFAULT_RETENTION_DAYS if retention_days is None else retention_days)
    retention = max(1, retention)
    secrets = logging_setup.secret_values()
    resolved_log_dir = (
        logging_setup.resolve_log_dir() if log_dir is None else Path(log_dir))
    log_files = logging_setup.recent_log_files(
        resolved_log_dir, retention, today=generated.date())

    frozen = bool(getattr(startup, "frozen", paths.is_frozen()))
    data_root = getattr(startup, "data_root", None) or paths.data_root()
    manifest = {
        "version": version or "未知",
        "generated_at": generated.isoformat(timespec="seconds"),
        "data_root": str(data_root),
        "port": getattr(startup, "port", None),
        "base_url": getattr(startup, "base_url", None),
        "form": "冻结" if frozen else "开发",
        "log_window": {
            "retention_days": retention,
            "from": (generated.date() - timedelta(days=retention - 1)).isoformat(),
            "to": generated.date().isoformat(),
            "files": [path.name for path in log_files],
        },
    }
    config_snapshot = sanitize_config(
        _config_raw() if config_raw is None else config_raw, secrets=secrets)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        _add_text(archive, MANIFEST_NAME,
                  json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", secrets)
        _add_text(archive, ENVIRONMENT_NAME, environment_text(), secrets)
        _add_text(archive, CONFIG_NAME,
                  json.dumps(config_snapshot, ensure_ascii=False, indent=2) + "\n",
                  secrets)

        for log_path in log_files:
            text = _read_text(log_path)
            if text is not None:
                _add_text(archive, f"{LOGS_PREFIX}{log_path.name}", text, secrets)

        for session_id, directory in _session_dirs(store):
            state = _read_text(directory / SESSION_FILENAME)
            if state is not None:
                _add_text(archive, f"{SESSIONS_PREFIX}{session_id}/{SESSION_FILENAME}",
                          state, secrets)
            events = _read_text(directory / EVENTS_FILENAME)
            if events is not None:
                _add_text(archive, f"{SESSIONS_PREFIX}{session_id}/{EVENTS_FILENAME}",
                          events, secrets)
            for name, text in _text_artifacts(store, session_id):
                _add_text(archive, f"{ARTIFACTS_PREFIX}{session_id}/{name}", text, secrets)

    return buffer.getvalue(), bundle_filename(version, generated)
