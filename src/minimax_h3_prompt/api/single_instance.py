"""同一数据根只许一个服务进程（#237 / spec #235 §5.3）。

为什么需要：两个服务指向**同一份会话存储**就是两个写入者，违反「每会话单写入者」
（D2 不变式，``docs/agents/workbench.md``）。随机端口让「双击两次」不再像冲突——第二个
实例会自己挑一个空闲端口照常起来，然后和第一个抢同一批会话文件。

做法：Windows 命名互斥体，名字由**数据根绝对路径的哈希**派生。用哈希而不是把路径拼进
名字，是因为互斥体名字对同机其它进程可见——路径里带着用户名与目录结构，没必要广播。

非 Windows 上 ``acquire`` 一律放行：打包目标只有 Windows 11（spec §11），而模块要能
被测试与静态分析安全导入（``ctypes.windll`` 在非 Windows 上根本不存在）。
"""
from __future__ import annotations

import hashlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path

# ``Local\`` 前缀 = 会话（登录会话）内可见，不跨远程会话抢占；名字语义见模块 docstring。
MUTEX_PREFIX = "Local\\MiniMaxH3Prompt-"

# Win32 ``CreateMutexW`` 在对象已存在时的 GetLastError 码。
ERROR_ALREADY_EXISTS = 183


def mutex_name(data_root: str | Path) -> str:
    """由数据根派生互斥体名字（同一目录的不同写法得到同一个名字）。

    ``normcase`` 抹平 Windows 的大小写与斜杠差异；再 ``casefold`` 兜住非 Windows 上
    ``normcase`` 不改大小写的情况——否则同一个目录换个大小写就能起第二个实例。
    """
    key = os.path.normcase(str(Path(data_root).resolve())).casefold()
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
    return f"{MUTEX_PREFIX}{digest}"


def _windows_available() -> bool:
    """是否走 Windows 分支。单独一个函数，非 Windows 的降级路径因此可测。"""
    return sys.platform == "win32"


@dataclass
class InstanceLock:
    """一次单实例判定结果。

    ``acquired``：本次是否拿到锁（``False`` 表示已有实例在跑，调用方应提示后退出）。
    ``existing``：锁存在是因为**别人**已经持有（非 Windows 降级路径恒为 ``False``）。
    ``message``：给用户看的提示（含数据根路径，用户要知道该关哪个进程）。
    """

    acquired: bool
    existing: bool
    message: str = ""
    _handle: int | None = None

    def release(self) -> None:
        """释放句柄。进程退出时内核也会回收，但测试与显式收尾需要它。"""
        handle, self._handle = self._handle, None
        if handle is None:
            return
        import ctypes

        ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(handle)


def acquire(data_root: str | Path) -> InstanceLock:
    """尝试对 ``data_root`` 取得单实例锁。

    拿不到不算错误：返回 ``acquired=False`` 并给出提示，由调用方决定退出码——启动入口
    按 spec §5.3「提示后退出」处理。
    """
    root = Path(data_root).resolve()
    if not _windows_available():
        return InstanceLock(acquired=True, existing=False)

    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    name = mutex_name(root)
    handle = kernel32.CreateMutexW(None, False, name)
    last_error = ctypes.get_last_error()
    if not handle:
        raise OSError(last_error, "CreateMutexW 失败：无法建立单实例锁")
    if last_error == ERROR_ALREADY_EXISTS:
        # 已有实例持有：这个多出来的句柄立刻关掉，不影响第一个进程的锁。
        kernel32.CloseHandle(handle)
        return InstanceLock(
            acquired=False,
            existing=True,
            message=f"同一个数据目录已经有一个服务在运行：{root}\n"
                    "请先关掉正在运行的那个窗口／进程，再重新启动。",
        )
    return InstanceLock(acquired=True, existing=False, _handle=handle)
