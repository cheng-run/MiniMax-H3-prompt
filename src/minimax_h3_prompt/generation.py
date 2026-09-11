"""项目级剧本与提示词生成结果模型、持久化和人工查看适配。"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from .brief_parser import Brief
from .tools.theme_guard import (
    _CONFLICTING_OUTDOOR_TERMS,
    contains_unqualified_conflict as _contains_unqualified_conflict,
    scene_requirement_groups,
)

PromptKind = Literal["script", "video", "character", "prop", "scene"]
_FRAME_MODELS = ("zimage", "flux2")
_PROMPT_KINDS = ("script", "video", "character", "prop", "scene")
_GENERATION_ID = re.compile(r"^GEN\d{3}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

_IMAGE_PROFILE_DEFAULTS = {
    "zimage": ("zimage_t2i_v1", "10", 640, 1280, "candidate"),
    "flux2": ("flux2_t2i_v1", "118", 1024, 1024, "candidate"),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _require_text(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} 不能为空")


def _infer_frame_variant(first: tuple, last: tuple) -> str:
    """从关键帧存在性推断变体：双帧→FL2VA、仅首帧→I2VA、仅尾帧→L2VA、无帧→FL2VA（保持报缺失错误）。"""
    if first and last:
        return "FL2VA"
    if first:
        return "I2VA"
    if last:
        return "L2VA"
    return "FL2VA"


def _variant_frame_names(variant: str) -> tuple[str, ...]:
    return {"FL2VA": ("first", "last"), "I2VA": ("first",), "L2VA": ("last",)}[str(variant).upper()]


@dataclass(frozen=True)
class PromptArtifact:
    """一份供用户查看、复制的剧本或提示词文本；不是媒体资产。"""

    artifact_id: str
    kind: PromptKind
    content: str
    source_stage: str
    sha256: str = ""
    version: str = "v001"
    created_at: str = ""
    schema_version: str = "prompt_artifact.v1"

    def __post_init__(self) -> None:
        _require_text(self.artifact_id, "artifact_id")
        if self.kind not in _PROMPT_KINDS:
            raise ValueError(f"提示词产物类型无效：{self.kind}")
        _require_text(self.content, f"{self.kind} 内容")
        _require_text(self.source_stage, "source_stage")
        expected = _sha256(self.content)
        if self.sha256 and self.sha256 != expected:
            raise ValueError("提示词产物 sha256 与正文不一致")
        object.__setattr__(self, "sha256", expected)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> PromptArtifact:
        return cls(
            artifact_id=str(raw.get("artifact_id", "")),
            kind=str(raw.get("kind", "")),
            content=str(raw.get("content", "")),
            source_stage=str(raw.get("source_stage", "")),
            sha256=str(raw.get("sha256", "")),
            version=str(raw.get("version", "v001")),
            created_at=str(raw.get("created_at", "")),
            schema_version=str(raw.get("schema_version", "prompt_artifact.v1")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "kind": self.kind,
            "content": self.content,
            "source_stage": self.source_stage,
            "sha256": self.sha256,
            "version": self.version,
            "created_at": self.created_at,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class ImagePromptVariant:
    """一份针对具体生图模型、可直接复制的提示词。"""

    model_family: Literal["zimage", "flux2"]
    kind: Literal["character", "prop", "scene"]
    positive_prompt: str
    negative_prompt: str = ""
    profile_id: str = ""
    prompt_node_id: str = ""
    width: int = 0
    height: int = 0
    profile_status: str = "candidate"
    instructions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.model_family not in {"zimage", "flux2"}:
            raise ValueError(f"生图模型类型无效：{self.model_family}")
        if self.kind not in {"character", "prop", "scene"}:
            raise ValueError(f"生图产物类型无效：{self.kind}")
        _require_text(self.positive_prompt, "positive_prompt")
        if self.width < 0 or self.height < 0:
            raise ValueError("生图尺寸不能为负数")

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ImagePromptVariant:
        return cls(
            model_family=str(raw.get("model_family", "")),
            kind=str(raw.get("kind", "")),
            positive_prompt=str(raw.get("positive_prompt", "")),
            negative_prompt=str(raw.get("negative_prompt", "")),
            profile_id=str(raw.get("profile_id", "")),
            prompt_node_id=str(raw.get("prompt_node_id", "")),
            width=int(raw.get("width", 0) or 0),
            height=int(raw.get("height", 0) or 0),
            profile_status=str(raw.get("profile_status", "candidate")),
            instructions=tuple(str(x) for x in raw.get("instructions", [])),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_family": self.model_family,
            "kind": self.kind,
            "positive_prompt": self.positive_prompt,
            "negative_prompt": self.negative_prompt,
            "profile_id": self.profile_id,
            "prompt_node_id": self.prompt_node_id,
            "width": self.width,
            "height": self.height,
            "profile_status": self.profile_status,
            "instructions": list(self.instructions),
        }


@dataclass(frozen=True)
class FL2VAFramePrompt:
    """一张 FL2VA 首帧或尾帧的静态生图提示词。"""

    frame: Literal["first", "last"]
    model_family: Literal["zimage", "flux2"]
    positive_prompt: str
    negative_prompt: str = ""
    shot_id: str = "SH001"
    time_seconds: float = 0.0
    scene_anchor: str = ""
    subject_anchor: str = ""
    composition: str = ""
    lighting: str = ""
    profile_id: str = ""
    prompt_node_id: str = ""
    width: int = 0
    height: int = 0
    profile_status: str = "candidate"
    instructions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.frame not in {"first", "last"}:
            raise ValueError(f"FL2VA 帧类型无效：{self.frame}")
        if self.model_family not in _FRAME_MODELS:
            raise ValueError(f"FL2VA 生图模型类型无效：{self.model_family}")
        _require_text(self.positive_prompt, "FL2VA positive_prompt")
        if self.time_seconds < 0:
            raise ValueError("FL2VA 帧时间不能为负数")
        if self.width < 0 or self.height < 0:
            raise ValueError("FL2VA 生图尺寸不能为负数")

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "FL2VAFramePrompt":
        profile = _IMAGE_PROFILE_DEFAULTS.get(str(raw.get("model_family", "zimage")))
        if profile is None:
            profile = ("", "", 0, 0, "candidate")
        return cls(
            frame=str(raw.get("frame", "")),
            model_family=str(raw.get("model_family", "")),
            positive_prompt=str(raw.get("positive_prompt") or raw.get("prompt", "")),
            negative_prompt=str(raw.get("negative_prompt", "")),
            shot_id=str(raw.get("shot_id", "SH001")),
            time_seconds=float(raw.get("time_seconds", 0) or 0),
            scene_anchor=str(raw.get("scene_anchor", "")),
            subject_anchor=str(raw.get("subject_anchor", "")),
            composition=str(raw.get("composition", "")),
            lighting=str(raw.get("lighting", "")),
            profile_id=str(raw.get("profile_id", profile[0])),
            prompt_node_id=str(raw.get("prompt_node_id", profile[1])),
            width=int(raw.get("width", profile[2]) or profile[2]),
            height=int(raw.get("height", profile[3]) or profile[3]),
            profile_status=str(raw.get("profile_status", profile[4])),
            instructions=tuple(str(x) for x in raw.get("instructions", [])),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "frame": self.frame,
            "model_family": self.model_family,
            "positive_prompt": self.positive_prompt,
            "negative_prompt": self.negative_prompt,
            "shot_id": self.shot_id,
            "time_seconds": self.time_seconds,
            "scene_anchor": self.scene_anchor,
            "subject_anchor": self.subject_anchor,
            "composition": self.composition,
            "lighting": self.lighting,
            "profile_id": self.profile_id,
            "prompt_node_id": self.prompt_node_id,
            "width": self.width,
            "height": self.height,
            "profile_status": self.profile_status,
            "instructions": list(self.instructions),
        }


@dataclass(frozen=True)
class FL2VAPromptBundle:
    """同一段 FL2VA 视频的首帧/尾帧融合提示词集合。"""

    scene_anchor: str
    first: tuple[FL2VAFramePrompt, ...]
    last: tuple[FL2VAFramePrompt, ...]
    continuity_constraints: tuple[str, ...] = ()
    profile_id: str = "h3_fl2va_v2"
    prompt_node_id: str = "187"
    prompt_input: str = "value"
    first_frame_slot_id: str = "first_frame"
    last_frame_slot_id: str = "last_frame"
    required_scene_terms: tuple[tuple[str, ...], ...] = ()
    schema_version: str = "fl2va_prompt_bundle.v1"

    def __post_init__(self) -> None:
        _require_text(self.scene_anchor, "FL2VA scene_anchor")
        variant = _infer_frame_variant(self.first, self.last)
        if variant == "FL2VA" and (not self.first or not self.last):
            raise ValueError("FL2VA 必须同时包含首帧和尾帧提示词")
        for frame_name in _variant_frame_names(variant):
            rows = getattr(self, frame_name)
            models = {row.model_family for row in rows}
            if len(models) != len(rows):
                raise ValueError(f"{variant} {frame_name} 帧不能重复同一生图模型")
            if any(row.frame != frame_name for row in rows):
                raise ValueError(f"{variant} {frame_name} 帧包含错误的 frame 标识")
            if any(not row.scene_anchor for row in rows):
                raise ValueError(f"{variant} {frame_name} 帧缺少场景锚点")
        if variant == "FL2VA" and self.profile_id != "h3_fl2va_v2":
            raise ValueError(f"FL2VA Profile 无效：{self.profile_id}")
        if variant == "FL2VA" and (not self.first_frame_slot_id or not self.last_frame_slot_id):
            raise ValueError("FL2VA 首尾帧输入槽不能为空")

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "FL2VAPromptBundle":
        def parse_frame(frame_name: str) -> tuple[FL2VAFramePrompt, ...]:
            value = raw.get(frame_name, [])
            if isinstance(value, dict):
                rows = []
                for model_family in _FRAME_MODELS:
                    item = value.get(model_family)
                    if isinstance(item, dict):
                        rows.append(FL2VAFramePrompt.from_dict({
                            **item, "frame": frame_name, "model_family": model_family,
                            "scene_anchor": str(item.get("scene_anchor") or raw.get("scene_anchor", "")),
                            "shot_id": str(item.get("shot_id") or value.get("shot_id", "SH001")),
                            "time_seconds": item.get("time_seconds", value.get("time_seconds", 0)),
                        }))
                if not rows and (value.get("positive_prompt") or value.get("prompt")):
                    rows.append(FL2VAFramePrompt.from_dict({
                        **value, "frame": frame_name, "model_family": value.get("model_family", "zimage"),
                        "scene_anchor": str(value.get("scene_anchor") or raw.get("scene_anchor", "")),
                    }))
                return tuple(rows)
            return tuple(
                FL2VAFramePrompt.from_dict(item)
                for item in value
                if isinstance(item, dict)
            )

        first = parse_frame("first")
        last = parse_frame("last")
        variant = _infer_frame_variant(first, last)
        profile_default = "h3_fl2va_v2" if variant == "FL2VA" else ""
        return cls(
            scene_anchor=str(raw.get("scene_anchor", "")),
            first=first,
            last=last,
            continuity_constraints=tuple(str(x) for x in raw.get("continuity_constraints", [])),
            profile_id=str(raw.get("profile_id") or profile_default),
            prompt_node_id=str(raw.get("prompt_node_id", "187")),
            prompt_input=str(raw.get("prompt_input", "value")),
            first_frame_slot_id=str(raw.get("first_frame_slot_id", "first_frame")),
            last_frame_slot_id=str(raw.get("last_frame_slot_id", "last_frame")),
            required_scene_terms=tuple(
                tuple(str(term) for term in group)
                for group in raw.get("required_scene_terms", [])
            ),
            schema_version=str(raw.get("schema_version", "fl2va_prompt_bundle.v1")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "scene_anchor": self.scene_anchor,
            "first": [item.to_dict() for item in self.first],
            "last": [item.to_dict() for item in self.last],
            "continuity_constraints": list(self.continuity_constraints),
            "profile_id": self.profile_id,
            "prompt_node_id": self.prompt_node_id,
            "prompt_input": self.prompt_input,
            "first_frame_slot_id": self.first_frame_slot_id,
            "last_frame_slot_id": self.last_frame_slot_id,
            "required_scene_terms": [list(group) for group in self.required_scene_terms],
            "schema_version": self.schema_version,
        }


def validate_fl2va_bundle(
    bundle: FL2VAPromptBundle,
    *,
    duration: float | None = None,
    variant: str | None = None,
) -> list[str]:
    """返回该变体关键帧提示词的确定性问题，不调用模型或外部服务。"""
    variant = str(variant or _infer_frame_variant(bundle.first, bundle.last)).upper()
    issues: list[str] = []
    if variant == "FL2VA":
        if bundle.first_frame_slot_id != "first_frame" or bundle.last_frame_slot_id != "last_frame":
            issues.append("FL2VA_FRAME_SLOT_MISMATCH")
    if bundle.prompt_node_id != "187":
        issues.append("FL2VA_PROMPT_NODE_MISMATCH")
    if not bundle.continuity_constraints:
        issues.append("FL2VA_CONTINUITY_MISSING")
    # 单一要求地点组时：逐帧校验（防漂移语义）。
    # 多场景穿越题材（森林→海边→星空）下"每帧都含全部地点词"不成立：
    # 只要求每组地点词在 anchor 或至少一个关键帧里被覆盖（覆盖式校验）。
    groups = [group for group in bundle.required_scene_terms if group]
    multi_scene = len(groups) > 1
    all_frame_text = " ".join(
        row.positive_prompt
        for frame_name in _variant_frame_names(variant)
        for row in getattr(bundle, frame_name)
    ).lower()
    anchor_lower = bundle.scene_anchor.lower()
    for frame_name in _variant_frame_names(variant):
        for row in getattr(bundle, frame_name):
            if row.scene_anchor != bundle.scene_anchor:
                issues.append(f"FL2VA_{frame_name.upper()}_ANCHOR_MISMATCH")
            if not row.positive_prompt.strip():
                issues.append(f"FL2VA_{frame_name.upper()}_PROMPT_EMPTY")
            if frame_name == "first" and row.time_seconds != 0:
                issues.append("FL2VA_FIRST_FRAME_TIME_MISMATCH")
            if duration is not None and frame_name == "last" and row.time_seconds > duration:
                issues.append("FL2VA_LAST_FRAME_TIME_EXCEEDS_DURATION")
            if multi_scene:
                # 覆盖式：每组词至少在 anchor 或某帧出现
                prompt_text = row.positive_prompt.lower()
                for group in groups:
                    if not any(term.lower() in anchor_lower for term in group) \
                            and not any(term.lower() in all_frame_text for term in group):
                        issues.append("FL2VA_SCENE_GROUP_UNCOVERED")
                    elif not any(term.lower() in anchor_lower for term in group) \
                            and not any(term.lower() in prompt_text for term in group) \
                            and any(term.lower() in all_frame_text for term in group):
                        pass  # 该组由其他帧覆盖：合法的多场景分布
            else:
                for group in groups:
                    if not any(term.lower() in anchor_lower for term in group):
                        issues.append("FL2VA_SCENE_ANCHOR_MISMATCH")
                    if not any(term.lower() in row.positive_prompt.lower() for term in group):
                        issues.append("FL2VA_SCENE_PROMPT_MISMATCH")
            if groups:
                for term in _CONFLICTING_OUTDOOR_TERMS:
                    if _contains_unqualified_conflict(row.positive_prompt, term):
                        issues.append("FL2VA_SCENE_DRIFT")
    return list(dict.fromkeys(issues))


def fl2va_bundle_from_dict(raw: dict[str, Any], brief: Brief) -> FL2VAPromptBundle:
    """把模型 JSON 补齐为项目协议，并执行主题地点约束校验。"""
    enriched = dict(raw)
    enriched["required_scene_terms"] = [list(group) for group in scene_requirement_groups(brief.plot)]
    for frame_name, default_time in (("first", 0.0), ("last", brief.duration)):
        value = enriched.get(frame_name)
        if not isinstance(value, dict):
            continue
        frame_value = dict(value)
        frame_value.setdefault("time_seconds", default_time)
        for model_family in _FRAME_MODELS:
            item = frame_value.get(model_family)
            if isinstance(item, dict):
                item_value = dict(item)
                item_value.setdefault("time_seconds", default_time)
                frame_value[model_family] = item_value
        enriched[frame_name] = frame_value
    bundle = FL2VAPromptBundle.from_dict(enriched)
    if str(brief.variant).upper() == "FL2VA" and (not bundle.first or not bundle.last):
        raise ValueError("FL2VA 必须同时包含首帧和尾帧提示词")
    issues = validate_fl2va_bundle(bundle, duration=brief.duration, variant=brief.variant)
    if issues:
        raise ValueError("FL2VA 首尾帧提示词校验失败：" + ", ".join(issues))
    return bundle


@dataclass(frozen=True)
class GenerationResult:
    """一次完整管线生成的可恢复项目产物。"""

    generation_id: str
    topic_id: str
    project_id: str
    script: str
    artifacts: tuple[PromptArtifact, ...]
    mode: str
    variant: str
    duration: float
    style: str
    language: str
    brief_snapshot: dict[str, Any] = field(default_factory=dict)
    image_prompts: tuple[ImagePromptVariant, ...] = ()
    fl2va_prompt_bundle: FL2VAPromptBundle | None = None
    created_at: str = ""
    schema_version: str = "generation_result.v1"

    def __post_init__(self) -> None:
        if not _GENERATION_ID.fullmatch(self.generation_id):
            raise ValueError(f"generation_id 格式无效：{self.generation_id}")
        _require_text(self.topic_id, "topic_id")
        _require_text(self.project_id, "project_id")
        _require_text(self.script, "script")
        if self.duration <= 0:
            raise ValueError("duration 必须大于 0")
        kinds = [artifact.kind for artifact in self.artifacts]
        if self.variant.upper() == "FL2VA":
            if self.fl2va_prompt_bundle is None:
                raise ValueError("FL2VA GenerationResult 必须包含首尾帧提示词")
            if kinds != ["video"]:
                raise ValueError("FL2VA GenerationResult 只保存 video artifact；人物、道具、场景必须融合到首尾帧")
        else:
            missing = [kind for kind in _PROMPT_KINDS if kind != "script" and kind not in kinds]
            if "script" in kinds or missing:
                raise ValueError("GenerationResult 的 artifacts 只能保存四类提示词，且不能重复或缺失")
            if len(kinds) != len(set(kinds)):
                raise ValueError("GenerationResult 不允许重复提示词类型")
        for artifact in self.artifacts:
            if not _SHA256.fullmatch(artifact.sha256):
                raise ValueError("提示词产物必须带有效 SHA-256")
        if self.fl2va_prompt_bundle is not None:
            issues = validate_fl2va_bundle(self.fl2va_prompt_bundle, duration=self.duration)
            if issues:
                raise ValueError("FL2VA 首尾帧提示词校验失败：" + ", ".join(issues))

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> GenerationResult:
        return cls(
            generation_id=str(raw.get("generation_id", "")),
            topic_id=str(raw.get("topic_id", "")),
            project_id=str(raw.get("project_id", "")),
            script=str(raw.get("script", "")),
            artifacts=tuple(PromptArtifact.from_dict(x) for x in raw.get("artifacts", [])),
            mode=str(raw.get("mode", "base")),
            variant=str(raw.get("variant", "T2VA")),
            duration=float(raw.get("duration", 0)),
            style=str(raw.get("style", "")),
            language=str(raw.get("language", "")),
            brief_snapshot=dict(raw.get("brief_snapshot", {})),
            image_prompts=tuple(ImagePromptVariant.from_dict(x) for x in raw.get("image_prompts", [])),
            fl2va_prompt_bundle=(
                FL2VAPromptBundle.from_dict(raw["fl2va_prompt_bundle"])
                if isinstance(raw.get("fl2va_prompt_bundle"), dict)
                else None
            ),
            created_at=str(raw.get("created_at", "")),
            schema_version=str(raw.get("schema_version", "generation_result.v1")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "generation_id": self.generation_id,
            "topic_id": self.topic_id,
            "project_id": self.project_id,
            "script": self.script,
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
            "mode": self.mode,
            "variant": self.variant,
            "duration": self.duration,
            "style": self.style,
            "language": self.language,
            "brief_snapshot": self.brief_snapshot,
            "image_prompts": [item.to_dict() for item in self.image_prompts],
            "fl2va_prompt_bundle": (
                self.fl2va_prompt_bundle.to_dict()
                if self.fl2va_prompt_bundle is not None else None
            ),
            "created_at": self.created_at,
            "schema_version": self.schema_version,
        }

    def artifact(self, kind: str) -> PromptArtifact:
        if kind == "script":
            return PromptArtifact(
                f"{self.generation_id}-script", "script", self.script, "script",
                created_at=self.created_at,
            )
        for item in self.artifacts:
            if item.kind == kind:
                return item
        raise KeyError(f"生成结果中不存在提示词类型：{kind}")


def brief_snapshot(brief: Brief) -> dict[str, Any]:
    """只保留 Brief 的非秘密内容，不保存运行时配置或 API key。"""
    return {
        "mode": brief.mode,
        "variant": brief.variant,
        "duration": brief.duration,
        "style": brief.style,
        "language": brief.language,
        "plot": brief.plot,
        "refs": [
            {"picture": ref.picture, "name": ref.name, "description": ref.description}
            for ref in brief.refs
        ],
    }


def result_from_state(
    state: dict[str, Any], brief: Brief, *, generation_id: str, topic_id: str, project_id: str,
) -> GenerationResult:
    values = {
        "script": state.get("script", ""),
        "video": state.get("final_prompt", ""),
        "character": state.get("character_design", ""),
        "prop": state.get("prop_design", ""),
        "scene": state.get("background_design", ""),
    }
    if brief.variant.upper() == "FL2VA":
        required = {"script": values["script"], "video": values["video"]}
        missing = [kind for kind, content in required.items() if not str(content).strip()]
        if missing:
            raise ValueError("管线缺少结构化产物：" + ", ".join(missing))
        raw_bundle = state.get("fl2va_prompt_bundle")
        if not isinstance(raw_bundle, dict):
            raise ValueError("管线缺少 FL2VA 首尾帧提示词")
        image_bundle = fl2va_bundle_from_dict(raw_bundle, brief)
        artifacts = (
            PromptArtifact(
                artifact_id=f"{generation_id}-video",
                kind="video",
                content=str(values["video"]),
                source_stage="final_prompt",
                created_at=_now(),
            ),
        )
        image_prompts: tuple[ImagePromptVariant, ...] = ()
    else:
        missing = [kind for kind, content in values.items() if not str(content).strip()]
        if missing:
            raise ValueError("管线缺少结构化产物：" + ", ".join(missing))
        artifacts = tuple(
            PromptArtifact(
                artifact_id=f"{generation_id}-{kind}",
                kind=kind,
                content=str(values[kind]),
                source_stage={"video": "final_prompt", "character": "character_design", "prop": "prop_design", "scene": "background_design"}[kind],
                created_at=_now(),
            )
            for kind in ("video", "character", "prop", "scene")
        )
        image_bundle = (
            FL2VAPromptBundle.from_dict(state["fl2va_prompt_bundle"])
            if isinstance(state.get("fl2va_prompt_bundle"), dict)
            else None
        )
        image_prompts = image_prompt_variants(state, brief)

    return GenerationResult(
        generation_id=generation_id,
        topic_id=topic_id,
        project_id=project_id,
        script=str(values["script"]),
        artifacts=artifacts,
        mode=brief.mode,
        variant=brief.variant,
        duration=brief.duration,
        style=brief.style,
        language=brief.language,
        brief_snapshot=brief_snapshot(brief),
        image_prompts=image_prompts,
        fl2va_prompt_bundle=image_bundle,
        created_at=_now(),
        schema_version="generation_result.v2" if image_bundle is not None else "generation_result.v1",
    )


def generation_directory(project_directory: Path, generation_id: str) -> Path:
    if not _GENERATION_ID.fullmatch(generation_id):
        raise ValueError(f"generation_id 格式无效：{generation_id}")
    return project_directory / "generations" / generation_id


def image_prompt_variants(state: dict[str, Any], brief: Brief) -> tuple[ImagePromptVariant, ...]:
    """从结构化适配器结果构造六份可复制提示词；旧 state 缺少时保留兼容降级。"""
    profile = {
        "zimage": ("zimage_t2i_v1", "10", 640, 1280),
        "flux2": ("flux2_t2i_v1", "118", 1024, 1024),
    }
    variants: list[ImagePromptVariant] = []
    for kind, field in (("character", "character_image_prompts"), ("prop", "prop_image_prompts"), ("scene", "scene_image_prompts")):
        raw = state.get(field, {})
        for model_family in ("zimage", "flux2"):
            item = raw.get(model_family, {}) if isinstance(raw, dict) else {}
            if isinstance(item, str):
                item = {"positive_prompt": item}
            default_id, node, width, height = profile[model_family]
            variants.append(ImagePromptVariant(
                model_family=model_family,
                kind=kind,
                positive_prompt=str(item.get("positive_prompt") or state.get({"character": "character_design", "prop": "prop_design", "scene": "background_design"}[kind], "")),
                negative_prompt=str(item.get("negative_prompt", "")),
                profile_id=str(item.get("profile_id", default_id)),
                prompt_node_id=str(item.get("prompt_node_id", node)),
                width=int(item.get("width", width) or width),
                height=int(item.get("height", height) or height),
                profile_status=str(item.get("profile_status", "candidate")),
                instructions=tuple(item.get("instructions", ())),
            ))
    return tuple(variants)


def render_image_prompt_markdown(result: GenerationResult, kind: str) -> str:
    """渲染一个资产类型的 Z-Image/Flux.2 可复制提示词。"""
    rows = [item for item in result.image_prompts if item.kind == kind]
    if not rows:
        # 旧 generation 没有双模型字段时，仍返回旧正文。
        return result.artifact(kind).content
    labels = {"character": "人物", "prop": "道具", "scene": "场景"}
    parts = [f"# {labels[kind]}生图提示词", "", "请选择对应模型版本复制。", ""]
    for item in rows:
        title = "Z-Image" if item.model_family == "zimage" else "Flux.2"
        parts.extend([
            f"## {title}", "",
            f"- Workflow Profile: `{item.profile_id}`（状态：{item.profile_status}）",
            f"- 主提示词节点：`{item.prompt_node_id}`",
            f"- 推荐尺寸：`{item.width} × {item.height}`", "",
            "### Positive Prompt", "", item.positive_prompt, "",
        ])
        if item.negative_prompt:
            parts.extend(["### Negative Prompt", "", item.negative_prompt, ""])
        if item.instructions:
            parts.extend(["### 操作说明", "", *[f"- {x}" for x in item.instructions], ""])
    return "\n".join(parts).rstrip() + "\n"


def render_fl2va_frame_markdown(result: GenerationResult, frame: str) -> str:
    """渲染首帧或尾帧的双模型可复制提示词。"""
    if result.fl2va_prompt_bundle is None:
        raise KeyError("生成结果中不存在 FL2VA 首尾帧提示词")
    if frame not in {"first", "last"}:
        raise ValueError(f"FL2VA 帧类型无效：{frame}")
    bundle = result.fl2va_prompt_bundle
    rows = bundle.first if frame == "first" else bundle.last
    title = "首帧" if frame == "first" else "尾帧"
    variant = str(result.variant).upper()
    if not rows:
        return f"# {variant} {title}生图提示词\n\n（{variant} 变体未生成{title}提示词）\n"
    parts = [
        f"# {variant} {title}生图提示词", "",
        "人物、道具和场景已经融合在同一张关键帧画面中。请选择对应模型版本复制。", "",
        f"- 场景锚点：{bundle.scene_anchor}",
        f"- {variant} 视频 Profile：`{bundle.profile_id}`",
        f"- 视频提示词节点：`{bundle.prompt_node_id}`", "",
    ]
    slot_id = bundle.first_frame_slot_id if frame == "first" else bundle.last_frame_slot_id
    for item in rows:
        model_title = "Z-Image" if item.model_family == "zimage" else "Flux.2"
        parts.extend([
            f"## {model_title}", "",
            f"- 静态图 Workflow Profile：`{item.profile_id}`（状态：{item.profile_status}）",
            f"- 主提示词节点：`{item.prompt_node_id}`",
            f"- 推荐尺寸：`{item.width} × {item.height}`",
            f"- {variant} 输入槽：`{slot_id}`", "",
            "### Positive Prompt", "", item.positive_prompt, "",
        ])
        if item.negative_prompt:
            parts.extend(["### Negative Prompt", "", item.negative_prompt, ""])
        if item.instructions:
            parts.extend(["### 操作说明", "", *[f"- {x}" for x in item.instructions], ""])
    return "\n".join(parts).rstrip() + "\n"


def render_fl2va_markdown(result: GenerationResult) -> str:
    """渲染关键帧生图提示词与连续性约束摘要（FL2VA 保持原输出不变）。"""
    if result.fl2va_prompt_bundle is None:
        raise KeyError("生成结果中不存在 FL2VA 首尾帧提示词")
    bundle = result.fl2va_prompt_bundle
    variant = str(result.variant).upper()
    if variant == "FL2VA":
        parts = [
            "# FL2VA 融合首尾帧提示词", "",
            "首帧和尾帧均把人物、道具和场景融合在同一画面中；视频提示词描述两帧之间的连续变化。", "",
            f"- 场景锚点：{bundle.scene_anchor}",
            f"- 视频 Profile：`{bundle.profile_id}`",
            f"- 视频提示词节点：`{bundle.prompt_node_id}`（输入：`{bundle.prompt_input}`）",
            f"- 首帧输入槽：`{bundle.first_frame_slot_id}`",
            f"- 尾帧输入槽：`{bundle.last_frame_slot_id}`", "",
            "## 连续性约束", "",
            *[f"- {item}" for item in bundle.continuity_constraints], "",
            "## 文件", "",
            "- 首帧：`first-frame-prompt.md`",
            "- 尾帧：`last-frame-prompt.md`",
            "- 视频剧情提示词：`video-prompt.md`", "",
        ]
        return "\n".join(parts).rstrip() + "\n"
    intro = {
        "I2VA": "首帧把人物、道具和场景融合在同一画面中；视频提示词从该首帧画面出发向前发展。",
        "L2VA": "尾帧把人物、道具和场景融合在同一画面中；视频提示词从合理开场逐步收敛到该尾帧画面。",
    }[variant]
    parts = [
        f"# {variant} 关键帧提示词", "",
        intro, "",
        f"- 场景锚点：{bundle.scene_anchor}",
        f"- 视频 Profile：`{bundle.profile_id}`",
        f"- 视频提示词节点：`{bundle.prompt_node_id}`（输入：`{bundle.prompt_input}`）",
    ]
    if bundle.first:
        parts.append(f"- 首帧输入槽：`{bundle.first_frame_slot_id}`")
    if bundle.last:
        parts.append(f"- 尾帧输入槽：`{bundle.last_frame_slot_id}`")
    parts.extend(["", "## 连续性约束", "", *[f"- {item}" for item in bundle.continuity_constraints], "", "## 文件", ""])
    if bundle.first:
        parts.append("- 首帧：`first-frame-prompt.md`")
    if bundle.last:
        parts.append("- 尾帧：`last-frame-prompt.md`")
    parts.append("- 视频剧情提示词：`video-prompt.md`")
    parts.append("")
    return "\n".join(parts).rstrip() + "\n"
def render_generation(result: GenerationResult) -> dict[str, str]:
    """返回当前生成模式的可复制产物；FL2VA 只输出融合首尾帧。"""
    if result.fl2va_prompt_bundle is not None:
        return {
            "script": result.script,
            "video": result.artifact("video").content,
            "fl2va": render_fl2va_markdown(result),
            "first-frame": render_fl2va_frame_markdown(result, "first"),
            "last-frame": render_fl2va_frame_markdown(result, "last"),
        }
    return {
        "script": result.script,
        "video": result.artifact("video").content,
        "character": render_image_prompt_markdown(result, "character"),
        "prop": render_image_prompt_markdown(result, "prop"),
        "scene": render_image_prompt_markdown(result, "scene"),
    }


__all__ = [
    "GenerationResult", "PromptArtifact", "PromptKind", "ImagePromptVariant",
    "FL2VAFramePrompt", "FL2VAPromptBundle", "brief_snapshot",
    "fl2va_bundle_from_dict", "generation_directory", "render_generation",
    "render_fl2va_frame_markdown", "render_fl2va_markdown", "result_from_state",
    "scene_requirement_groups", "validate_fl2va_bundle",
]
