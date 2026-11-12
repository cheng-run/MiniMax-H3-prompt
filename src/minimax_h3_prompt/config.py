"""全局配置：读配置目录的 `agent.yaml` + 密钥文件 `.env`（落点由 ``paths`` 按形态定）。"""
from __future__ import annotations

import difflib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from . import paths
from .errors import ConfigError
# 日志保留天数的默认值从实现处派生（不是在这里再抄一个 7）：改默认值只改一处，
# 配置读取与文件清理不会各说各话。
from .logging_setup import DEFAULT_RETENTION_DAYS

# 路径一律由 ``paths`` 唯一产出（#237 / spec #235 §6）：它是轻量模块，不读 YAML/.env，
# 因此在这里 import 不会引出循环依赖。
#
# 配置与密钥各走各的落点（#238 / spec #235 §7）：``CONFIG_PATH`` 跟着**数据根**走
# （冻结态 ``data/config/agent.yaml``，首次启动由 ``bootstrap`` 复制模板）；密钥文件
# 跟着**这台机器的人**走（``paths.env_file_path()``，冻结态在 ``%APPDATA%``）。
CONFIG_PATH = paths.config_path()

# 执行段（Segment）拆分约束：段时长必须为整数秒。**首尾帧链规划保守档 4–10**
# （ADR 0012；节点网格实测 4–15，工作流 62 未复核前规划不放宽——旧注释
# 「时长选项只有 4-10」是已过时的说法，别照抄回去）。多参规划档 4–15 见
# segment_planner.MIN_SEG_S/MAX_SEG_S；执行段/交接校验档见 preflight。
SEGMENT_CONFIG: dict[str, float | int] = {
    "default_segment_seconds": 10.0,  # 首尾帧链规划保守档上限
    "min_segment_seconds": 4.0,       # 执行段时长下限（两路线同）
    "max_segment_seconds": 10.0,      # 首尾帧链规划保守档上限；Shot 超过即拆分
    "shot_seconds_ceiling": 10.0,     # > 10s 的镜头自动拆成连续段
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


def missing_api_key_message(settings: ModelSettings, *, context: str | None = None) -> str:
    """缺密钥的统一文案：变量名 + 按形态算好的落点 + 「写完重启」。

    ``context`` 是括号里那句语境（用途 / provider），由调用方给；**路径与措辞只有这一处**
    ——``Config.resolve_api_key`` 与 ``model_factory._require`` 原先各拼一份，改一处文案
    另一处就过时（#243 两轴复审）。落点按形态算（``paths.env_file_path()``）：冻结后是
    ``%APPDATA%\\MiniMax-H3-prompt\\.env``，写死「项目根」会把用户指到不存在的文件上。
    """
    detail = context or settings.purpose
    return (f"缺失 {settings.api_key_env}：请在 {paths.env_file_path()} 中配置"
            f"（{detail}）。写好后重启程序生效。")


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

# 导演包参数的老配置兜底（issue #104）：值由插件侧口径决定，改动要同时改
# ``config/agent.yaml`` 的同名键与 ``director/pack.py`` 的注释。
_DIRECTOR_DEFAULTS = {
    "width": 736,
    "height": 1280,
    "ref_max_size": 864,
    "ref_image_size": "match",
}

# 段缓存根目录的默认值（issue #105）：与本机 ComfyUI 输出目录一致，只是**兜底**——
# 真正的口径是 ``director:`` 节，环境变量 ``H3_COMFY_OUTPUT_ROOT`` 优先级最高。
_COMFY_OUTPUT_ROOT_ENV = "H3_COMFY_OUTPUT_ROOT"
_DEFAULT_COMFY_OUTPUT_ROOT = "D:/Comfyui/ComfyUI/output"

# ---------------------------------------------------------------------------
# 配置 schema（体检 §4.4 / P3）：**未知键报错**。
#
# 为什么要有：``provider`` 拼成 ``provder`` 时旧实现静默回落到环境探测，用户以为
# 换了网关、实际没换（2026-10-20 换模型期间反复踩到）。键集合是契约，写错要响。
# 加新键时同步这里——少加一处，用户写了也不生效，那正是本守卫要消灭的形态。
# ---------------------------------------------------------------------------
_KNOWN_TOP = frozenset({
    "pipeline", "cost", "models", "director", "defaults", "model", "logging"})
_KNOWN_SECTIONS: dict[str, frozenset[str]] = {
    "pipeline": frozenset({
        "default_duration", "max_qa_iterations", "max_clarification_rounds",
        "roundtable_max_rounds", "output_path", "save_stages", "stages_path",
        "common_sense_qa",
    }),
    "cost": frozenset({"price_per_m_input", "price_per_m_output"}),
    "director": frozenset({
        "width", "height", "ref_max_size", "ref_image_size",
        "comfy_output_root", "frame_tolerance",
    }),
    "defaults": frozenset({"style", "language", "sessions_root"}),
    "models": frozenset({"primary", "vision"}),
    # 运行日志（#239 / spec #235 §8.1）：只放保留天数；日志目录由 paths 与
    # ``H3_LOG_DIR`` 决定，不是用户配置项。
    "logging": frozenset({"retention_days"}),
}
_KNOWN_MODEL_FIELDS: dict[str, frozenset[str]] = {
    "primary": frozenset({"provider", "model", "base_url", "api_key_env"}),
    # 视觉档**没有** provider：客户端恒为 ChatOpenAI（OpenAI 兼容协议），写别的值
    # 不会换协议、只会让报错文案说错（2026-11-02 体检 §4.4 裁定「删掉」）。
    "vision": frozenset({"model", "base_url", "api_key_env"}),
}
# 视觉档 ``ModelSettings.provider`` 的取值：它是**协议标签**不是网关名（网关名从
# base_url 的 host 现取）。写死一个常量，避免任何地方再把它当可配项读。
VISION_PROTOCOL_LABEL = "openai-compatible"


def _reject_unknown(where: str, raw: dict, known: frozenset[str]) -> None:
    """未知键报错并给最接近的候选（``provder`` → ``provider``）。"""
    unknown = [key for key in raw if key not in known]
    if not unknown:
        return
    hints = []
    for key in unknown:
        close = difflib.get_close_matches(str(key), sorted(known), n=1)
        hints.append(f"{key!r}" + (f"（是不是 {close[0]!r}？）" if close else ""))
    raise ConfigError(
        f"config/agent.yaml 的 {where} 有未识别的键：{'、'.join(hints)}。"
        f"可用键：{', '.join(sorted(known))}。"
        "拼错的键不会生效——旧实现会静默忽略它并回落到默认值。")


def validate_schema(raw: object) -> None:
    """校验 ``agent.yaml`` 的键集合与节形状；坏配置在**构造期**就报错。

    纯函数（不碰文件、不碰环境），用例可以直接喂 dict 做变异检验。
    """
    if raw is None:
        return
    if not isinstance(raw, dict):
        raise ConfigError(f"config/agent.yaml 顶层必须是映射，实际 {type(raw).__name__}")
    _reject_unknown("顶层", raw, _KNOWN_TOP)
    for section, known in _KNOWN_SECTIONS.items():
        if section not in raw or raw[section] is None:
            continue
        value = raw[section]
        if not isinstance(value, dict):
            raise ConfigError(
                f"config/agent.yaml 的 {section} 节必须是映射，实际 {type(value).__name__}")
        _reject_unknown(section, value, known)
    models = raw.get("models") or {}
    for name, known in _KNOWN_MODEL_FIELDS.items():
        if not isinstance(models, dict) or name not in models or models[name] is None:
            continue
        block = models[name]
        if not isinstance(block, dict):
            raise ConfigError(
                f"config/agent.yaml 的 models.{name} 必须是映射，实际 {type(block).__name__}")
        if name == "vision" and "provider" in block:
            raise ConfigError(
                "config/agent.yaml 的 models.vision.provider 已退役（2026-11-02，体检 §4.4）："
                "视觉档恒走 OpenAI 兼容协议（ChatOpenAI），写 provider 不会换协议、"
                "只会让报错文案说错。请删掉这一行；要换网关请改 base_url。")
        _reject_unknown(f"models.{name}", block, known)


class Config:
    """应用级配置。字段只读（兼容既有运行时对 save_stages 的测试修改）。"""

    def __init__(self) -> None:
        # 密钥文件按形态分流（#238 / spec #235 §7）：冻结态 ``%APPDATA%\<应用>\.env``，
        # 开发态仓库根 ``.env``。**不读** ``data/.env`` / ``data/config/.env``——作品目录
        # 会被整份拷走，密钥跟着走等于随作品泄露。路径每次现算，别在 import 期缓存成常量。
        load_dotenv(paths.env_file_path())

        self.raw = _load_yaml(CONFIG_PATH)
        validate_schema(self.raw)
        pipeline = self.raw.get("pipeline", {}) or {}
        defaults = self.raw.get("defaults", {}) or {}

        self.default_duration: float = float(pipeline.get("default_duration", 5.0))
        self.max_qa_iterations: int = int(pipeline.get("max_qa_iterations", 2))
        # 起步前置澄清最多问几个问题（0 = 关闭澄清）。与质检轮数上限并列的第二个「轮数上限」；
        # 向导每次提问时都从这个对象读，所以改 YAML 重启即生效，不存在第二处硬编码默认值。
        self.max_clarification_rounds: int = int(pipeline.get("max_clarification_rounds", 3))
        self.roundtable_max_rounds: int = int(pipeline.get("roundtable_max_rounds", 2))
        self.output_path: Path = paths.data_path(
            pipeline.get("output_path", "output/final_prompt.txt"))
        self.save_stages: bool = bool(pipeline.get("save_stages", True))
        self.stages_path: Path = paths.data_path(pipeline.get("stages_path", "output/stages"))
        # 生图提示词 / 视频提示词的日常常识 QA（每代一次 LLM 审核；可关以省成本）。
        self.common_sense_qa: bool = bool(pipeline.get("common_sense_qa", True))

        cost = self.raw.get("cost", {}) or {}
        self.price_per_m_input: float = float(cost.get("price_per_m_input", 0.5))
        self.price_per_m_output: float = float(cost.get("price_per_m_output", 1.5))

        # 运行日志保留天数（#239 / spec #235 §8.1）：缺这一节时取默认 7——老配置
        # 不能因为新加的键就启动失败。类型在这里判死：``retention_days: "7"`` 或
        # 布尔值会让清理逻辑按字符串/0 天比较，静默删掉全部或一条不删。
        log_section = self.raw.get("logging", {}) or {}
        retention = log_section.get("retention_days", DEFAULT_RETENTION_DAYS)
        if isinstance(retention, bool) or not isinstance(retention, int) or retention < 1:
            raise ConfigError(
                "config/agent.yaml 的 logging.retention_days 必须是正整数，"
                f"实际 {retention!r}")
        self.log_retention_days: int = retention

        self.default_style: str = defaults.get("style", "Cinematic")
        self.default_language: str = defaults.get("language", "Chinese")
        # 会话根目录：两阶段向导的会话落盘与关键帧暂存位置。冻结态固定 ``data/sessions``、
        # 开发态 ``output/sessions``——两条口径与理由写在 ``paths.sessions_root`` 里。
        self.sessions_root: Path = paths.sessions_root(defaults.get("sessions_root"))
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
        # provider 不再读配置（2026-11-02 体检 §4.4）：视觉客户端恒为 ChatOpenAI，
        # 这个字段只做「协议」标签，网关名从 base_url 的 host 现取。
        default_model, default_base_url, default_key_env = _DEFAULTS["dashscope"]
        model = _env("DASHSCOPE_MODEL", "DASHSCOPE_MODEL_NAME") or str(raw.get("model") or "").strip() or "qwen3.7-plus"
        base_url = _env("DASHSCOPE_BASE_URL") or str(raw.get("base_url") or "").strip() or default_base_url
        key_env = str(raw.get("api_key_env") or default_key_env).strip()
        return ModelSettings(VISION_PROTOCOL_LABEL, model, base_url or None, key_env, "视觉模型")

    def director_settings(self):
        """导演包参数（``director:`` 节，issue #104）：画布宽高、参考图尺寸。

        默认值与插件侧口径一致（``width/height`` 736×1280 是 H3 竖幅原生尺寸，
        ``ref_max_size`` 864 是导演台节点默认值，``ref_image_size`` 取插件与官方节点的
        默认值 ``match``——``max`` 是「按 ref_max_size 上限送图」的备选，2026-10-29
        用户裁定先按 ``match``），YAML 里显式写了一份、这里只是老配置的兜底。**读的时候
        就过打包器的校验门**（32 对齐 / match|max）：配置错了应当在这一步响亮报错，而不是
        等用户导入导演包、点了运行才发现后端拒绝画布。
        """
        from .director.pack import (
            DirectorPackSettings,
            validate_canvas,
            validate_ref_image_size,
        )

        raw = self.raw.get("director", {}) or {}
        if not isinstance(raw, dict):
            raise ConfigError(f"config/agent.yaml 的 director 节必须是对象：{raw!r}")

        def _read_int(key: str, fallback: int) -> int:
            value = raw.get(key, fallback)
            # bool 是 int 的子类：`width: true` 不该被当成 1 静默放行。
            if isinstance(value, bool) or not isinstance(value, int):
                raise ConfigError(
                    f"config/agent.yaml 的 director.{key} 必须是整数，实际 {value!r}")
            return value

        settings = DirectorPackSettings(
            width=_read_int("width", _DIRECTOR_DEFAULTS["width"]),
            height=_read_int("height", _DIRECTOR_DEFAULTS["height"]),
            ref_max_size=_read_int("ref_max_size", _DIRECTOR_DEFAULTS["ref_max_size"]),
            ref_image_size=str(raw.get("ref_image_size", _DIRECTOR_DEFAULTS["ref_image_size"])),
        )
        validate_canvas(settings.width, settings.height)
        validate_ref_image_size(settings.ref_image_size)
        return settings

    def director_comfy_output_root(self) -> Path:
        """ComfyUI 输出目录（段缓存的根，issue #105）。

        优先级：``H3_COMFY_OUTPUT_ROOT`` > ``director.comfy_output_root`` > 本机默认。
        **段缓存路径不写死在模块里**：同一份代码在不同机器上指着不同的 ComfyUI，
        写死默认只会让换机器的用户看到「一段缓存都找不到」而不知道该改哪。

        空值/非字符串**报错**：空根会让定位落到当前工作目录上，随后「扫缓存」就会
        扫错地方且不报错（那是最坏的一类失败）。
        """
        raw = self.raw.get("director", {}) or {}
        if not isinstance(raw, dict):
            raise ConfigError(f"config/agent.yaml 的 director 节必须是对象：{raw!r}")
        value = _env(_COMFY_OUTPUT_ROOT_ENV) or raw.get(
            "comfy_output_root", _DEFAULT_COMFY_OUTPUT_ROOT)
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(
                f"config/agent.yaml 的 director.comfy_output_root 必须是非空字符串，"
                f"实际 {value!r}")
        return Path(value.strip())

    def director_frame_tolerance(self) -> int:
        """有引导段的帧容差（issue #105，设计 §3.7）。

        默认值从 ``director.companion.DIRECTOR_FRAME_TOLERANCE`` 现取，不在本文件里抄
        一份字面量：判据住在 companion，配置只是用户的旋钮（本仓「守卫形态从常量派生」
        的纪律）。**方法内 import** 与 ``director_settings`` 同一形态，避免 config 顶层
        把 director 包整个拉进来。
        """
        from .director.companion import DIRECTOR_FRAME_TOLERANCE

        raw = self.raw.get("director", {}) or {}
        if not isinstance(raw, dict):
            raise ConfigError(f"config/agent.yaml 的 director 节必须是对象：{raw!r}")
        value = raw.get("frame_tolerance", DIRECTOR_FRAME_TOLERANCE)
        # bool 是 int 的子类：`frame_tolerance: true` 会被当成 1 静默放行。
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ConfigError(
                f"config/agent.yaml 的 director.frame_tolerance 必须是非负整数，"
                f"实际 {value!r}")
        return value

    def resolve_api_key(self, settings: ModelSettings) -> str:
        """从已加载的密钥文件环境中读取 key；绝不从 YAML 读取 key 值。"""
        key = self._lookup_api_key(settings)
        if not key:
            raise ConfigError(missing_api_key_message(settings))
        return key

    @staticmethod
    def _lookup_api_key(settings: ModelSettings) -> str:
        key = _env(settings.api_key_env)
        if settings.provider == "anthropic" and not key and settings.api_key_env == "ANTHROPIC_API_KEY":
            key = _env("ANTHROPIC_AUTH_TOKEN")
        return key

    def missing_api_keys(self) -> list[str]:
        """当前环境里缺失的密钥**变量名**（绝不含值），供健康接口与概览页提示用。

        按 ``api_key_env`` 派生而不是抄一份键名清单：``agent.yaml`` 换网关后这里自动
        跟着变（本仓「守卫形态要从常量派生」的纪律）。主/视觉两档同名时只列一次。
        """
        missing: list[str] = []
        for settings in (self.primary_model, self.vision_model):
            name = settings.api_key_env
            if name in missing:
                continue
            if not self._lookup_api_key(settings):
                missing.append(name)
        return missing


config = Config()
