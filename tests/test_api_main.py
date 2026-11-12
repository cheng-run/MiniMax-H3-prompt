"""服务入口把标准流切到 UTF-8（#221 真实跑实测）。

为什么需要守卫：Windows 控制台默认 GBK，阶段 1 的进度横幅含 ``▶``，一次 ``print``
编不出来就会把后台 job 炸成 ``UnicodeEncodeError``——打印失败不该等于任务失败。
"""
from __future__ import annotations

import sys
from pathlib import Path

from minimax_h3_prompt.api import __main__ as entry


class _Stream:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def reconfigure(self, **kwargs: str) -> None:
        self.calls.append(kwargs)


def test_force_utf8_stdio_reconfigures_both_streams(monkeypatch):
    out, err = _Stream(), _Stream()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)

    entry._force_utf8_stdio()

    assert out.calls == [{"encoding": "utf-8", "errors": "replace"}]
    assert err.calls == [{"encoding": "utf-8", "errors": "replace"}]


def test_force_utf8_stdio_tolerates_streams_without_reconfigure(monkeypatch):
    """测试捕获对象不一定有 ``reconfigure``；入口不能因此崩。"""
    monkeypatch.setattr(sys, "stdout", object())
    monkeypatch.setattr(sys, "stderr", object())

    entry._force_utf8_stdio()


def test_log_retention_days_reads_the_logging_section():
    """启动入口的日志保留天数取 ``logging.retention_days``（#239 / spec §8.1）。"""
    assert entry._log_retention_days() == 7


def test_startup_line_carries_the_version_alongside_the_legacy_fields():
    """启动行补上版本，但地址/数据根/形态三个既有字段一个不改（解析器仍认得）。"""
    info = entry.StartupInfo(
        host="127.0.0.1",
        port=9123,
        base_url="http://127.0.0.1:9123/",
        data_root=Path("D:/data"),
        frozen=False,
        version="9.9.9",
    )

    line = info.startup_line()

    assert line.startswith("[启动] 地址=http://127.0.0.1:9123/；")
    assert "数据根=" in line
    assert "形态=开发" in line
    assert "版本=9.9.9" in line


def test_startup_line_without_version_stays_backwards_compatible():
    """没传版本的构造（既有调用/测试）输出与旧格式逐字一致。"""
    info = entry.StartupInfo(
        host="127.0.0.1", port=8000, base_url="http://127.0.0.1:8000/",
        data_root=Path("D:/data"), frozen=True,
    )
    assert info.startup_line() == "[启动] 地址=http://127.0.0.1:8000/；数据根=D:\\data；形态=冻结"
