"""面向用户的单入口：主题 → 项目 → 剧本与四类提示词。"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from .brief_parser import Brief, RefItem
from .config import Config
from .generation import GenerationResult
from .graph.pipeline import run_pipeline_structured
from .project_store import ProjectStore


_SLUG_KEEP_RE = re.compile(r"[^\w-]+", re.UNICODE)


def topic_slug(topic: str) -> str:
    """主题摘要命名：可读前缀 + 哈希后缀，如 `雨夜旧信-a3f2b1c0`（可读且唯一）。"""
    text = _SLUG_KEEP_RE.sub("-", topic.strip()).strip("-")
    text = re.sub(r"-{2,}", "-", text)[:20].strip("-") or "topic"
    return f"{text}-{hashlib.sha256(topic.strip().encode('utf-8')).hexdigest()[:8]}"


def _next_project_id(store: ProjectStore, topic_id: str) -> str:
    root = store.root / topic_id / "projects"
    if not root.exists():
        return "project-001"
    numbers = []
    for path in root.iterdir():
        if path.is_dir() and path.name.startswith("project-"):
            try:
                numbers.append(int(path.name.rsplit("-", 1)[1]))
            except ValueError:
                continue
    return f"project-{max(numbers, default=0) + 1:03d}"


def create_video_from_topic(
    topic: str,
    config: Config,
    *,
    root: str | Path = r"D:\笔记\Assets",
    duration: float | None = None,
    style: str | None = None,
    language: str | None = None,
    variant: str = "FL2VA",
    refs: list[RefItem] | tuple[RefItem, ...] = (),
) -> tuple[GenerationResult, Path]:
    """由主题自动创建项目并保存生成产物，不执行任何外部媒体工作流。"""
    if not topic or not topic.strip():
        raise ValueError("视频主题不能为空")
    topic = topic.strip()
    variant = variant.upper()
    store = ProjectStore(root)
    resolved_topic_id = topic_slug(topic)
    project_id = _next_project_id(store, resolved_topic_id)
    store.init_project(
        resolved_topic_id,
        project_id,
        topic[:120],
        duration_seconds=duration or config.default_duration,
        variant=variant,
        global_style=style or config.default_style,
    )
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
        topic_id=resolved_topic_id,
        project_id=project_id,
    )
    directory = store.save_generation_result(resolved_topic_id, project_id, result)
    return result, directory


__all__ = ["create_video_from_topic", "topic_slug"]
