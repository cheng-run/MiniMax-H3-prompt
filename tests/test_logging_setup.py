"""运行日志落盘（#239 / spec #235 §8.1）：按日滚动、保留 N 天、写入前脱敏。

判据全部落在**磁盘上的文件**上，不读 handler 内部状态：改名、换实现只要文件名格式、
保留窗口与脱敏结果不变，这些用例就不该跟着改。
"""
from __future__ import annotations

import logging
import os
from datetime import date, timedelta

import pytest

from minimax_h3_prompt import logging_setup, paths

FAKE_KEY = "sk-fake-diagnostic-key-0123456789"
FAKE_TOKEN = "tok-fake-diagnostic-token-9876543210"


@pytest.fixture(autouse=True)
def _clean_handlers():
    """每条用例后摘掉本模块挂到 root 的 handler，避免写进下一条用例的 tmp_path。"""
    yield
    logging_setup.reset_file_logging()


def _read(path) -> str:
    return path.read_text(encoding="utf-8")


def test_daily_log_file_is_named_app_yyyymmdd(tmp_path):
    """文件名是 ``app-YYYYMMDD.log``（spec §8.1 的落盘格式）。"""
    target = logging_setup.setup_file_logging(tmp_path, retention_days=7)

    logging.getLogger("test.daily").info("hello-daily")

    logging_setup.reset_file_logging()
    expected = tmp_path / f"app-{date.today():%Y%m%d}.log"
    assert target == tmp_path
    assert expected.is_file()
    assert "hello-daily" in _read(expected)


def test_handler_rolls_to_a_new_file_when_the_day_changes(tmp_path, monkeypatch):
    """跨日时换新文件，旧文件原样留下（按日滚动，不是追加到同一个文件）。"""
    first_day = date(2026, 11, 11)
    monkeypatch.setattr(logging_setup, "_today", lambda: first_day)
    handler = logging_setup.DailyFileHandler(tmp_path, retention_days=7)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger("test.roll")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        logger.info("day-one")

        second_day = date(2026, 11, 12)
        monkeypatch.setattr(logging_setup, "_today", lambda: second_day)
        logger.info("day-two")
    finally:
        logger.removeHandler(handler)
        handler.close()

    assert "day-one" in _read(tmp_path / "app-20261111.log")
    assert "day-two" in _read(tmp_path / "app-20261112.log")
    assert "day-two" not in _read(tmp_path / "app-20261111.log")


def test_retention_keeps_last_n_days(tmp_path):
    """保留最近 N 天：第 N 天之前（含第 N 天）的文件被删。"""
    today = date(2026, 11, 12)
    for offset in range(10):
        day = today - timedelta(days=offset)
        (tmp_path / f"app-{day:%Y%m%d}.log").write_text("x", encoding="utf-8")

    removed = logging_setup.prune_old_logs(tmp_path, retention_days=7, today=today)

    remaining = sorted(path.name for path in tmp_path.glob("app-*.log"))
    assert len(remaining) == 7
    assert f"app-{(today - timedelta(days=6)):%Y%m%d}.log" in remaining
    assert f"app-{(today - timedelta(days=7)):%Y%m%d}.log" not in remaining
    assert len(removed) == 3


def test_retention_ignores_files_that_are_not_dated_logs(tmp_path):
    """同目录里别的文件（非本格式）不许被日志保留策略误删。"""
    keep = tmp_path / "notes.txt"
    keep.write_text("keep", encoding="utf-8")
    stale = tmp_path / "app-20200101.log"
    stale.write_text("stale", encoding="utf-8")

    logging_setup.prune_old_logs(tmp_path, retention_days=7, today=date(2026, 11, 12))

    assert keep.is_file()
    assert not stale.exists()


def test_secret_values_are_redacted_before_write(tmp_path, monkeypatch):
    """脱敏按**值**替换：消息里没写变量名，只要带了密钥值也要替换成 ``<redacted>``。"""
    monkeypatch.setenv("MINIMAX_API_KEY", FAKE_KEY)
    monkeypatch.setenv("SOME_SERVICE_TOKEN", FAKE_TOKEN)

    logging_setup.setup_file_logging(tmp_path, retention_days=7)
    logger = logging.getLogger("test.redact")
    logger.info("调用失败：%s / %s", FAKE_KEY, FAKE_TOKEN)
    logger.info("裸值 %s", FAKE_KEY)
    logging_setup.reset_file_logging()

    text = _read(tmp_path / f"app-{date.today():%Y%m%d}.log")
    assert FAKE_KEY not in text
    assert FAKE_TOKEN not in text
    assert logging_setup.REDACTION_PLACEHOLDER in text


def test_secret_values_in_exception_text_are_redacted(tmp_path, monkeypatch):
    """异常栈里带出的密钥同样要脱敏（traceback 也走同一个 Filter）。"""
    monkeypatch.setenv("MINIMAX_API_KEY", FAKE_KEY)

    logging_setup.setup_file_logging(tmp_path, retention_days=7)
    logger = logging.getLogger("test.redact.exc")
    try:
        raise RuntimeError(f"上游 401：key={FAKE_KEY}")
    except RuntimeError:
        logger.exception("请求失败")
    logging_setup.reset_file_logging()

    text = _read(tmp_path / f"app-{date.today():%Y%m%d}.log")
    assert FAKE_KEY not in text
    assert logging_setup.REDACTION_PLACEHOLDER in text


def test_empty_log_dir_env_means_no_file_logging(monkeypatch):
    """``H3_LOG_DIR`` 显式为空 = **不落盘**（测试与干净检出的开关，spec #239）。"""
    monkeypatch.setenv(logging_setup.LOG_DIR_ENV, "")
    assert logging_setup.resolve_log_dir() is None
    assert logging_setup.setup_file_logging() is None
    assert not any(
        getattr(handler, "_h3_file_log", False)
        for handler in logging.getLogger().handlers
    )


def test_env_absent_falls_back_to_paths_logs_dir(monkeypatch):
    """没设开关时落 ``paths.logs_dir()``（开发态即仓库 ``logs/``，spec §6）。"""
    monkeypatch.delenv(logging_setup.LOG_DIR_ENV, raising=False)
    assert logging_setup.resolve_log_dir() == paths.logs_dir()


def test_test_suite_is_wired_to_not_write_repo_logs():
    """全量用例默认不落盘（conftest 把 ``H3_LOG_DIR`` 设为空串）。

    这条守的是「跑完全量后仓库 ``logs/`` 没有新文件」：谁把 conftest 的开关删了，
    或者实现不再认「空串 = 关闭」，这里当场红。
    """
    assert os.environ.get(logging_setup.LOG_DIR_ENV) == ""
    assert not any(
        getattr(handler, "_h3_file_log", False)
        for handler in logging.getLogger().handlers
    )
