"""单实例保护（#237 / spec #235 §5.3）：同一数据根只许一个服务进程。

判据是 Windows 命名互斥体，名字由**数据根绝对路径的哈希**派生——所以名字本身不能
泄漏路径（互斥体名字对同机其它进程可见），同一根的不同写法要落到同一个名字上。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from minimax_h3_prompt.api import single_instance


def test_mutex_name_changes_with_data_root(tmp_path: Path) -> None:
    first = single_instance.mutex_name(tmp_path / "one")
    second = single_instance.mutex_name(tmp_path / "two")
    assert first != second
    assert first.startswith(single_instance.MUTEX_PREFIX)


def test_mutex_name_does_not_leak_the_path(tmp_path: Path) -> None:
    """名字对同机其它进程可见：不许带路径（盘符、目录名、用户名都不行）。"""
    root = tmp_path / "ticket-237" / "data"
    name = single_instance.mutex_name(root)
    folded = name.casefold()
    assert "ticket-237" not in folded
    assert tmp_path.name.casefold() not in folded
    assert ":" not in name and "\\" not in name.removeprefix(single_instance.MUTEX_PREFIX)
    assert "/" not in name


def test_mutex_name_is_stable_across_path_spellings(tmp_path: Path) -> None:
    """同一个目录的两种写法（大小写 / 正反斜杠）必须落到同一个互斥体上。"""
    root = tmp_path / "data"
    other = Path(str(root).upper())
    assert single_instance.mutex_name(root) == single_instance.mutex_name(other)


def test_non_windows_platform_degrades_to_no_lock(monkeypatch) -> None:
    """非 Windows 上模块必须能安全使用：不碰 ctypes，直接放行（打包目标是 Win11）。"""
    monkeypatch.setattr(single_instance, "_windows_available", lambda: False)
    lock = single_instance.acquire(Path("/tmp/data-root-not-used"))
    try:
        assert lock.acquired is True
        assert lock.existing is False
    finally:
        lock.release()


@pytest.mark.skipif(sys.platform != "win32", reason="命名互斥体只在 Windows 上可用")
def test_second_instance_on_same_data_root_is_rejected(tmp_path: Path) -> None:
    first = single_instance.acquire(tmp_path / "data")
    try:
        assert first.acquired is True
        second = single_instance.acquire(tmp_path / "data")
        try:
            assert second.acquired is False
            assert second.existing is True
            # 提示里要给出数据根，用户才知道该关哪个进程。
            assert str((tmp_path / "data")) in second.message
        finally:
            second.release()
    finally:
        first.release()


@pytest.mark.skipif(sys.platform != "win32", reason="命名互斥体只在 Windows 上可用")
def test_different_data_roots_do_not_block_each_other(tmp_path: Path) -> None:
    first = single_instance.acquire(tmp_path / "a")
    second = single_instance.acquire(tmp_path / "b")
    try:
        assert first.acquired is True
        assert second.acquired is True
    finally:
        second.release()
        first.release()


@pytest.mark.skipif(sys.platform != "win32", reason="命名互斥体只在 Windows 上可用")
def test_lock_is_reusable_after_release(tmp_path: Path) -> None:
    """关掉进程（释放句柄）之后同一个根还能再起——否则重启即被自己挡住。"""
    lock = single_instance.acquire(tmp_path / "data")
    assert lock.acquired is True
    lock.release()
    again = single_instance.acquire(tmp_path / "data")
    try:
        assert again.acquired is True
    finally:
        again.release()
