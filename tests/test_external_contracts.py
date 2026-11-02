"""外部契约（体检 §5 盲区 1 / P4）。

体检结论：导演包字段、网关流式/usage、ffmpeg 调用都没有「对着对方真实契约」的
测试——本仓的用例钉的是**自己模块的声明**，而 #142（导入把 ``continuityFromPrev``
翻成 true）正是这样全量绿着漏过去的。

三处契约，各自写明「谁是真源」，断言的是**对方会怎么读**，不是我们写了什么：

1. **OpenAI 兼容网关**：``streaming`` 与 ``stream_usage`` 必须同时显式开
   （2026-10-21 实测：只开前者，``usage_metadata`` 是 None，成本表记 0 而不报错）；
2. **ComfyUI_MiniMaxH3_Director 插件**：``REQUIRED_OUTPUT`` 是插件认得的字面契约，
   ``continuityFromPrev`` 缺字段时插件按 true 处理——所以切镜段必须显式写 false；
3. **ffmpeg**：一律列表参数、不传 ``shell``（注入面为零），路径由 imageio-ffmpeg 给。
"""
from __future__ import annotations

import subprocess

import pytest

from minimax_h3_prompt import model_factory
from minimax_h3_prompt.config import ModelSettings, config
from minimax_h3_prompt.director import pack
from minimax_h3_prompt.tools import seam_audit


def test_gateway_client_asks_for_streaming_and_usage_together(monkeypatch):
    """变异检验：把 ``stream_usage=True`` 删掉，本用例红。"""
    monkeypatch.setattr(config, "resolve_api_key", lambda settings: "test-key")
    model = model_factory.build_chat_model()

    assert model.streaming is True, "关掉流式＝「卡住」与「慢」再也分不开（#59）"
    assert model.stream_usage is True, "不开 usage 上报＝成本表数是空的且不报错"
    assert model.max_retries == 0, "客户端自动重试会把完整上下文重发一次、token 翻倍"


def test_anthropic_branch_also_asks_for_usage(monkeypatch):
    anthropic = pytest.importorskip("langchain_anthropic")
    monkeypatch.setattr(config, "resolve_api_key", lambda settings: "test-key")
    settings = ModelSettings(
        provider="anthropic", model="claude-test", base_url=None,
        api_key_env="ANTHROPIC_API_KEY", purpose="测试")
    model = model_factory._anthropic("test-key", settings)

    assert isinstance(model, anthropic.ChatAnthropic)
    assert model.streaming is True and model.stream_usage is True


def test_plugin_contract_keys_are_the_literal_ones_it_reads():
    """``REQUIRED_OUTPUT`` 的字面契约：值不是「我们写什么插件就用什么」。"""
    assert pack.REQUIRED_OUTPUT == {
        "continuityEnabled": True,
        "continuityOverlapFrames": pack.CONTINUITY_OVERLAP_FRAMES,
        "continuityKeepTail": True,
        "exportMode": "all",
    }
    assert pack.COMMON_ENABLED is False, (
        "打开公共参考会让参考编号顺延、段内挂图全部错位（设计 §3.4 约束 2）")


def test_ffmpeg_is_called_with_list_arguments_and_no_shell(tmp_path, monkeypatch):
    """ffmpeg 是所有「拼接/裁剪/探测」的唯一外部命令：参数表＋不 shell 是硬约束。"""
    calls: list[tuple] = []
    real_run = subprocess.run

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)
    from conftest import write_test_video

    video = write_test_video(tmp_path / "probe.mp4", frames=6)
    assert "video" in seam_audit.probe_streams(video)

    assert calls, "probe_streams 没有调用 ffmpeg"
    argv, kwargs = calls[0]
    assert isinstance(argv[0], list), "参数表必须是列表：拼成字符串就打开了注入面"
    assert all(isinstance(part, str) for part in argv[0])
    assert kwargs.get("shell") in (None, False)
