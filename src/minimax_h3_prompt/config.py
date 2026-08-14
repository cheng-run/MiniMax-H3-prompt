"""全局配置：读项目根 `config/agent.yaml` + `.env`（API key 一律从 .env 读取）。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

# 项目根目录：src/minimax_h3_prompt/config.py -> parents[0]=包, [1]=src, [2]=项目根
PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PROJECT_ROOT / "config" / "agent.yaml"


def _load_yaml(path: Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


class Config:
    """应用级配置。字段只读，导入即加载。"""

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

        cost = self.raw.get("cost", {}) or {}
        self.price_per_m_input: float = float(cost.get("price_per_m_input", 0.5))
        self.price_per_m_output: float = float(cost.get("price_per_m_output", 1.5))

        self.default_style: str = defaults.get("style", "Cinematic")
        self.default_language: str = defaults.get("language", "Chinese")
        # 可选：YAML 里覆盖模型名；优先级低于 .env 的 *_MODEL
        self.model_override: str = (self.raw.get("model") or "").strip()


config = Config()
