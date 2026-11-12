"""#243 两轴复审：WebView2 检测只有一套实现（``winenv``），两个消费者都从它取。

原先 ``desktop.read_webview2_version`` 与 ``diagnostics.webview2_version`` 各写一份注册表
探测：GUID、探测点、失败口径全是两份「同一事实」，改一处另一处会悄悄过时。
"""
from __future__ import annotations

from minimax_h3_prompt import desktop, diagnostics, winenv


def test_both_consumers_use_the_single_webview2_probe():
    assert desktop.read_webview2_version is winenv.read_webview2_version
    assert diagnostics.webview2_version is winenv.webview2_version
    assert desktop.WEBVIEW2_CLIENT_GUID == winenv.WEBVIEW2_CLIENT_GUID


def test_unreadable_version_becomes_the_placeholder(monkeypatch):
    """诊断包那一侧只写「不可读」、从不抛：那是环境行里唯一读系统的一项。"""
    monkeypatch.setattr(winenv, "read_webview2_version", lambda **_: None)

    assert winenv.webview2_version() == winenv.WEBVIEW2_UNREADABLE


def test_bundle_environment_line_uses_the_shared_probe(monkeypatch):
    monkeypatch.setattr(winenv, "read_webview2_version", lambda **_: "157.0.4341.42")

    assert "WebView2: 157.0.4341.42" in diagnostics.environment_text()
