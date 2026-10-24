from __future__ import annotations

from unittest.mock import Mock

from minimax_h3_prompt import model_factory
from minimax_h3_prompt.config import ModelSettings


def _stub_openai_constructor(monkeypatch, settings: ModelSettings) -> Mock:
    """把工厂里的 ChatOpenAI 换成 Mock，只观察它被要求了什么配置。

    走 Mock 而不是真建客户端，是因为这条用例要断言的是「构造参数」这一外部行为：
    真建客户端会读 .env、可能发网络请求（见 tests/test_wizard_asset_loop.py 的教训）。
    """
    monkeypatch.setattr(model_factory.config, "primary_model", settings)
    monkeypatch.setattr(model_factory.config, "resolve_api_key", lambda _: "secret")
    constructor = Mock()
    monkeypatch.setattr(model_factory, "ChatOpenAI", constructor)
    return constructor


def test_build_deepseek_from_config(monkeypatch):
    """OpenAI 兼容分支：流式 + usage 上报 + 分相超时（issue #60）。

    这里刻意把读超时常量改成 7.0 再断言 `timeout.read == 7.0`——这是变异检验：
    实现若重抄了 30 这个字面量、没从常量派生，常量一改断言就红。同理，
    `_LLM_MAX_RETRIES` 保留断言（它防的是客户端重试重发完整上下文导致 token 翻倍），
    但不再钉 900 那个会变的数字（票面「钉行为不钉数字」）。
    """
    settings = ModelSettings(
        provider="deepseek",
        model="deepseek-v4-flash",
        base_url="https://api.deepseek.com/v1",
        api_key_env="DEEPSEEK_API_KEY",
        purpose="主模型",
    )
    monkeypatch.setattr(model_factory, "_LLM_READ_TIMEOUT_SECONDS", 7.0)
    constructor = _stub_openai_constructor(monkeypatch, settings)

    model_factory.build_chat_model()

    constructor.assert_called_once()
    kwargs = constructor.call_args.kwargs
    assert kwargs["model"] == "deepseek-v4-flash"
    assert kwargs["api_key"] == "secret"
    assert kwargs["base_url"] == "https://api.deepseek.com/v1"
    # 开流式让「服务端在不在动」变成可观测；usage 上报必须同时开，否则
    # usage_metadata 返回 None、成本表静默记 0 token 而不报错。
    assert kwargs["streaming"] is True
    assert kwargs["stream_usage"] is True
    assert kwargs["max_retries"] == model_factory._LLM_MAX_RETRIES
    # 流式下 httpx read 超时＝两次数据块之间的间隔，故它同时是首字节线与块间线。
    assert kwargs["timeout"].read == 7.0
    # 其余三相另给短值，不再被读超时拖着。
    assert kwargs["timeout"].connect == model_factory._LLM_CONNECT_TIMEOUT_SECONDS
    assert kwargs["timeout"].write == model_factory._LLM_WRITE_TIMEOUT_SECONDS
    assert kwargs["timeout"].pool == model_factory._LLM_POOL_TIMEOUT_SECONDS


def test_build_custom_openai_compatible_provider(monkeypatch):
    """未内置的 provider（自建网关/中转站）按 OpenAI 兼容端点建，不再被白名单拦下。

    2026-10-20 实测：`config/agent.yaml` 写 provider=opencode（opencode.ai/zen/go）
    时 `build_chat_model` 抛 `ValueError: 未知 LLM_PROVIDER`。而 config.py 早就为任意
    provider 生成 `{PROVIDER}_API_KEY` 默认名、工厂里的视觉侧（reference_auditor）也从
    不校验 provider 名——白名单只活在主模型这一处，属于自相矛盾。

    issue #60 之后，这条分支同样要开流式与 usage 上报：15 个角色、圆桌、阶段 2 共用
    这一个客户端，任何一条分支漏了都会让对应网关下的成本表变成空账。
    """
    settings = ModelSettings(
        provider="opencode",
        model="deepseek-v4.1-flash",
        base_url="https://opencode.ai/zen/go",
        api_key_env="OPENCODE_API_KEY",
        purpose="主模型",
    )
    monkeypatch.setattr(model_factory, "_LLM_READ_TIMEOUT_SECONDS", 7.0)
    constructor = _stub_openai_constructor(monkeypatch, settings)

    model_factory.build_chat_model()

    constructor.assert_called_once()
    kwargs = constructor.call_args.kwargs
    assert kwargs["model"] == "deepseek-v4.1-flash"
    assert kwargs["api_key"] == "secret"
    assert kwargs["base_url"] == "https://opencode.ai/zen/go"
    assert kwargs["streaming"] is True
    assert kwargs["stream_usage"] is True
    assert kwargs["max_retries"] == model_factory._LLM_MAX_RETRIES
    assert kwargs["timeout"].read == 7.0


def test_build_anthropic_streams_and_reports_usage(monkeypatch):
    """Anthropic 兼容分支也必须开流式与 usage 上报（issue #60）。

    该分支的 pydantic 字段（`default_request_timeout`）只收 float，传 httpx.Timeout
    会 ValidationError，所以四相形状在这里表达不出来——只能给一个数，取读超时常量。
    但 streaming / stream_usage 两个开关没有这个限制，必须显式写上：
    `stream_usage` 在 ChatAnthropic 里默认 True，可守卫测试钉的是「构造时明确要求了
    usage 上报」，依赖库默认值会在升级时静默变化。
    """
    settings = ModelSettings(
        provider="anthropic",
        model="claude-sonnet-5",
        base_url="https://api.anthropic.com",
        api_key_env="ANTHROPIC_API_KEY",
        purpose="主模型",
    )
    monkeypatch.setattr(model_factory.config, "primary_model", settings)
    monkeypatch.setattr(model_factory.config, "resolve_api_key", lambda _: "secret")
    constructor = Mock()
    monkeypatch.setattr(model_factory, "ChatAnthropic", constructor)
    monkeypatch.setattr(model_factory, "_LLM_READ_TIMEOUT_SECONDS", 7.0)

    model_factory.build_chat_model()

    constructor.assert_called_once()
    kwargs = constructor.call_args.kwargs
    assert kwargs["model"] == "claude-sonnet-5"
    assert kwargs["api_key"] == "secret"
    assert kwargs["base_url"] == "https://api.anthropic.com"
    assert kwargs["streaming"] is True
    assert kwargs["stream_usage"] is True
    assert kwargs["timeout"] == 7.0
    assert kwargs["max_retries"] == model_factory._LLM_MAX_RETRIES


def test_build_vision_uses_config_and_streams(monkeypatch):
    """视觉侧也从配置取（issue #60 的既有口径）；读图主路走流式（issue #71）。

    首字节线**从主档常量派生**：视觉与文本各写一个数就是两处会各自漂移的口径——
    改主档的 30 秒时读图不动，用户就得在两道线之间猜。所以这里 monkeypatch 常量，
    断言构造出来的就是那个数。
    """
    from minimax_h3_prompt import model_factory
    from minimax_h3_prompt.tools import reference_auditor

    settings = ModelSettings(
        provider="dashscope",
        model="qwen3.7-plus",
        base_url="https://vision.example/v1",
        api_key_env="DASHSCOPE_API_KEY",
        purpose="视觉模型",
    )
    monkeypatch.setattr(reference_auditor.config, "vision_model", settings)
    monkeypatch.setattr(reference_auditor.config, "resolve_api_key", lambda _: "secret")
    monkeypatch.setattr(model_factory, "_LLM_READ_TIMEOUT_SECONDS", 7.0)
    constructor = Mock()
    monkeypatch.setattr(reference_auditor, "ChatOpenAI", constructor)

    reference_auditor._build_vision_model()

    kwargs = constructor.call_args.kwargs
    assert kwargs["model"] == "qwen3.7-plus"
    assert kwargs["api_key"] == "secret"
    assert kwargs["base_url"] == "https://vision.example/v1"
    assert kwargs["streaming"] is True
    # 与主档同一条「开流式必须开 usage 上报」的纪律：漏了它成本表记 0 且不报错
    assert kwargs["stream_usage"] is True
    assert kwargs["max_retries"] == model_factory._LLM_MAX_RETRIES
    assert kwargs["timeout"].read == 7.0


def test_build_vision_fallback_stays_nonstreaming_with_a_long_timeout(monkeypatch):
    """网关不支持流式时的回退档：非流式、900 秒（那条路等的是完整响应，不是首字节）。"""
    from minimax_h3_prompt.tools import reference_auditor

    settings = ModelSettings(
        provider="dashscope",
        model="qwen3.7-plus",
        base_url="https://vision.example/v1",
        api_key_env="DASHSCOPE_API_KEY",
        purpose="视觉模型",
    )
    monkeypatch.setattr(reference_auditor.config, "vision_model", settings)
    monkeypatch.setattr(reference_auditor.config, "resolve_api_key", lambda _: "secret")
    constructor = Mock()
    monkeypatch.setattr(reference_auditor, "ChatOpenAI", constructor)

    reference_auditor._build_vision_model(streaming=False)

    kwargs = constructor.call_args.kwargs
    assert kwargs["streaming"] is False
    assert kwargs["timeout"] == reference_auditor._FALLBACK_TIMEOUT_SECONDS
