"""#242 D5-7 验收驱动器的机械部分守卫。

不启 EXE、不建窗口、不调模型：这里钉的是**证据判据本身**——报告里 AC 一条不漏、
诊断包的排除面（密钥值/媒体/.env）真的会报红、``env -i`` 清单不会把开发环境漏给
被测进程、启动行解析只认新追加的日志。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "accept_d5.py"
SPEC = importlib.util.spec_from_file_location("accept_d5", SCRIPT)
assert SPEC and SPEC.loader
accept_d5 = importlib.util.module_from_spec(SPEC)
# dataclass 定义时要按 ``__module__`` 回查 sys.modules；不登记会拿到 None。
sys.modules[SPEC.name] = accept_d5
SPEC.loader.exec_module(accept_d5)

GOOD_ENTRIES = {
    "manifest.json": b'{"form": "frozen"}',
    "environment.txt": b"OS: Windows 11\n",
    "config.json": b'{"models": {}}',
    "logs/app-20261112.log": b"startup\n",
    "sessions/雨夜/GEN001/session-state.json": b"{}",
    "sessions/雨夜/GEN001/events.jsonl": b"{}\n",
    "artifacts/雨夜/GEN001/video-prompt.md": b"- shot\n",
}


# ------------------------------------------------------------------ .env 与密钥

def test_parse_env_file_handles_quotes_comments_and_export(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "# 注释\n"
        "export DASHSCOPE_API_KEY=\"sk-abcdef123456\"\n"
        "OPENAI_API_KEY='sk-openai-987654'  # 行尾注释\n"
        "EMPTY=\n"
        "NOT_A_PAIR\n",
        encoding="utf-8",
    )

    values = accept_d5.parse_env_file(path)

    assert values["DASHSCOPE_API_KEY"] == "sk-abcdef123456"
    assert values["OPENAI_API_KEY"] == "sk-openai-987654"
    assert values["EMPTY"] == ""
    assert "NOT_A_PAIR" not in values


def test_parse_env_file_missing_file_is_empty(tmp_path):
    assert accept_d5.parse_env_file(tmp_path / "nope.env") == {}


def test_secret_values_only_takes_key_like_names_and_long_values():
    values = accept_d5.secret_values({
        "DASHSCOPE_API_KEY": "sk-abcdef123456",
        "SOME_TOKEN": "tok-123456",
        "SHORT_TOKEN": "abc",
        "MODEL": "deepseek-v4.1",
        "api_key_env": "DASHSCOPE_API_KEY",
    })

    assert "sk-abcdef123456" in values
    assert "tok-123456" in values
    assert "abc" not in values
    assert "deepseek-v4.1" not in values
    # ``api_key_env`` 是键名不是密钥值：它的**名字**不该被当成命中串。
    assert "DASHSCOPE_API_KEY" not in values


# ------------------------------------------------------------------ 子进程环境

def test_restricted_env_matches_env_i_and_overrides_apdata():
    base = {
        "SYSTEMROOT": r"C:\Windows",
        "SystemDrive": "C:",
        "TEMP": r"C:\Temp",
        "APPDATA": r"C:\Users\me\AppData\Roaming",
        "PYTHONPATH": r"D:\repo\src",
        "VIRTUAL_ENV": r"D:\repo\.venv",
        "PATH": r"D:\repo\.venv\Scripts;C:\Windows",
        "DASHSCOPE_API_KEY": "sk-should-not-leak",
    }

    env = accept_d5.restricted_env(Path(r"C:\empty-appdata"), base)

    assert env["APPDATA"] == r"C:\empty-appdata"
    assert env["PATH"] == accept_d5.SYSTEM_PATH
    assert env["SYSTEMROOT"] == r"C:\Windows"
    assert env["SystemDrive"] == "C:"
    # 开发环境的东西一个都不许漏给被测进程。
    assert "PYTHONPATH" not in env
    assert "VIRTUAL_ENV" not in env
    assert "DASHSCOPE_API_KEY" not in env


def test_restricted_env_keeps_extra_overrides():
    env = accept_d5.restricted_env(
        Path(r"C:\appdata"), {"SYSTEMROOT": r"C:\Windows"},
        extra={"MINIMAX_FAKE_PIPELINE": "1"})
    assert env["MINIMAX_FAKE_PIPELINE"] == "1"


# ------------------------------------------------------------------ 启动行与日志窗口

def test_parse_startup_lines_reads_all_three_facts():
    text = (
        "2026-11-12 10:00:00 INFO minimax_h3_prompt.api.__main__: "
        "[启动] 地址=http://127.0.0.1:54321/；数据根=D:\\temp\\x\\data；形态=冻结；版本=0.1.0\n"
    )

    lines = accept_d5.parse_startup_lines(text)

    assert lines == [{
        "base_url": "http://127.0.0.1:54321/",
        "port": 54321,
        "data_root": "D:\\temp\\x\\data",
        "form": "冻结",
    }]


def test_parse_startup_lines_ignores_other_lines():
    assert accept_d5.parse_startup_lines("INFO 没有启动行\n") == []


def test_latest_startup_only_returns_appended_lines(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    log = log_dir / "app-20261112.log"
    log.write_text(
        "[启动] 地址=http://127.0.0.1:11111/；数据根=D:\\old\\data；形态=冻结\n",
        encoding="utf-8")
    baseline = accept_d5.snapshot_logs(log_dir)

    # 旧行还在：只按 baseline 之后的追加内容判断，不能把上一轮的端口当本轮的。
    assert accept_d5.latest_startup(log_dir, baseline) is None

    with log.open("a", encoding="utf-8") as handle:
        handle.write(
            "[启动] 地址=http://127.0.0.1:22222/；数据根=D:\\new\\data；形态=冻结\n")

    found = accept_d5.latest_startup(log_dir, baseline)

    assert found is not None
    assert found[0]["port"] == 22222
    assert found[1] == log


# ------------------------------------------------------------------ 诊断包判据

def test_bundle_problems_accepts_the_spec_layout():
    assert accept_d5.bundle_problems(GOOD_ENTRIES, session_id="雨夜/GEN001") == []


@pytest.mark.parametrize("missing", [
    "manifest.json", "environment.txt", "config.json",
    "logs/app-20261112.log",
    "sessions/雨夜/GEN001/session-state.json",
    "sessions/雨夜/GEN001/events.jsonl",
    "artifacts/雨夜/GEN001/video-prompt.md",
])
def test_bundle_problems_flags_each_missing_entry(missing):
    entries = {name: payload for name, payload in GOOD_ENTRIES.items() if name != missing}

    problems = accept_d5.bundle_problems(entries, session_id="雨夜/GEN001")

    assert problems, f"缺 {missing} 时没有报问题"


def test_bundle_problems_flags_media_and_dotenv():
    entries = dict(GOOD_ENTRIES)
    entries["assets/frame.png"] = b"\x89PNG"
    entries["sessions/雨夜/GEN001/.env"] = b"DASHSCOPE_API_KEY=sk-x"

    problems = accept_d5.bundle_problems(entries, session_id="雨夜/GEN001")

    assert any("媒体" in problem for problem in problems)
    assert any(".env" in problem for problem in problems)


def test_secret_hits_reports_only_entry_names():
    entries = {
        "config.json": b'{"api_key": "<redacted>"}',
        "logs/app.log": b"Authorization: Bearer sk-very-secret-123",
    }

    hits = accept_d5.secret_hits(entries, ("sk-very-secret-123",))

    assert hits == ["logs/app.log"]
    # 返回值里只有条目名，不把密钥值带进证据。
    assert "sk-very-secret-123" not in "".join(hits)


def test_secret_hits_is_empty_when_clean():
    assert accept_d5.secret_hits(GOOD_ENTRIES, ("sk-very-secret-123",)) == []


def test_zip_media_or_env_flags_delivery_zip_content():
    names = [
        "MiniMax-H3-prompt/MiniMax-H3-prompt.exe",
        "MiniMax-H3-prompt/_internal/frontend/dist/index.html",
    ]
    assert accept_d5.zip_media_or_env(names) == []

    assert accept_d5.zip_media_or_env(names + ["MiniMax-H3-prompt/data/a.png"])
    assert accept_d5.zip_media_or_env(names + ["MiniMax-H3-prompt/data/.env"])


# ------------------------------------------------------------------ 下载捕获的三种取法

class _FakeDownload:
    def __init__(self, url: str, payload: bytes, fail: bool = False):
        self.url = url
        self._payload = payload
        self._fail = fail
        self.saved: Path | None = None

    def save_as(self, path: str) -> None:
        if self._fail:
            raise RuntimeError("accept_downloads 未开启")
        self.saved = Path(path)
        self.saved.write_bytes(self._payload)


class _FakeResponse:
    def __init__(self, url: str, status: int, payload: bytes):
        self.url = url
        self.status = status
        self._payload = payload

    def body(self) -> bytes:
        return self._payload


class _FakePage:
    """只实现 ``capture_browser_bytes`` 用到的那几个方法。"""

    def __init__(self, fetch_payload: bytes = b"", download=None, response=None):
        self.handlers: dict[str, list] = {}
        self.fetch_payload = fetch_payload
        self.download = download
        self.response = response
        self.fetched_url = ""

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    def set_default_timeout(self, _milliseconds):
        return None

    def wait_for_timeout(self, _milliseconds):
        return None

    def evaluate(self, _script, url):
        self.fetched_url = url
        return list(self.fetch_payload)

    def emit(self, event):
        for handler in self.handlers.get(event, []):
            if event == "download":
                handler(self.download)
            else:
                handler(self.response)


URL = "http://127.0.0.1:8000/api/v1/artifacts/s/GEN001/video-prompt.md/content"


def test_capture_prefers_a_real_download_event(tmp_path):
    page = _FakePage(download=_FakeDownload(URL, b"download-body"))
    saved = tmp_path / "artifact.md"

    body, method = accept_d5.capture_browser_bytes(
        page, URL, lambda: page.emit("download"), 5_000, save_path=saved)

    assert method == "cdp-download"
    assert body == b"download-body"
    assert saved.read_bytes() == b"download-body"


def test_capture_falls_back_to_response_body_when_download_save_fails(tmp_path):
    page = _FakePage(
        download=_FakeDownload(URL, b"", fail=True),
        response=_FakeResponse(URL, 200, b"response-body"),
    )

    body, method = accept_d5.capture_browser_bytes(
        page, URL, lambda: (page.emit("download"), page.emit("response")), 5_000,
        save_path=tmp_path / "artifact.md")

    assert method == "cdp-response-body"
    assert body == b"response-body"


def test_capture_falls_back_to_page_fetch_when_click_raises():
    page = _FakePage(fetch_payload=b"fetch-body")

    def trigger():
        raise RuntimeError("Download is starting")

    body, method = accept_d5.capture_browser_bytes(page, URL, trigger, 100)

    assert method == "page-fetch"
    assert body == b"fetch-body"
    assert page.fetched_url == URL


def test_capture_rejects_non_200_response():
    page = _FakePage(response=_FakeResponse(URL, 500, b"boom"))

    with pytest.raises(accept_d5.AcceptanceError):
        accept_d5.capture_browser_bytes(
            page, URL, lambda: page.emit("response"), 5_000)


# ------------------------------------------------------------------ 配置来源核对

DASHSCOPE_CONFIG = """\
models:
  primary:
    provider: dashscope
    model: deepseek-v4.1-flash-1017
    base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
    api_key_env: DASHSCOPE_API_KEY
  vision:
    model: deepseek-v4.1-flash-1017
    base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
    api_key_env: DASHSCOPE_API_KEY
"""


def test_install_config_file_copies_bytes_before_launch(tmp_path):
    source = tmp_path / "agent.yaml"
    source.write_text(DASHSCOPE_CONFIG, encoding="utf-8")
    data_root = tmp_path / "portable-data"

    facts = accept_d5.install_config_file(source, data_root)

    target = data_root / "config" / "agent.yaml"
    assert target.read_bytes() == source.read_bytes()
    assert facts["match"] is True
    assert facts["target_bytes"] == len(source.read_bytes())
    assert facts["target_sha256"] == facts["source_sha256"]
    assert facts["contains_dashscope"] is True
    assert facts["models"] == ["deepseek-v4.1-flash-1017", "deepseek-v4.1-flash-1017"]
    assert facts["base_urls"] == [
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
    ]


def test_install_config_file_rejects_the_old_minimax_template(tmp_path):
    """旧模板起 B 段会打 api.minimax.cn——必须在这里拦住（2026-11-12 实测）。"""
    source = tmp_path / "agent.yaml"
    source.write_text(
        "models:\n  primary:\n    provider: minimax\n"
        "    base_url: https://api.minimax.cn/v1\n", encoding="utf-8")

    with pytest.raises(accept_d5.AcceptanceError) as excinfo:
        accept_d5.install_config_file(source, tmp_path / "data")

    message = str(excinfo.value)
    assert "dashscope" in message
    assert "api.minimax.cn" in message
    assert not (tmp_path / "data" / "config" / "agent.yaml").exists()


def test_install_config_file_rejects_a_missing_source(tmp_path):
    with pytest.raises(accept_d5.AcceptanceError):
        accept_d5.install_config_file(tmp_path / "nope.yaml", tmp_path / "data")


def test_forbidden_host_hits_flags_the_old_endpoint(tmp_path):
    clean = tmp_path / "clean.log"
    clean.write_text("GET /api/v1/health 200\n", encoding="utf-8")
    dirty = tmp_path / "dirty.log"
    dirty.write_text(
        "Error posting to https://api.minimax.cn/v1/chat/completions\n",
        encoding="utf-8")

    assert accept_d5.forbidden_host_hits([clean]) == []
    assert accept_d5.forbidden_host_hits([clean, dirty]) == [str(dirty)]
    assert accept_d5.forbidden_host_hits([tmp_path / "missing.log"]) == []


def test_default_config_is_none_and_does_not_touch_the_template(tmp_path):
    """默认不传 --config：不覆盖 EXE 生成的模板（也不把本机路径写进仓库）。"""
    args = accept_d5.parse_args([])
    assert args.config is None

    facts = accept_d5.prepare_config(args.config, tmp_path / "data")

    assert facts["explicit"] is False
    assert facts["source"] == accept_d5.TEMPLATE_CONFIG_LABEL
    assert not (tmp_path / "data" / "config" / "agent.yaml").exists()


def test_prepare_config_with_an_explicit_path_copies_before_launch(tmp_path):
    source = tmp_path / "agent.yaml"
    source.write_text(DASHSCOPE_CONFIG, encoding="utf-8")

    facts = accept_d5.prepare_config(str(source), tmp_path / "data")

    assert facts["explicit"] is True
    assert (tmp_path / "data" / "config" / "agent.yaml").read_bytes() == source.read_bytes()


def test_config_target_facts_reads_the_effective_file(tmp_path):
    target = tmp_path / "data" / "config" / "agent.yaml"
    target.parent.mkdir(parents=True)
    target.write_text(DASHSCOPE_CONFIG, encoding="utf-8")

    facts = accept_d5.config_target_facts(tmp_path / "data", "源标签")

    assert facts["source"] == "源标签"
    assert facts["target"] == str(target)
    assert facts["target_bytes"] == len(target.read_bytes())
    assert facts["contains_dashscope"] is True
    assert facts["providers"] == ["dashscope"]


# ------------------------------------------------------------------ 阶段 1 闸门快停

class _FakeCount:
    def __init__(self, present: bool):
        self.present = present

    def count(self) -> int:
        return 1 if self.present else 0


class _FakeGatePage:
    def __init__(self, texts: set[str]):
        self.texts = texts

    def get_by_text(self, text, exact=False):
        return _FakeCount(text in self.texts)

    def wait_for_timeout(self, _milliseconds):
        return None


def test_wait_review_gate_returns_when_gate_is_visible():
    page = _FakeGatePage({"等待人工"})
    assert accept_d5.wait_review_gate(page, 5_000) == "awaiting-review"


def test_wait_review_gate_stops_immediately_on_failed_session():
    """会话变「失败」时不能白等整步超时（2026-11-12 实测 402 的教训）。"""
    page = _FakeGatePage({"失败"})

    with pytest.raises(accept_d5.AcceptanceError) as excinfo:
        accept_d5.wait_review_gate(page, 60_000)

    assert "阶段 1 执行失败" in str(excinfo.value)
    assert "jobs[].error_code" in str(excinfo.value)


def test_wait_review_gate_times_out_when_neither_state_appears():
    page = _FakeGatePage(set())

    with pytest.raises(accept_d5.AcceptanceError) as excinfo:
        accept_d5.wait_review_gate(page, 300, poll_ms=50)

    assert "人工闸门" in str(excinfo.value)


# ------------------------------------------------------------------ 截图是证据不是判据

class _FakeShotPage:
    def __init__(self, fail_times: int):
        self.fail_times = fail_times
        self.calls: list[bool] = []

    def screenshot(self, path, timeout=None, full_page=None):
        self.calls.append(bool(full_page))
        if len(self.calls) <= self.fail_times:
            raise RuntimeError("Page.screenshot: Timeout 20000ms exceeded.")
        Path(path).write_bytes(b"png")


def test_shot_falls_back_from_viewport_to_full_page(tmp_path):
    page = _FakeShotPage(fail_times=1)
    facts: dict = {}

    path = accept_d5.shot(page, tmp_path, "B-03", facts)

    assert path and Path(path).is_file()
    assert page.calls == [False, True]
    assert facts["screenshots"] == [path]
    assert "screenshot_failures" not in facts


def test_shot_never_aborts_the_run_when_both_attempts_fail(tmp_path):
    """2026-11-12 实测：真实修订后整页截图卡满 30s，不能把已成功的步骤全带红。"""
    page = _FakeShotPage(fail_times=2)
    facts: dict = {}

    path = accept_d5.shot(page, tmp_path, "B-03", facts)

    assert path == ""
    assert page.calls == [False, True]
    assert facts["screenshot_failures"][0]["name"] == "B-03"
    assert len(facts["screenshot_failures"][0]["errors"]) == 2


# ------------------------------------------------------------------ 报告与 AC 表

def test_render_ac_rows_always_lists_all_sixteen_rows():
    rows = accept_d5.render_ac_rows({})

    assert len(rows) == 2 + 16
    assert rows[0].startswith("| AC |")
    for key in accept_d5.AC_KEYS:
        assert any(key in row for row in rows)
    assert "未验" in "\n".join(rows)


def test_render_ac_rows_uses_registered_status_and_evidence():
    rows = accept_d5.render_ac_rows({
        "D5-AC12": {"status": "成立", "evidence": "会话 abc/GEN001 completed"},
    })
    joined = "\n".join(rows)

    assert "| D5-AC12" in joined
    assert "成立" in joined
    assert "会话 abc/GEN001 completed" in joined
    # 其余条目不受影响。
    assert joined.count("未验") == 15


def test_report_lines_declare_degraded_clean_environment():
    report = accept_d5.Report()
    report.facts.update({
        "environment": {
            "os": "Windows-11", "variables": ["APPDATA", "PATH"],
            "path": accept_d5.SYSTEM_PATH, "apdata_empty": r"D:\temp\empty",
            "apdata_real": r"C:\Users\me\AppData\Roaming",
        },
        "pipeline": "real（真实 LLM）",
        "instances": [], "screenshots": [],
    })
    report.ac_set("D5-AC1", "降级", "非全新系统")

    text = "\n".join(accept_d5.report_lines(report))

    assert "非全新系统" in text
    assert "R 降级" in text
    assert accept_d5.SYSTEM_PATH in text
    assert "| D5-AC1" in text
    assert len(accept_d5.AC_KEYS) == 16
