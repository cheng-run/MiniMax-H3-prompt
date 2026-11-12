"""Windows 环境探测的唯一实现（#243 两轴复审）：WebView2 运行时版本。

为什么单独一个模块：``desktop.py``（启动前拦截缺失/过旧）与 ``diagnostics.py``（诊断包
的 ``environment.txt`` 环境行）都要这个版本串。原先两处各写一份注册表探测——GUID、
四个探测点、失败口径全是两份「同一事实」，改一处另一处会悄悄过时。``desktop`` 只在
Windows 需要，但 ``diagnostics`` 要能在任何平台上安全导入，因此这里对非 Windows 只返回
``None``，从不 import ``winreg``。
"""
from __future__ import annotations

import sys
from typing import Callable

# WebView2 Runtime 在 EdgeUpdate 的固定客户端 GUID（微软官方 Evergreen 检测口径）。
WEBVIEW2_CLIENT_GUID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
# 诊断包里读不到时的占位（那行是纯文本，没有「没有」这个语义，只能写清楚）。
WEBVIEW2_UNREADABLE = "不可读"

# 注册表探测点（#240 票面）：WOW6432Node 那条是微软文档的 64 位系统标准落点；
# 另外补「64 位运行时」与「每用户安装（HKCU）」两处，32/64 位视图都试。
WEBVIEW2_REGISTRY_TARGETS: tuple[tuple[str, str, str], ...] = (
    ("HKLM", "SOFTWARE\\WOW6432Node\\Microsoft\\EdgeUpdate\\Clients\\"
             f"{WEBVIEW2_CLIENT_GUID}", "64"),
    ("HKLM", "SOFTWARE\\Microsoft\\EdgeUpdate\\Clients\\"
             f"{WEBVIEW2_CLIENT_GUID}", "32"),
    ("HKLM", "SOFTWARE\\Microsoft\\EdgeUpdate\\Clients\\"
             f"{WEBVIEW2_CLIENT_GUID}", "64"),
    ("HKCU", "Software\\Microsoft\\EdgeUpdate\\Clients\\"
             f"{WEBVIEW2_CLIENT_GUID}", "64"),
)


def _read_registry_value(hive: str, path: str, view: str) -> str | None:
    """读 ``pv``；不存在/不可读返回 None（探测不该因为「没装」抛异常）。"""
    import winreg

    root = winreg.HKEY_LOCAL_MACHINE if hive == "HKLM" else winreg.HKEY_CURRENT_USER
    view_flag = winreg.KEY_WOW64_32KEY if view == "32" else winreg.KEY_WOW64_64KEY
    try:
        with winreg.OpenKey(root, path, 0, winreg.KEY_READ | view_flag) as key:
            value, _ = winreg.QueryValueEx(key, "pv")
    except OSError:
        return None
    text = str(value).strip()
    return text or None


def read_webview2_version(
    *,
    platform: str | None = None,
    reader: Callable[[str, str, str], str | None] | None = None,
) -> str | None:
    """WebView2 Runtime 的版本串；非 Windows 或没装返回 None。

    ``platform`` / ``reader`` 可注入：非 Windows 上不 import ``winreg``（那条 import 会
    直接失败），用例也不必真去读注册表。
    """
    if (platform or sys.platform) != "win32":
        return None
    reader = reader or _read_registry_value
    for hive, path, view in WEBVIEW2_REGISTRY_TARGETS:
        version = reader(hive, path, view)
        if version:
            return version
    return None


def webview2_version() -> str:
    """诊断包环境行的口径：读不到（非 Windows / 未安装 / 注册表被锁）写「不可读」。"""
    return read_webview2_version() or WEBVIEW2_UNREADABLE
