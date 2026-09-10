"""全局配置：读项目根 `config/agent.yaml` + `.env`。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

# 项目根目录：src/minimax_h3_prompt/config.py -> parents[0]=包, [1]=src, [2]=项目根
PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PROJECT_ROOT / "config" / "agent.yaml"

# 执行段（Segment）拆分约束：H3 单次可执行窗口约 3-8s。
SEGMENT_CONFIG: dict[str, float | int] = {
    "default_segment_seconds": 7.0,   # 工作流 62 已验证的执行窗口
    "min_segment_seconds": 3.0,       # 低于此首尾帧几乎重合，无意义
    "max_segment_seconds": 8.0,       # H3 可执行窗口上限；Shot 超过即拆分
    "shot_seconds_ceiling": 8.0,      # > 8s 的镜头自动拆成连续段
    "warn_shot_count_below": 8,       # 60s 项目镜头少于 8 个时给出提示
}


@dataclass(frozen=True)
class ModelSettings:
    """模型的非敏感配置；API key 只在真正构造模型时从环境变量解析。"""

    provider: str
    model: str
    base_url: str | None
    api_key_env: str
    purpose: str


def _load_yaml(path: Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _env(*names: str) -> str:
    """返回第一个非空环境变量。"""
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def _detect_provider() -> str:
    if _env("DEEPSEEK_API_KEY"):
        return "deepseek"
    if _env("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        return "anthropic"
    if _env("OPENAI_API_KEY"):
        return "openai"
    if _env("DASHSCOPE_API_KEY"):
        return "dashscope"
    return "deepseek"


_DEFAULTS = {
    "deepseek": ("deepseek-chat", "https://api.deepseek.com/v1", "DEEPSEEK_API_KEY"),
    "anthropic": ("deepseek-v4-flash", None, "ANTHROPIC_API_KEY"),
    "openai": ("gpt-4o", None, "OPENAI_API_KEY"),
    "dashscope": (
        "qwen3.7-max",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "DASHSCOPE_API_KEY",
    ),
}


class Config:
    """应用级配置。字段只读（兼容既有运行时对 save_stages 的测试修改）。"""

    def __init__(self) -> None:
        load_dotenv(PROJECT_ROOT / ".env")

        self.raw = _load_yaml(CONFIG_PATH)
        pipeline = self.raw.get("pipeline", {}) or {}
        defaults = self.raw.get("defaults", {}) or {}

        self.default_duration: float = float(pipeline.get("default_duration", 5.0))
        self.max_qa_iterations: int = int(pipeline.get("max_qa_iterations", 2))
        self.roundtable_max_rounds: int = int(pipeline.get("roundtable_max_rounds", 2))
        self.output_path: Path = PROJECT_ROOT / pipeline.get("output_path", "output/final_prompt.txt")
        self.save_stages: bool = bool(pipeline.get("save_stages", True))
        self.stages_path: Path = PROJECT_ROOT / pipeline.get("stages_path", "output/stages")
        # 生图提示词 / 视频提示词的日常常识 QA（每代一次 LLM 审核；可关以省成本）。
        self.common_sense_qa: bool = bool(pipeline.get("common_sense_qa", True))

        cost = self.raw.get("cost", {}) or {}
        self.price_per_m_input: float = float(cost.get("price_per_m_input", 0.5))
        self.price_per_m_output: float = float(cost.get("price_per_m_output", 1.5))

        self.default_style: str = defaults.get("style", "Cinematic")
        self.default_language: str = defaults.get("language", "Chinese")
        # 资产库根目录（两阶段向导的会话落盘与帧图入库位置）。
        self.assets_root: str = str(defaults.get("assets_root", r"D:\笔记\Assets"))
        # 旧字段保留作为最低优先级的模型名 fallback。
        self.model_override: str = (self.raw.get("model") or "").strip()

        models = self.raw.get("models", {}) or {}
        self.primary_model = self._build_primary_settings(
            models.get("primary", {}) or {}, self.model_override
        )
        self.vision_model = self._build_vision_settings(models.get("vision", {}) or {})

    @staticmethod
    def _build_primary_settings(raw: dict[str, Any], legacy_model: str = "") -> ModelSettings:
        provider = _env("LLM_PROVIDER").lower() or str(raw.get("provider") or _detect_provider()).strip().lower()
        default_model, default_base_url, default_key_env = _DEFAULTS.get(
            provider, ("", None, f"{provider.upper()}_API_KEY")
        )
        model = _env(
            f"{provider.upper()}_MODEL",
            f"{provider.upper()}_MODEL_NAME",
        ) or str(raw.get("model") or "").strip() or legacy_model or default_model
        base_url = _env(f"{provider.upper()}_BASE_URL") or str(raw.get("base_url") or "").strip() or default_base_url
        if provider == "deepseek" and base_url == "https://api.deepseek.com":
            base_url = "https://api.deepseek.com/v1"
        key_env = str(raw.get("api_key_env") or default_key_env).strip()
        # 显式 LLM_PROVIDER 切换后，不应继续使用 YAML 中旧 provider 的默认 key 名。
        yaml_provider = str(raw.get("provider") or "").strip().lower()
        if yaml_provider and provider != yaml_provider and key_env == _DEFAULTS.get(yaml_provider, ("", None, key_env))[2]:
            key_env = default_key_env
        return ModelSettings(provider, model, base_url or None, key_env, "主模型")

    @staticmethod
    def _build_vision_settings(raw: dict[str, Any]) -> ModelSettings:
        provider = str(raw.get("provider") or "dashscope").strip().lower()
        default_model, default_base_url, default_key_env = _DEFAULTS["dashscope"]
        model = _env("DASHSCOPE_MODEL", "DASHSCOPE_MODEL_NAME") or str(raw.get("model") or "").strip() or "qwen3.7-plus"
        base_url = _env("DASHSCOPE_BASE_URL") or str(raw.get("base_url") or "").strip() or default_base_url
        key_env = str(raw.get("api_key_env") or default_key_env).strip()
        return ModelSettings(provider, model, base_url or None, key_env, "视觉模型")

    def resolve_api_key(self, settings: ModelSettings) -> str:
        """从已加载的 `.env` 环境中读取 key；绝不从 YAML 读取 key 值。"""
        key = _env(settings.api_key_env)
        if settings.provider == "anthropic" and not key and settings.api_key_env == "ANTHROPIC_API_KEY":
            key = _env("ANTHROPIC_AUTH_TOKEN")
        if not key:
            raise ValueError(
                f"缺失 {settings.api_key_env}：请在项目根 .env 中配置（{settings.purpose}）"
            )
        return key


config = Config()
