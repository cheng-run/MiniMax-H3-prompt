"""D5-3（#238）：密钥落位——只认 ``paths.env_file_path()``，不读 ``data/`` 下的 ``.env``。

票面两条硬边界（#238 验收标准）都在这份文件里：

- **缺密钥时系统不崩**：错误文案点名密钥文件绝对路径，健康接口照常 200；
- **``data/`` 下的 ``.env`` 不被读取**：冻结模拟 + 临时数据根里放一份诱饵，
  启动后它必须**没有**进 ``os.environ``（否则作品目录被拷走就带走密钥）。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from minimax_h3_prompt import paths
from minimax_h3_prompt.config import Config

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _reset_data_root():
    paths.set_data_root(None)
    yield
    paths.set_data_root(None)


@pytest.fixture
def config_without_keys(monkeypatch):
    """摘掉当前 YAML 实际用到的密钥变量与 ``load_dotenv``，造出「一台没配密钥的机器」。"""

    from minimax_h3_prompt import config as config_module

    monkeypatch.setattr(config_module, "load_dotenv", lambda *_a, **_k: None)
    probe = Config()
    names = {probe.primary_model.api_key_env, probe.vision_model.api_key_env}
    # anthropic 的 `_lookup_api_key` 还认这个回退名，一并摘掉才真的是「缺」。
    names.update({"ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"})
    for name in names:
        monkeypatch.delenv(name, raising=False)
    return probe


def test_missing_key_error_names_the_env_file_absolute_path(config_without_keys) -> None:
    """报错文案必须给出密钥文件的绝对路径（冻结后「项目根 .env」指错地方）。"""
    with pytest.raises(ValueError) as excinfo:
        config_without_keys.resolve_api_key(config_without_keys.primary_model)
    message = str(excinfo.value)
    assert config_without_keys.primary_model.api_key_env in message
    assert str(paths.env_file_path()) in message


def test_missing_api_keys_lists_names_only(config_without_keys) -> None:
    """``missing_api_keys`` 只给变量名，绝不带值；两张模型档都算，重名只列一次。"""
    missing = config_without_keys.missing_api_keys()
    assert config_without_keys.primary_model.api_key_env in missing
    assert config_without_keys.vision_model.api_key_env in missing
    assert len(missing) == len(set(missing))


def test_present_keys_are_not_reported_missing(config_without_keys, monkeypatch) -> None:
    probe = config_without_keys
    monkeypatch.setenv(probe.primary_model.api_key_env, "test-secret")
    monkeypatch.setenv(probe.vision_model.api_key_env, "test-secret")
    assert probe.missing_api_keys() == []


def test_frozen_start_reads_appdata_env_and_ignores_data_env(tmp_path: Path) -> None:
    """冻结模拟：只加载 ``%APPDATA%/MiniMax-H3-prompt/.env``。

    诱惑物放两份（``data/.env`` 与 ``data/config/.env``）：两者都不该被读。
    用例在**子进程**里跑——``load_dotenv`` 在 import 期只发生一次，进程内没法重来。
    """
    from test_api_entry import child_env, make_data_root

    data_root = make_data_root(tmp_path / "data")
    (data_root / ".env").write_text("H3_SENTINEL_DATA_ENV=leaked\n", encoding="utf-8")
    (data_root / "config" / ".env").write_text(
        "H3_SENTINEL_CONFIG_ENV=leaked\n", encoding="utf-8")
    appdata = tmp_path / "Roaming"
    (appdata / "MiniMax-H3-prompt").mkdir(parents=True)
    (appdata / "MiniMax-H3-prompt" / ".env").write_text(
        "H3_SENTINEL_APPDATA_ENV=from-appdata\n", encoding="utf-8")

    script = "\n".join([
        "import json, os, sys",
        "sys.frozen = True",
        f"sys._MEIPASS = r'{REPO_ROOT}'",
        f"sys.executable = r'{tmp_path / 'exe' / 'MiniMax-H3-prompt.exe'}'",
        "from minimax_h3_prompt.api import __main__ as entry",
        f"handle = entry.start(['--data-root', r'{data_root}', '--port', '0'])",
        "from minimax_h3_prompt import paths as p",
        "print('CHECK', json.dumps({",
        "    'data': os.environ.get('H3_SENTINEL_DATA_ENV'),",
        "    'config': os.environ.get('H3_SENTINEL_CONFIG_ENV'),",
        "    'appdata': os.environ.get('H3_SENTINEL_APPDATA_ENV'),",
        "    'env_path': str(p.env_file_path()),",
        "}))",
        "handle.stop()",
        "handle.close()",
    ])
    env = child_env()
    env["APPDATA"] = str(appdata)
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=REPO_ROOT, env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    line = next(x for x in result.stdout.splitlines() if x.startswith("CHECK"))
    check = json.loads(line.split(" ", 1)[1])
    assert check["data"] is None, "data/.env 被加载了：密钥会跟着作品目录一起被拷走"
    assert check["config"] is None, "data/config/.env 被加载了"
    assert check["appdata"] == "from-appdata"
    assert check["env_path"] == str(appdata / "MiniMax-H3-prompt" / ".env")
