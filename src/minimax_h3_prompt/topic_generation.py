"""面向用户的单入口：主题 → 剧本与四类提示词（落盘到项目内会话目录）。

不再依赖外部资产库：产出统一写到 ``<sessions_root>/<topic_slug>/GEN001/``。
只产提示词，不执行任何 ComfyUI 工作流，也不落盘 API key。
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .brief_parser import Brief, RefItem
from .config import Config
from .generation import GenerationResult, render_generation
from .graph.pipeline import run_pipeline_structured

_SLUG_KEEP_RE = re.compile(r"[^\w-]+", re.UNICODE)

# 生成产物类型 → 落盘文件名（与 render_generation 的键一致）。
_ARTIFACT_FILES = {
    "script": "script.md",
    "video": "video-prompt.md",
    "character": "character-prompt.md",
    "prop": "prop-prompt.md",
    "scene": "scene-prompt.md",
    "fl2va": "fl2va-prompt.md",
    "first-frame": "first-frame-prompt.md",
    "last-frame": "last-frame-prompt.md",
}


def topic_slug(topic: str) -> str:
    """主题摘要命名：可读前缀 + 哈希后缀，如 `雨夜旧信-a3f2b1c0`（可读且唯一）。"""
    text = _SLUG_KEEP_RE.sub("-", topic.strip()).strip("-")
    text = re.sub(r"-{2,}", "-", text)[:20].strip("-") or "topic"
    return f"{text}-{hashlib.sha256(topic.strip().encode('utf-8')).hexdigest()[:8]}"


def save_generation(result: GenerationResult, directory: str | Path, *, overwrite: bool = False) -> Path:
    """把生成结果落盘：generation.json + fl2va-prompt.json + 各提示词 Markdown。"""
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    _write_json(target / "generation.json", result.to_dict(), overwrite=overwrite)
    if result.fl2va_prompt_bundle is not None:
        _write_json(target / "fl2va-prompt.json", result.fl2va_prompt_bundle.to_dict(), overwrite=overwrite)
    for kind, content in render_generation(result).items():
        path = target / _ARTIFACT_FILES[kind]
        text = content.rstrip("\n") + "\n"
        if path.exists() and not overwrite and path.read_text(encoding="utf-8") != text:
            raise FileExistsError(f"目标已存在且内容不同：{path}")
        if not path.exists() or overwrite:
            path.write_text(text, encoding="utf-8")
    return target


def _write_json(path: Path, data: object, *, overwrite: bool = False) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    if path.exists() and not overwrite:
        if path.read_text(encoding="utf-8") != text:
            raise FileExistsError(f"目标已存在且内容不同：{path}")
        return
    path.write_text(text, encoding="utf-8")


def create_video_from_topic(
    topic: str,
    config: Config,
    *,
    duration: float | None = None,
    style: str | None = None,
    language: str | None = None,
    variant: str = "FL2VA",
    refs: list[RefItem] | tuple[RefItem, ...] = (),
) -> tuple[GenerationResult, Path]:
    """由主题生成剧本与提示词并落盘；不执行任何外部媒体工作流。"""
    if not topic or not topic.strip():
        raise ValueError("视频主题不能为空")
    topic = topic.strip()
    variant = variant.upper()
    directory = Path(config.sessions_root) / topic_slug(topic) / "GEN001"
    brief = Brief(
        mode="base",
        variant=variant,
        duration=duration or config.default_duration,
        style=style or config.default_style,
        language=language or config.default_language,
        plot=topic,
        raw=topic,
        refs=list(refs),
    )
    result = run_pipeline_structured(
        brief,
        config,
        generation_id="GEN001",
        topic_id=topic_slug(topic),
        project_id="GEN001",
    )
    save_generation(result, directory, overwrite=True)
    return result, directory


__all__ = ["create_video_from_topic", "save_generation", "topic_slug"]
