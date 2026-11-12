"""运行日志落盘（#239 / spec #235 §8.1）：按日滚动、保留 N 天、写入前脱敏。

为什么单独一个模块而不是塞进 ``observability.py``：那里的 ``RunLog`` 是**一次运行**
一份 JSONL（结构化事件、按运行分段，只有 CLI 用）；这里是**服务进程**的应用日志
（按日滚动、跨运行持续、uvicorn 与业务 logger 共用）。两者的生命周期、文件格式与
保留策略都不同，合在一起会让「关掉运行日志」连带关掉排障所需的启动信息。

三条不变式：

1. **脱敏在写入前完成**（spec §8.1）：靠 ``RedactionFilter`` 按**值**替换，不靠事后
   扫文件。事后扫无法覆盖「写了一半崩溃」的窗口，也会把已落盘的密钥留在日志里。
2. **日志写失败不影响主流程**：目录不可写时退回 stderr 说一句，服务照常启动——日志
   是观测不是业务（与 ``RunLog`` 同口径）。
3. **测试默认不落盘**：``H3_LOG_DIR=""`` 是显式关闭开关（``tests/conftest.py`` 设它），
   否则全量用例会把日志写进仓库 ``logs/``，干净检出上留下垃圾文件。
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Mapping

from . import paths

# 日志目录开关：不设 = 落 ``paths.logs_dir()``；设成空串 = **不落盘**；设成路径 = 用它。
# 三种语义靠「键在不在环境里」区分，不用默认值兜——``H3_LOG_DIR=`` 与未设必须可分辨，
# 否则测试开关（空串）会被当成「未设」写回仓库 logs/。
LOG_DIR_ENV = "H3_LOG_DIR"

# 文件名与保留窗口（spec §8.1：``app-YYYYMMDD.log``，保留最近 N 天）。
LOG_FILE_PREFIX = "app-"
LOG_FILE_SUFFIX = ".log"
DEFAULT_RETENTION_DAYS = 7

# 密钥值替换成的占位符；大小写与 spec §8.2 的写法一致。
REDACTION_PLACEHOLDER = "<redacted>"

# 哪些环境变量名算「密钥」：按后缀认，不列死名单——换网关（``MINIMAX_API_KEY`` /
# ``OPENAI_API_KEY`` / 自定义 ``*_TOKEN``）不该回去改这个表。
# **这是全仓唯一一份密钥后缀表**：``diagnostics`` 的配置键名匹配与 ``scripts/accept_d5.py``
# 的验收扫描都从它派生（#243 两轴复审抓出三处各抄一份、还差了 ``_CREDENTIAL`` 一项）。
SENSITIVE_SUFFIXES: tuple[str, ...] = (
    "_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_CREDENTIAL")

# 短于此长度的值不做替换：``*_TOKEN=1`` 这类值替换掉会把日志里的普通数字/单词打碎，
# 而真正的密钥（各家 SDK 的 key/token）都远长于它。
_MIN_SECRET_CHARS = 6

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def _today() -> date:
    """当天日期。单独成函数是为了用例能把「跨日」真正跑一遍（monkeypatch 它）。"""
    return date.today()


def log_file_name(day: date) -> str:
    return f"{LOG_FILE_PREFIX}{day:%Y%m%d}{LOG_FILE_SUFFIX}"


def log_file_path(log_dir: str | Path, day: date | None = None) -> Path:
    return Path(log_dir) / log_file_name(day or _today())


def _parse_log_day(path: Path) -> date | None:
    """从 ``app-YYYYMMDD.log`` 取日期；不是本格式返回 None（保留策略不碰别的文件）。"""
    if not path.name.startswith(LOG_FILE_PREFIX) or not path.name.endswith(LOG_FILE_SUFFIX):
        return None
    stamp = path.name[len(LOG_FILE_PREFIX):-len(LOG_FILE_SUFFIX)]
    try:
        return datetime.strptime(stamp, "%Y%m%d").date()
    except ValueError:
        return None


def resolve_log_dir(environ: Mapping[str, str] | None = None) -> Path | None:
    """解析日志目录；``None`` 表示本次进程**不落盘**（spec #239 的显式开关）。"""
    env = os.environ if environ is None else environ
    if LOG_DIR_ENV in env:
        raw = str(env[LOG_DIR_ENV]).strip()
        return Path(raw).expanduser() if raw else None
    return paths.logs_dir()


def secret_values(environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """当前环境里所有密钥类变量的**值**（脱敏按值替换，不按变量名/行匹配）。

    长的排前面：两个 key 的值互为前缀时，先替换短的会在日志里留下半截真实密钥。
    """
    env = os.environ if environ is None else environ
    found: set[str] = set()
    for name, value in env.items():
        if not name.upper().endswith(SENSITIVE_SUFFIXES):
            continue
        text = str(value).strip()
        if len(text) >= _MIN_SECRET_CHARS:
            found.add(text)
    return tuple(sorted(found, key=len, reverse=True))


def redact_text(text: str, secrets: Iterable[str]) -> str:
    """把文本里出现的每个密钥值换成占位符（其余内容一字不动）。"""
    for value in secrets:
        text = text.replace(value, REDACTION_PLACEHOLDER)
    return text


class RedactionFilter(logging.Filter):
    """日志 Filter：消息、参数与异常栈里的密钥值在**格式化之前**被替换。

    为什么不靠 Formatter：Filter 能同时改 ``record.msg`` / ``record.args`` 与
    ``exc_text``，这样任何 handler（文件或控制台）拿到的都是已脱敏的记录；只包 Formatter
    的话，换了 handler 就漏一处。
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 - logging 的接口名
        secrets = secret_values()
        if not secrets:
            return True
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - 坏参数只表示「这条消息读不出来」，不该炸 handler
            return True
        redacted = redact_text(message, secrets)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        if record.exc_info:
            # **不能**只看 ``record.exc_text`` 已存在就跳过：别的 handler（pytest 的
            # caplog）可能已经格式化过同一条 record，把未脱敏的栈缓存进了 exc_text。
            # 这里统一重写一遍，handler 顺序就不再影响脱敏结果（实测踩过）。
            if not record.exc_text:
                record.exc_text = "".join(
                    traceback.format_exception(*record.exc_info))
            record.exc_text = redact_text(record.exc_text, secrets)
        if record.stack_info:
            record.stack_info = redact_text(record.stack_info, secrets)
        return True


def prune_old_logs(
    log_dir: str | Path, retention_days: int = DEFAULT_RETENTION_DAYS,
    *, today: date | None = None,
) -> list[Path]:
    """删掉保留窗口之外的日志文件；返回被删的路径（用例与排障都用得着）。

    窗口按**文件名里的日期**算而不是 mtime：拷目录、改系统时间都不会让保留策略失真。
    """
    directory = Path(log_dir)
    if not directory.is_dir():
        return []
    cutoff = (today or _today()) - timedelta(days=max(1, int(retention_days)) - 1)
    removed: list[Path] = []
    for path in sorted(directory.iterdir()):
        day = _parse_log_day(path)
        if day is None or day >= cutoff:
            continue
        try:
            path.unlink()
        except OSError as exc:  # 删不掉不该拦住写日志
            print(f"[日志] 清理旧日志失败 {path}：{exc}", file=sys.stderr)
            continue
        removed.append(path)
    return removed


def recent_log_files(
    log_dir: str | Path | None, retention_days: int = DEFAULT_RETENTION_DAYS,
    *, today: date | None = None,
) -> list[Path]:
    """保留窗口内的日志文件（诊断包按它取 ``logs/*.log``，spec §8.2）。"""
    if log_dir is None:
        return []
    directory = Path(log_dir)
    if not directory.is_dir():
        return []
    cutoff = (today or _today()) - timedelta(days=max(1, int(retention_days)) - 1)
    return [
        path for path in sorted(directory.glob(f"{LOG_FILE_PREFIX}*{LOG_FILE_SUFFIX}"))
        if (day := _parse_log_day(path)) is not None and day >= cutoff
    ]


class DailyFileHandler(logging.StreamHandler):
    """按日滚动的文件 handler：每天一个 ``app-YYYYMMDD.log``，开新文件时清旧文件。

    为什么不用 ``TimedRotatingFileHandler``：它的轮转命名是 ``base.log.YYYY-MM-DD``，
    与 spec §8.1 钉死的 ``app-YYYYMMDD.log`` 不同形；改 suffix 也拼不出这个形态。
    自己实现只有三件事——按日期选文件、跨日重开、重开时清窗口外的旧文件。
    """

    # 让 ``reset_file_logging`` 认得出本模块挂的 handler（不误删 pytest/caplog 的）。
    _h3_file_log = True

    def __init__(self, log_dir: str | Path,
                 retention_days: int = DEFAULT_RETENTION_DAYS,
                 *, when: date | None = None) -> None:
        # 不走 ``StreamHandler.__init__``：它在没传 stream 时把 ``self.stream`` 默认成
        # ``sys.stderr``，而紧接着的 ``_open`` 会关掉当前 stream——那等于把调用方的
        # stderr 关掉（pytest 捕获对象因此报 "I/O operation on closed file"，实测）。
        logging.Handler.__init__(self)
        self.stream = None
        self.log_dir = Path(log_dir)
        self.retention_days = max(1, int(retention_days))
        self._day: date | None = None
        self._stream_lock = threading.Lock()
        self._open(when or _today())

    @property
    def current_path(self) -> Path | None:
        return None if self._day is None else log_file_path(self.log_dir, self._day)

    def _open(self, day: date) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        prune_old_logs(self.log_dir, self.retention_days, today=day)
        self._close_stream()
        self.stream = open(  # noqa: SIM115 - 生命周期由 close/跨日重开管理
            log_file_path(self.log_dir, day), "a", encoding="utf-8", newline="\n")
        self._day = day

    def _close_stream(self) -> None:
        stream, self.stream = getattr(self, "stream", None), None
        if stream is None:
            return
        try:
            stream.close()
        except OSError:
            pass

    def emit(self, record: logging.LogRecord) -> None:
        with self._stream_lock:
            today = _today()
            if self._day != today:
                try:
                    self._open(today)
                except OSError as exc:
                    # 换日时磁盘写不进去：这条日志丢掉，但别让异常从 handler 里冒出去。
                    self.handleError(record)
                    print(f"[日志] 打开 {log_file_path(self.log_dir, today)} 失败：{exc}",
                          file=sys.stderr)
                    return
            super().emit(record)

    def close(self) -> None:
        self._close_stream()
        super().close()


class _ConsoleHandler(logging.Handler):
    """控制台输出：**每次 emit 现取 ``sys.stderr``**，不持有构造时的流。

    为什么不用 ``StreamHandler(sys.stderr)``：pytest 的捕获对象在用例结束后会关闭，
    固定引用会让之后任何一条日志（包括 pytest 自己的收尾）打到已关闭的流上，触发
    ``handleError`` 再去写同一个坏流——最坏会把解释器弄成 "lost sys.stderr"。
    """

    _h3_console_log = True
    # ``logging.StreamHandler`` 的类属性；本类直接继承 ``Handler``，要自己声明
    # （mypy 会抓 attr-defined，运行时靠它拼行尾）。
    terminator = "\n"

    def emit(self, record: logging.LogRecord) -> None:
        try:
            stream = sys.stderr
            if stream is None:
                return
            stream.write(self.format(record) + self.terminator)
            stream.flush()
        except Exception:  # noqa: BLE001 - 控制台写不进去只是少一行显示，不该升级
            return


def _make_formatter() -> logging.Formatter:
    return logging.Formatter(_LOG_FORMAT)


def reset_file_logging() -> None:
    """摘掉本模块挂到 root 的 handler（生产只在重配时用；测试每条用例后清场）。"""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_h3_file_log", False) or getattr(
                handler, "_h3_console_log", False):
            root.removeHandler(handler)
            try:
                handler.close()
            except Exception:  # noqa: BLE001 - 收尾失败不该反过来影响调用方
                pass


def setup_file_logging(
    log_dir: str | Path | None = None, *,
    retention_days: int | None = None,
    level: int = logging.INFO,
) -> Path | None:
    """配置根 logger：文件日志（按日滚动 + 脱敏）＋ 控制台输出。

    返回实际使用的日志目录；``None`` 表示本次进程按开关**不落盘**。重复调用是安全的
    （先摘掉上一次的 handler），测试与 ``--data-root`` 切换都靠它。
    """
    target = resolve_log_dir() if log_dir is None else Path(log_dir)
    if target is None:
        return None
    retention = DEFAULT_RETENTION_DAYS if retention_days is None else int(retention_days)
    try:
        handler = DailyFileHandler(target, retention_days=retention)
    except OSError as exc:
        # 目录不可写是环境问题，不是启动失败：日志是观测，不能把服务一起拖死。
        print(f"[日志] 无法写入 {target}：{exc}（本次运行不落盘）", file=sys.stderr)
        return None
    handler.setFormatter(_make_formatter())
    handler.addFilter(RedactionFilter())

    # uvicorn 用 ``log_config=None`` 启动后不再自带 handler，靠这一份控制台输出保住
    # 开发期的访问日志；它同样挂脱敏 Filter（终端里也不该出现密钥）。
    console = _ConsoleHandler()
    console.setFormatter(_make_formatter())
    console.addFilter(RedactionFilter())

    root = logging.getLogger()
    reset_file_logging()
    root.addHandler(console)
    root.addHandler(handler)
    if root.level == logging.NOTSET or root.level > level:
        root.setLevel(level)
    return target
