"""LLM 模型工厂：统一消费 config 中的模型配置。"""
from __future__ import annotations

from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI

from .config import ModelSettings, config

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
    return ChatAnthropic(model=settings.model, api_key=key, base_url=settings.base_url)


def build_chat_model() -> BaseChatModel:
    settings = config.primary_model
    provider = settings.provider
    key = _require(settings)

    if provider == "anthropic":
        return _anthropic(key, settings)

    if provider in {"deepseek", "openai", "dashscope"}:
        base_url = settings.base_url
        if provider == "deepseek" and base_url and "anthropic" in base_url:
            return _anthropic(key, settings)
        return ChatOpenAI(model=settings.model, api_key=key, base_url=base_url)

    raise ValueError(f"未知 LLM_PROVIDER: {provider}（应为 anthropic | openai | dashscope | deepseek）")
