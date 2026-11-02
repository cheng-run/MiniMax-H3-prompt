"""真实素材路径的解析契约：H3_COMFY_OUTPUT 优先，未设时退回本机默认值。

这条契约决定"换机器怎么跑真实素材回归"，所以钉成测试。它**不依赖真实素材**，
所以缺素材的机器上照跑——真实素材模块本身会 SKIP，本文件不会。
"""
from __future__ import annotations

from pathlib import Path

from tests.conftest import DEFAULT_COMFY_OUTPUT, comfy_output_dir


def test_comfy_output_dir_prefers_env_var(monkeypatch):
    monkeypatch.setenv("H3_COMFY_OUTPUT", "X:/别处/2026-09-18")
    assert comfy_output_dir() == Path("X:/别处/2026-09-18")


def test_comfy_output_dir_falls_back_to_local_default(monkeypatch):
    monkeypatch.delenv("H3_COMFY_OUTPUT", raising=False)
    assert comfy_output_dir() == DEFAULT_COMFY_OUTPUT


def test_comfy_output_dir_treats_empty_env_var_as_unset(monkeypatch):
    """空字符串当没设——`VAR=` 这种写法不该被解析成当前目录。"""
    monkeypatch.setenv("H3_COMFY_OUTPUT", "")
    assert comfy_output_dir() == DEFAULT_COMFY_OUTPUT
