"""服务入口把标准流切到 UTF-8（#221 真实跑实测）。

为什么需要守卫：Windows 控制台默认 GBK，阶段 1 的进度横幅含 ``▶``，一次 ``print``
编不出来就会把后台 job 炸成 ``UnicodeEncodeError``——打印失败不该等于任务失败。
"""
from __future__ import annotations

import sys

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
