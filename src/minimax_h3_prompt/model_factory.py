"""LLM 模型工厂：按 `.env` 的 LLM_PROVIDER 选择后端。

支持：anthropic（含 DeepSeek Anthropic 兼容端点）/ openai / dashscope / deepseek。
未设 LLM_PROVIDER 时自动探测（DEEPSEEK_* → deepseek，ANTHROPIC_* → anthropic …）。
API key 一律从 .env 读取（config 导入时已 load_dotenv）。
"""
from __future__ import annotations

import os

from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI

from .config import config

try:  # deepagents 依赖 langchain-anthropic，单独 import 避免硬依赖
    from langchain_anthropic import ChatAnthropic  # type: ignore
except ImportError:  # pragma: no cover
    ChatAnthropic = None  # type: ignore


def _require(key: str, provider: str) -> str:
    value = os.getenv(key, "").strip()
    if not value:
        raise ValueError(f"缺失 {key}：请在项目根 .env 中配置（LLM_PROVIDER={provider}）")
    return value


def _detect_provider() -> str:
    if os.getenv("DEEPSEEK_API_KEY"):
        return "deepseek"
    if os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"):
        return "anthropic"
    if os.getenv("OPENAI_API_KEY"):
        return "openai"
    if os.getenv("DASHSCOPE_API_KEY"):
        return "dashscope"
    return "anthropic"


def _anthropic(key: str, base_url: str | None, model: str) -> BaseChatModel:
    if ChatAnthropic is None:
        raise ImportError("langchain-anthropic 未安装：`uv add langchain-anthropic`")
    return ChatAnthropic(model=model, api_key=key, base_url=base_url)


def build_chat_model() -> BaseChatModel:
    provider = (os.getenv("LLM_PROVIDER") or _detect_provider()).strip().lower()
    model_override = config.model_override or None

    if provider == "anthropic":
        key = os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN") or ""
        if not key:
            raise ValueError("缺失 ANTHROPIC_API_KEY（或 ANTHROPIC_AUTH_TOKEN）：请在 .env 配置")
        base_url = os.getenv("ANTHROPIC_BASE_URL", "").strip() or None
        model = os.getenv("ANTHROPIC_MODEL") or model_override or "deepseek-v4-flash"
        return _anthropic(key, base_url, model)

    if provider == "deepseek":
        key = _require("DEEPSEEK_API_KEY", provider)
        base_url = os.getenv("DEEPSEEK_BASE_URL", "").strip() or None
        model = os.getenv("DEEPSEEK_MODEL") or os.getenv("DEEPSEEK_MODEL_NAME") or model_override or "deepseek-chat"
        if base_url and "anthropic" in base_url:
            return _anthropic(key, base_url, model)
        return ChatOpenAI(model=model, api_key=key, base_url=base_url or "https://api.deepseek.com/v1")

    if provider == "openai":
        key = _require("OPENAI_API_KEY", provider)
        base_url = os.getenv("OPENAI_BASE_URL", "").strip() or None
        model = os.getenv("OPENAI_MODEL") or model_override or "gpt-4o"
        return ChatOpenAI(model=model, api_key=key, base_url=base_url)

    if provider == "dashscope":
        key = _require("DASHSCOPE_API_KEY", provider)
        base_url = os.getenv("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1").strip()
        model = os.getenv("DASHSCOPE_MODEL") or os.getenv("DASHSCOPE_MODEL_NAME") or model_override or "qwen3.7-max"
        return ChatOpenAI(model=model, api_key=key, base_url=base_url)

    raise ValueError(f"未知 LLM_PROVIDER: {provider}（应为 anthropic | openai | dashscope | deepseek）")
