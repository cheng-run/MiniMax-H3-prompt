"""LLM 模型工厂：统一消费 config 中的模型配置。"""
from __future__ import annotations

import httpx
from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI

from .config import ModelSettings, config

# 关掉 openai 客户端自动重试——它重试会重发完整上下文，token 计费翻倍；
# 重试由 run_agent 层面接管，次数由调用预算决定（issue #59、#61）。
_LLM_MAX_RETRIES = 0

# 不要退回一个 900 秒的「单值覆盖四相」——那正是 issue #59 的病根：非流式下
# 「卡住」与「慢慢生成」不可区分，服务端攒完才回包，所以除了等满 900 秒没有任何
# 办法知道它是不是在干活（实测：非流式首块到达时刻 = 总耗时的 100%）。
# 改成流式后 httpx 的 read 超时天然就是「两次数据块之间的间隔」，故这一个数
# 同时是首字节线与块间线：30 秒没有首字节＝服务端没在干活，可以直接止损重试。
# 30 秒的余量依据：真实那一发（23097 字符）实测首字节 0.53–2.04 秒，健康完整跑
# 267 秒、块间 gap 最大 0.62 秒；30 秒对最大首字节有 15 倍余量（issue #59 探针 2）。
_LLM_READ_TIMEOUT_SECONDS = 30.0
# 建连与写请求不该跟读超时共命运：30 秒连不上已经在重试阈值之内，10 秒足够判定
# 端点不可达并换节点。pool 给 10 秒是因为并发节点只有三个角色，等配额不该拖。
_LLM_CONNECT_TIMEOUT_SECONDS = 10.0
_LLM_WRITE_TIMEOUT_SECONDS = 30.0
_LLM_POOL_TIMEOUT_SECONDS = 10.0

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
    # 该分支的 pydantic 字段（default_request_timeout）只收 float，传 httpx.Timeout
    # 会 ValidationError（2026-10-21 实测），四相形状在这里表达不出来——只能给一个数，
    # 取读超时常量，至少让首字节线在这一支也成立。
    # stream_usage 在 ChatAnthropic 里默认 True，仍显式写：守卫测试钉的是「构造时
    # 明确要求了 usage 上报」，依赖库默认值会在升级时静默变化；一旦漏掉，
    # usage_metadata 返回 None，成本表会记成 0 token 而不报错（「规则在、接线不在」第 9 次）。
    return ChatAnthropic(
        model=settings.model,
        api_key=key,
        base_url=settings.base_url,
        timeout=_LLM_READ_TIMEOUT_SECONDS,
        max_retries=_LLM_MAX_RETRIES,
        streaming=True,
        stream_usage=True,
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
    # streaming=True 让「服务端在不在动」变成可观测（首字节就是活体信号）；
    # stream_usage=True 是它的伴生必要条件：实测只开 streaming 不开它，invoke 回来的
    # usage_metadata 是 None，成本表「看着还在、数是空的」，不报错也不留痕。
    # ChatOpenAI 的 request_timeout 收 httpx.Timeout（alias 是 timeout），四相可以分开配；
    # read 同时承担首字节线与块间线，理由见上方常量注释。
    return ChatOpenAI(
        model=settings.model,
        api_key=key,
        base_url=base_url,
        timeout=httpx.Timeout(
            connect=_LLM_CONNECT_TIMEOUT_SECONDS,
            read=_LLM_READ_TIMEOUT_SECONDS,
            write=_LLM_WRITE_TIMEOUT_SECONDS,
            pool=_LLM_POOL_TIMEOUT_SECONDS,
        ),
        max_retries=_LLM_MAX_RETRIES,
        streaming=True,
        stream_usage=True,
    )
