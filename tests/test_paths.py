"""唯一路径解析模块（#237）：开发态 / 冻结态 / ``--data-root`` 三形态的取值。

这两类根是 spec #235 §6 的裁定：只读资源随 EXE 进 ``_MEIPASS``，可写数据落 EXE 同级
``data/``；开发态两者都退回仓库根，既有行为一格不变。用例只走模块的公开口，
不读内部实现细节。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from minimax_h3_prompt import paths

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def data_root_reset():
    """数据根是模块级覆盖（全局状态），每条用例前后都归零，避免串味。"""
    paths.set_data_root(None)
    yield
    paths.set_data_root(None)


def test_dev_form_roots_are_repo_root() -> None:
    """开发态：只读资源根与可写数据根都是仓库根，``data_root`` 不被改写。"""
    assert paths.is_frozen() is False
    assert paths.resource_root() == REPO_ROOT
    assert paths.data_root() == REPO_ROOT


def test_dev_form_sessions_root_keeps_output_sessions(monkeypatch, tmp_path: Path) -> None:
    """开发态默认会话根仍是 ``output/sessions``（历史布局，不能跟着冻结态改）。"""
    assert paths.sessions_root() == REPO_ROOT / "output" / "sessions"
    assert paths.sessions_root("output/sessions") == REPO_ROOT / "output" / "sessions"
    # 显式覆盖仍按数据根的相对路径走（现状语义）。
    assert paths.sessions_root("custom/sessions") == REPO_ROOT / "custom" / "sessions"


def test_dev_form_full_path_table() -> None:
    """开发态全部路径都落在仓库根下的历史位置（既有代码的行为一格不变）。"""
    assert paths.config_dir() == REPO_ROOT / "config"
    assert paths.config_path() == REPO_ROOT / "config" / "agent.yaml"
    assert paths.image_config_path() == REPO_ROOT / "config" / "image.yaml"
    assert paths.workflow_config_path() == REPO_ROOT / "config" / "workflow.yaml"
    assert paths.library_config_path() == REPO_ROOT / "config" / "library.yaml"
    assert paths.bundled_config_path("agent.yaml") == REPO_ROOT / "config" / "agent.yaml"
    assert paths.output_dir() == REPO_ROOT / "output"
    assert paths.logs_dir() == REPO_ROOT / "logs"
    assert paths.data_path("output/stages") == REPO_ROOT / "output" / "stages"
    # 前端构建产物：开发态是仓库内的 ``frontend/dist``。
    assert paths.frontend_dist_dir() == REPO_ROOT / "frontend" / "dist"
    assert paths.resource_path("config", "agent.yaml") == REPO_ROOT / "config" / "agent.yaml"


@pytest.fixture()
def data_root_override(tmp_path: Path):
    """改数据根并保证用例结束后恢复（模块级覆盖是全局状态）。"""
    root = tmp_path / "data-root"
    paths.set_data_root(root)
    try:
        yield root
    finally:
        paths.set_data_root(None)


def test_data_root_override_moves_writable_paths_only(data_root_override: Path) -> None:
    """``--data-root`` 只挪可写数据；只读资源根不动（它跟 exe/仓库走）。"""
    assert paths.data_root() == data_root_override
    assert paths.resource_root() == REPO_ROOT
    assert paths.config_path() == data_root_override / "config" / "agent.yaml"
    assert paths.sessions_root() == data_root_override / "output" / "sessions"
    assert paths.output_dir() == data_root_override / "output"
    assert paths.logs_dir() == data_root_override / "logs"
    # 只读资源（内置模板、前端 dist）不受数据根影响。
    assert paths.bundled_config_path("agent.yaml") == REPO_ROOT / "config" / "agent.yaml"
    assert paths.frontend_dist_dir() == REPO_ROOT / "frontend" / "dist"


@pytest.fixture()
def frozen(monkeypatch, tmp_path: Path):
    """冻结态模拟：PyInstaller 的三个标记（``sys.frozen`` / ``_MEIPASS`` / 可执行文件）。"""
    app_dir = tmp_path / "MiniMax-H3-prompt"
    meipass = app_dir / "_internal"
    meipass.mkdir(parents=True)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(meipass), raising=False)
    monkeypatch.setattr(sys, "executable", str(app_dir / "MiniMax-H3-prompt.exe"))
    return app_dir, meipass


def test_frozen_form_full_path_table(frozen, data_root_reset) -> None:
    """冻结态：只读资源在 ``_MEIPASS``，可写数据在 EXE 同级 ``data/``（spec §4）。"""
    app_dir, meipass = frozen

    assert paths.is_frozen() is True
    assert paths.resource_root() == meipass
    # 只读资源
    assert paths.frontend_dist_dir() == meipass / "frontend" / "dist"
    assert paths.bundled_config_path("agent.yaml") == meipass / "config" / "agent.yaml"
    assert paths.resource_path("prompts", "x.md") == meipass / "prompts" / "x.md"
    # 可写数据（EXE 同级 data/）
    assert paths.data_root() == app_dir / "data"
    assert paths.config_path() == app_dir / "data" / "config" / "agent.yaml"
    assert paths.sessions_root() == app_dir / "data" / "sessions"
    assert paths.output_dir() == app_dir / "data" / "output"
    assert paths.logs_dir() == app_dir / "data" / "logs"
    assert paths.data_path("output/stages") == app_dir / "data" / "output" / "stages"


def test_frozen_sessions_root_ignores_legacy_yaml_value(frozen, data_root_reset) -> None:
    """冻结态会话根固定 ``data/sessions``：``agent.yaml`` 的 ``output/sessions`` 是开发态布局。"""
    app_dir, _ = frozen
    assert paths.sessions_root("output/sessions") == app_dir / "data" / "sessions"
    assert paths.sessions_root("custom") == app_dir / "data" / "sessions"


def test_frozen_form_honours_data_root_override(frozen, data_root_override: Path) -> None:
    """冻结态 + ``--data-root``：会话与配置跟着数据根走，只读资源仍在 ``_MEIPASS``。"""
    _, meipass = frozen
    assert paths.data_root() == data_root_override
    assert paths.sessions_root() == data_root_override / "sessions"
    assert paths.config_path() == data_root_override / "config" / "agent.yaml"
    assert paths.frontend_dist_dir() == meipass / "frontend" / "dist"


def test_config_modules_take_their_paths_from_paths_module() -> None:
    """散装的 ``parents[N]`` 收口：各配置模块与 spa 的常量只能等于路径模块的产出。

    为什么在**子进程**里看：``tests/conftest.py`` 的 ``isolated_asset_library`` 是 autouse，
    会把 ``library_config.LIBRARY_CONFIG_PATH`` 换到临时文件（防止测试写进真实素材库），
    进程内的常量因此不是模块真实的派生值。子进程里没有那层替换。
    """
    code = ";".join([
        "from minimax_h3_prompt import config, image_config, library_config, workflow_config, paths",
        "from minimax_h3_prompt.api import spa",
        "assert config.CONFIG_PATH == paths.config_path(), config.CONFIG_PATH",
        "assert image_config.IMAGE_CONFIG_PATH == paths.image_config_path()",
        "assert workflow_config.WORKFLOW_CONFIG_PATH == paths.workflow_config_path()",
        "assert library_config.LIBRARY_CONFIG_PATH == paths.library_config_path()",
        "assert spa.DEFAULT_DIST == paths.frontend_dist_dir()",
        "print('OK')",
    ])
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT / "src") + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO_ROOT, env=env,
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_config_derives_writable_paths_from_data_root(data_root_override: Path) -> None:
    """``Config`` 的会话/产物落盘跟着数据根走（数据根一改，不需要任何代码改动）。"""
    import yaml

    from minimax_h3_prompt.config import Config

    raw = yaml.safe_load(paths.bundled_config_path("agent.yaml").read_text(encoding="utf-8"))
    pipeline = raw.get("pipeline", {}) or {}
    defaults = raw.get("defaults", {}) or {}

    cfg = Config()

    assert cfg.sessions_root == paths.sessions_root(defaults.get("sessions_root"))
    assert cfg.output_path == paths.data_path(pipeline.get("output_path", "output/final_prompt.txt"))
    assert cfg.stages_path == paths.data_path(pipeline.get("stages_path", "output/stages"))
    assert cfg.sessions_root.parent.parent == data_root_override
