"""D5-3（#238）：首次启动把内置配置模板复制进 ``data/config/``（spec #235 §7）。

两条不变量：

- **只补缺**：已存在的文件一个字节都不动（用户改过的配置不能被模板盖回去）；
- **开发形态是空操作**：开发态配置目录就是仓库 ``config/``，模板也在那里。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from minimax_h3_prompt import bootstrap, paths

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _reset_data_root():
    paths.set_data_root(None)
    yield
    paths.set_data_root(None)


def test_first_start_copies_every_template_into_config_dir(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    paths.set_data_root(data_root)

    created = bootstrap.ensure_config_files()

    assert {path.name for path in created} == set(bootstrap.CONFIG_TEMPLATE_NAMES)
    for name in bootstrap.CONFIG_TEMPLATE_NAMES:
        target = data_root / "config" / name
        assert target.is_file(), f"{name} 没有被复制出来"
        assert target.read_bytes() == paths.bundled_config_path(name).read_bytes()


def test_second_start_does_not_overwrite_user_edits(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    paths.set_data_root(data_root)
    bootstrap.ensure_config_files()
    target = data_root / "config" / "agent.yaml"
    edited = target.read_text(encoding="utf-8") + "# 用户改过这一行\n"
    target.write_text(edited, encoding="utf-8")

    assert bootstrap.ensure_config_files() == []
    assert target.read_text(encoding="utf-8") == edited


def test_dev_form_is_a_no_op_and_leaves_repo_config_alone() -> None:
    """开发态：配置目录与模板目录是同一个仓库 ``config/``，什么都不该发生。"""
    before = {name: (REPO_ROOT / "config" / name).stat().st_mtime_ns
              for name in bootstrap.CONFIG_TEMPLATE_NAMES}
    assert bootstrap.ensure_config_files() == []
    after = {name: (REPO_ROOT / "config" / name).stat().st_mtime_ns
             for name in bootstrap.CONFIG_TEMPLATE_NAMES}
    assert before == after


def test_missing_template_raises_actionable_error(tmp_path: Path, monkeypatch) -> None:
    """模板缺失 = 打包漏收：报错要点名缺哪个文件、要写到哪，别裸 traceback。"""
    paths.set_data_root(tmp_path / "data")
    monkeypatch.setattr(paths, "bundled_config_dir", lambda: tmp_path / "no-templates")

    with pytest.raises(bootstrap.ConfigBootstrapError) as excinfo:
        bootstrap.ensure_config_files()

    message = str(excinfo.value)
    assert "agent.yaml" in message
    assert str(tmp_path / "data") in message


def test_frozen_start_creates_the_appdata_dir_without_an_env_file(
        monkeypatch, tmp_path: Path) -> None:
    """冻结形态要建 ``%APPDATA%\\MiniMax-H3-prompt\\``：缺密钥提示与 README 都让人往那里写。

    只建目录、不建 ``.env``（密钥由用户自己写）；开发形态一格不动（#243 复审）。
    """
    appdata = tmp_path / "Roaming"
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "meipass"), raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "exe" / "MiniMax-H3-prompt.exe"))

    created = bootstrap.ensure_env_dir()

    target = appdata / "MiniMax-H3-prompt"
    assert created == target
    assert target.is_dir()
    assert not (target / ".env").exists()


def test_dev_form_does_not_create_appdata_dir(monkeypatch, tmp_path: Path) -> None:
    appdata = tmp_path / "Roaming"
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.delattr(sys, "frozen", raising=False)

    assert bootstrap.ensure_env_dir() is None
    assert not appdata.exists()


def test_start_bootstraps_config_then_keeps_user_edits(tmp_path: Path) -> None:
    """真实启动入口：首次起服务后四个配置就位；第二次启动不覆盖改过的那份。

    子进程里模拟冻结形态（模板在 ``_MEIPASS``），数据根是临时空目录——这正是
    用户解压后第一次双击 EXE 的形态。
    """
    data_root = tmp_path / "data"
    appdata = tmp_path / "appdata"
    script = "\n".join([
        "import os",
        "import sys",
        "sys.frozen = True",
        f"sys._MEIPASS = r'{REPO_ROOT}'",
        f"sys.executable = r'{tmp_path / 'exe' / 'MiniMax-H3-prompt.exe'}'",
        # 密钥目录落在临时 APPDATA：别在跑测试的机器上真建 Roaming 目录。
        f"os.environ['APPDATA'] = r'{appdata}'",
        "from minimax_h3_prompt.api import __main__ as entry",
        f"handle = entry.start(['--data-root', r'{data_root}', '--port', '0'])",
        "print('STARTED')",
        "handle.stop()",
        "handle.close()",
    ])

    def run_start() -> subprocess.CompletedProcess[str]:
        from test_api_entry import child_env

        return subprocess.run(
            [sys.executable, "-c", script], cwd=REPO_ROOT, env=child_env(),
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
        )

    first = run_start()
    assert first.returncode == 0, first.stdout + first.stderr
    assert "STARTED" in first.stdout
    # 冻结启动要建好密钥目录（缺 key 提示让人往这里写）；但绝不代建 .env。
    env_dir = appdata / "MiniMax-H3-prompt"
    assert env_dir.is_dir(), "冻结启动没有创建 %APPDATA%\\MiniMax-H3-prompt"
    assert not (env_dir / ".env").exists(), "不该替用户创建 .env"
    for name in bootstrap.CONFIG_TEMPLATE_NAMES:
        target = data_root / "config" / name
        assert target.is_file(), f"首次启动没有生成 {name}"
        assert target.read_bytes() == (REPO_ROOT / "config" / name).read_bytes()

    agent_yaml = data_root / "config" / "agent.yaml"
    edited = agent_yaml.read_text(encoding="utf-8") + "# 用户改过这一行\n"
    agent_yaml.write_text(edited, encoding="utf-8")

    second = run_start()
    assert second.returncode == 0, second.stdout + second.stderr
    assert agent_yaml.read_text(encoding="utf-8") == edited, "第二次启动把用户改动盖回去了"
