"""启动入口（#237）：端口选择、单实例、数据根覆盖、实际地址的单一来源。

这一组用例只走入口的公开行为：``start()`` 的启动信息、``main()`` 的退出码与提示，
以及真实子进程里「argv 先于 import 生效」「随机端口真的能连上」。断言不摸内部变量。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from minimax_h3_prompt import paths
from minimax_h3_prompt.api import __main__ as entry

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_NAMES = ("agent.yaml", "image.yaml", "workflow.yaml", "library.yaml")


@pytest.fixture(autouse=True)
def _reset_data_root():
    paths.set_data_root(None)
    yield
    paths.set_data_root(None)


def make_data_root(root: Path, *, output_path: str | None = None) -> Path:
    """造一个可用的数据根：把仓库里的配置复制进去（模拟首次启动后的形态）。

    ``output_path`` 给出时改写 ``agent.yaml`` 的 ``pipeline.output_path``——子进程用这个
    可区分值证明「``--data-root`` 在 import config 之前就已生效」。
    """
    config_dir = root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    for name in CONFIG_NAMES:
        source = REPO_ROOT / "config" / name
        if source.is_file():
            shutil.copyfile(source, config_dir / name)
    if output_path is not None:
        yaml_path = config_dir / "agent.yaml"
        text = yaml_path.read_text(encoding="utf-8")
        lines = []
        for line in text.splitlines(keepends=True):
            if line.strip().startswith("output_path:"):
                indent = line[: len(line) - len(line.lstrip())]
                lines.append(f"{indent}output_path: {output_path}\n")
            else:
                lines.append(line)
        yaml_path.write_text("".join(lines), encoding="utf-8")
    return root


def child_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT / "src") + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["PYTHONIOENCODING"] = "utf-8"
    return env


class ChildServer:
    """子进程起的真实服务：读启动日志拿到实际地址，收尾杀掉进程。"""

    def __init__(self, args: list[str]) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "minimax_h3_prompt.api", *args],
            cwd=REPO_ROOT, env=child_env(),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace",
        )
        self.lines: list[str] = []
        self._reader = threading.Thread(target=self._drain, daemon=True)
        self._reader.start()

    def _drain(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self.lines.append(line.rstrip("\n"))

    def wait_for(self, marker: str, timeout: float = 60.0) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for line in list(self.lines):
                if marker in line:
                    return line
            if self.proc.poll() is not None and not self.lines:
                break
            time.sleep(0.05)
        raise AssertionError(f"等不到启动行 {marker!r}；已收到：\n" + "\n".join(self.lines))

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover - 极端兜底
            self.proc.kill()
            self.proc.wait(timeout=15)


def parse_startup_line(line: str) -> dict[str, str]:
    """``[启动] 地址=… 数据根=… 形态=…`` 这类一元行解析成字典（字段名即契约）。"""
    payload = line.split("[启动]", 1)[1].strip()
    result: dict[str, str] = {}
    for chunk in payload.split("；"):
        chunk = chunk.strip()
        if "=" in chunk:
            key, value = chunk.split("=", 1)
            result[key.strip()] = value.strip()
    return result


# --------------------------------------------------------------------------- 端口

def test_dev_form_default_port_is_8000() -> None:
    """开发形态必须仍绑 8000：Vite 代理写死 ``127.0.0.1:8000``。"""
    assert entry.default_port() == 8000
    assert entry.resolve_port(None) == 8000


def test_frozen_form_default_port_is_auto(monkeypatch) -> None:
    """冻结形态默认随机空闲端口（spec §5.2）；``--port 0`` 也是自动。"""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(REPO_ROOT), raising=False)
    monkeypatch.setattr(sys, "executable", str(REPO_ROOT / "MiniMax-H3-prompt.exe"))
    assert entry.default_port() == 0
    assert entry.resolve_port(None) == 0
    assert entry.resolve_port(0) == 0
    assert entry.resolve_port(9123) == 9123


def test_explicit_port_is_used_as_is() -> None:
    assert entry.resolve_port(9123) == 9123


def test_occupied_explicit_port_fails_and_names_the_port() -> None:
    """显式端口被占用：报错退出、点名该端口，不静默换端口（spec §5.2）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
        blocker.bind((entry.HOST, 0))
        blocker.listen(1)
        port = blocker.getsockname()[1]
        with pytest.raises(entry.PortInUseError) as excinfo:
            entry.ensure_port_available(entry.HOST, port)
        assert str(port) in str(excinfo.value)

        exit_code = entry.main(["--port", str(port)])
    assert exit_code != 0


def test_auto_port_is_not_probed_for_availability() -> None:
    """``--port 0`` / 默认随机端口不做事先探测：端口由内核分配。"""
    entry.ensure_port_available(entry.HOST, 0)  # 不抛即通过


# ------------------------------------------------------------------- 真实子进程

def test_child_server_serves_on_a_random_port_and_announces_the_address(tmp_path: Path) -> None:
    """随机端口 + 实际地址进启动日志；该地址上的健康接口真的可用（AC2/AC5 的前提）。"""
    data_root = make_data_root(tmp_path / "data")
    child = ChildServer(["--data-root", str(data_root), "--port", "0"])
    try:
        info = parse_startup_line(child.wait_for("[启动]"))
        base_url = info["地址"]
        assert base_url.startswith("http://127.0.0.1:")
        assert int(base_url.rsplit(":", 1)[1].rstrip("/")) > 1024, "不许绑特权端口"
        assert info["数据根"] == str(data_root)
        assert info["形态"] == "开发"

        with urllib.request.urlopen(f"{base_url}api/v1/health", timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        assert body["service"] == "minimax-h3-prompt"
    finally:
        child.stop()


def test_data_root_override_applies_before_config_import(tmp_path: Path) -> None:
    """``--data-root`` 必须在 import config 之前生效（模块级单例只算一次）。"""
    data_root = make_data_root(tmp_path / "data", output_path="output/FROM-DATA-ROOT.txt")
    script = (
        "from minimax_h3_prompt.api import __main__ as entry\n"
        f"handle = entry.start(['--data-root', r'{data_root}', '--port', '0'])\n"
        "from minimax_h3_prompt.config import config\n"
        "print('CHECK', config.output_path.as_posix(), config.sessions_root.as_posix())\n"
        "handle.stop()\n"
        "handle.close()\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=REPO_ROOT, env=child_env(),
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    assert result.returncode == 0, result.stderr
    check = next(line for line in result.stdout.splitlines() if line.startswith("CHECK"))
    parts = check.split()
    assert parts[1] == f"{data_root.as_posix()}/output/FROM-DATA-ROOT.txt"
    # 开发形态的会话根仍在数据根下（这里是临时目录，不是真实 output/sessions）。
    assert parts[2] == f"{data_root.as_posix()}/output/sessions"


def test_second_instance_on_same_data_root_is_refused(tmp_path: Path) -> None:
    """同一 ``data/`` 起第二个实例：提示后退出，只剩一个服务在跑（AC3）。"""
    data_root = make_data_root(tmp_path / "data")
    first = ChildServer(["--data-root", str(data_root), "--port", "0"])
    try:
        base_url = parse_startup_line(first.wait_for("[启动]"))["地址"]
        second = subprocess.run(
            [sys.executable, "-m", "minimax_h3_prompt.api",
             "--data-root", str(data_root), "--port", "0"],
            cwd=REPO_ROOT, env=child_env(),
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        )
        output = (second.stdout or "") + (second.stderr or "")
        assert second.returncode != 0
        assert str(data_root) in output
        assert "已经有一个服务在运行" in output
        # 第一个实例仍在服务（第二个没把它顶掉）。
        with urllib.request.urlopen(f"{base_url}api/v1/health", timeout=10) as resp:
            assert resp.status == 200
    finally:
        first.stop()
