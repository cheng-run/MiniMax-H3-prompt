"""LLM 模型工厂：统一消费 config 中的模型配置。"""
from __future__ import annotations

from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI

from .config import ModelSettings, config

# DashScope MaaS 网关对长输出（16k+ token）整体响应有约 10 分钟天花板；
# 取 900s 单次超时兜底，关掉 openai 客户端自动重试——它重试会重发完整上下文，
# token 计费翻倍；重试由 run_agent 层面接管（最多 1 次）。
_LLM_TIMEOUT_SECONDS = 900
_LLM_MAX_RETRIES = 0

try:  # deepagents 依赖 langchain-anthropic，单独 import 避免硬依赖
    from langchain_anthropic import ChatAnthropic  # type: ignore
except ImportError:  # pragma: no cover
    ChatAnthropic = None  # type: ignore


def _require(settings: ModelSettings) -> str:
    try:
        return config.resolve_api_key(settings)
    except ValueError as exc:
        raise ValueError(
            f"缺失 {settings.api_key_env}：请在项目根 .env 中配置（LLM_PROVIDER={settings.provider}）"
        ) from exc


def _detect_provider() -> str:
    """兼容旧的自动探测 API；实际配置解析由 Config 统一完成。"""
    return config.primary_model.provider


def _anthropic(key: str, settings: ModelSettings) -> BaseChatModel:
    if ChatAnthropic is None:
        raise ImportError("langchain-anthropic 未安装：`uv add langchain-anthropic`")
    return ChatAnthropic(
        model=settings.model,
        api_key=key,
        base_url=settings.base_url,
        timeout=_LLM_TIMEOUT_SECONDS,
        max_retries=_LLM_MAX_RETRIES,
    )


def build_chat_model() -> BaseChatModel:
    settings = config.primary_model
    provider = settings.provider
    key = _require(settings)

    if provider == "anthropic":
        return _anthropic(key, settings)

    base_url = settings.base_url
    # deepseek 的 anthropic 兼容端点仍走 ChatAnthropic（历史行为，别动）。
    if provider == "deepseek" and base_url and "anthropic" in base_url:
        return _anthropic(key, settings)

    # 其余 provider 一律按 OpenAI 兼容端点处理：自建网关 / 中转站（opencode、one-api、
    # openrouter…）不需要预先登记进名单，base_url 与 model 由 agent.yaml 显式给出即可。
    # 这里原是一份四值白名单，2026-10-20 用户配 provider=opencode 被它拦下；而 config.py
    # 早已为任意 provider 生成 {PROVIDER}_API_KEY 默认名、视觉侧（reference_auditor）也从
    # 不校验 provider 名——白名单只活在工厂这一处。名字写错仍有上面的 _require 兜住
    # （会报「缺失 XXX_API_KEY」），不会静默打到 api.openai.com。
    return ChatOpenAI(
        model=settings.model,
        api_key=key,
        base_url=base_url,
        timeout=_LLM_TIMEOUT_SECONDS,
        max_retries=_LLM_MAX_RETRIES,
    )
