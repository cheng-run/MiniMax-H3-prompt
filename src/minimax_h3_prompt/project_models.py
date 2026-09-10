"""长视频项目、资产与镜头计划的结构化模型。

这些模型只描述项目和可追溯任务，不执行 ComfyUI，也不改写原始工作流或输出。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

AssetStatus = Literal["planned", "candidate", "approved", "rejected"]
ReviewSeverity = Literal["info", "warning", "error"]
ReviewKind = Literal["static", "manual", "visual", "audio"]
SegmentPipelineState = Literal["proposed", "approved", "executed", "reviewed"]

_ASSET_ID = re.compile(r"^[CSP E]\d{2}$".replace(" ", ""))
_SHOT_ID = re.compile(r"^SH\d{3}$")
_VERSION_ID = re.compile(r"^(?:[A-Z]+\d{2}|SH\d{3})-v\d{3}$")
_GENERATION_ID = re.compile(r"^(?:[A-Z]+\d{2}|SH\d{3})-G\d{3}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SEGMENT_ID = re.compile(r"^SEG\d{2}-SH\d{3}[a-z]$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _jsonable(value: Any) -> Any:
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value


def _require_sha256(value: str, label: str) -> None:
    if value and not _SHA256.fullmatch(value):
        raise ValueError(f"{label} 必须是 64 位小写 SHA-256")


def _require_nonempty(value: str, label: str) -> None:
    if not value.strip():
        raise ValueError(f"{label} 不能为空")


@dataclass(frozen=True)
class ReviewRecord:
    """一个可追溯的静态、人工或媒体验收记录。"""

    kind: ReviewKind
    severity: ReviewSeverity
    code: str
    message: str
    entity_id: str = ""
    field_name: str = ""
    reviewer: str = ""
    reviewed_at: str = ""
    evidence: tuple[str, ...] = field(default_factory=tuple)
    outcome: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ReviewRecord:
        return cls(
            kind=raw.get("kind", "manual"), severity=raw.get("severity", "info"),
            code=str(raw.get("code", "")), message=str(raw.get("message", "")),
            entity_id=str(raw.get("entity_id", "")), field_name=str(raw.get("field", "")),
            reviewer=str(raw.get("reviewer", "")), reviewed_at=str(raw.get("reviewed_at", "")),
            evidence=tuple(str(x) for x in raw.get("evidence", [])),
            outcome=str(raw.get("outcome", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self.__dict__)


@dataclass(frozen=True)
class AssetReference:
    asset_id: str
    role: str = ""
    version: str = ""
    required: bool = True

    def __post_init__(self) -> None:
        if not _ASSET_ID.fullmatch(self.asset_id):
            raise ValueError(f"资产 ID 格式无效：{self.asset_id}")

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> AssetReference:
        return cls(str(raw.get("asset_id", "")), str(raw.get("role", "")), str(raw.get("version", "")), bool(raw.get("required", True)))

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self.__dict__)


@dataclass(frozen=True)
class AssetRecord:
    asset_id: str
    asset_type: str
    name: str
    description: str = ""
    version: str = ""
    generation_id: str = ""
    prompt_sha256: str = ""
    input_asset_ids: tuple[str, ...] = field(default_factory=tuple)
    workflow_profile_id: str = ""
    workflow_profile_version: str = ""
    workflow_sha256: str = ""
    source_path: str = ""
    target_path: str = ""
    sha256: str = ""
    status: AssetStatus = "planned"
    review_records: tuple[ReviewRecord, ...] = field(default_factory=tuple)
    derived_from_asset_id: str = ""

    def __post_init__(self) -> None:
        if not _ASSET_ID.fullmatch(self.asset_id):
            raise ValueError(f"资产 ID 格式无效：{self.asset_id}")
        if self.version and not _VERSION_ID.fullmatch(self.version):
            raise ValueError(f"资产版本格式无效：{self.version}")
        if self.generation_id and not _GENERATION_ID.fullmatch(self.generation_id):
            raise ValueError(f"generation_id 格式无效：{self.generation_id}")
        _require_sha256(self.prompt_sha256, "prompt_sha256")
        _require_sha256(self.workflow_sha256, "workflow_sha256")
        _require_sha256(self.sha256, "sha256")
        for asset_id in self.input_asset_ids:
            if not _ASSET_ID.fullmatch(asset_id):
                raise ValueError(f"输入资产 ID 格式无效：{asset_id}")

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> AssetRecord:
        data = dict(raw)
        data["input_asset_ids"] = tuple(str(x) for x in data.get("input_asset_ids", []))
        data["review_records"] = tuple(ReviewRecord.from_dict(x) for x in data.get("review_records", []))
        return cls(**{key: data[key] for key in cls.__dataclass_fields__ if key in data})

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self.__dict__)

    def with_status(self, status: AssetStatus, review: ReviewRecord | None = None) -> AssetRecord:
        allowed = {
            "planned": {"candidate", "rejected"},
            "candidate": {"approved", "rejected"},
            "rejected": {"candidate"},
            "approved": {"rejected"},
        }
        if status != self.status and status not in allowed[self.status]:
            raise ValueError(f"不允许的资产状态转换：{self.status} → {status}")
        records = self.review_records + ((review,) if review else ())
        if status == "approved":
            if not any(r.kind in ("visual", "audio") and r.outcome == "approved" for r in records):
                raise ValueError("资产只有在视觉/听觉验收记录 outcome=approved 后才能 approved")
        return AssetRecord(**{**self.to_dict(), "review_records": records, "status": status})


@dataclass(frozen=True)
class AssetRegistry:
    topic_id: str
    project_id: str
    assets: tuple[AssetRecord, ...] = field(default_factory=tuple)
    schema_version: str = "asset_registry.v1"

    def __post_init__(self) -> None:
        _require_nonempty(self.topic_id, "topic_id")
        _require_nonempty(self.project_id, "project_id")
        ids = [asset.asset_id for asset in self.assets]
        if len(ids) != len(set(ids)):
            raise ValueError("AssetRegistry 不允许重复 asset_id；修订必须创建新版本记录")

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> AssetRegistry:
        return cls(
            topic_id=str(raw.get("topic_id", "")), project_id=str(raw.get("project_id", "")),
            assets=tuple(AssetRecord.from_dict(x) for x in raw.get("assets", [])),
            schema_version=str(raw.get("schema_version", "asset_registry.v1")),
        )

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self.__dict__)

    def add(self, asset: AssetRecord) -> AssetRegistry:
        if any(item.asset_id == asset.asset_id for item in self.assets):
            raise ValueError(f"资产 {asset.asset_id} 已存在，禁止覆盖；请创建新版本/派生资产")
        return AssetRegistry(self.topic_id, self.project_id, self.assets + (asset,), self.schema_version)

    def get(self, asset_id: str) -> AssetRecord | None:
        return next((asset for asset in self.assets if asset.asset_id == asset_id), None)


@dataclass(frozen=True)
class ProfileReference:
    profile_id: str
    profile_version: str
    workflow_sha256: str
    status: str = "candidate"

    def __post_init__(self) -> None:
        _require_nonempty(self.profile_id, "profile_id")
        _require_sha256(self.workflow_sha256, "workflow_sha256")
        if self.status == "approved":
            raise ValueError("项目设计不能把未经人工验收的 Workflow Profile 伪装为 approved")

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ProfileReference:
        return cls(str(raw.get("profile_id", "")), str(raw.get("profile_version", "")), str(raw.get("workflow_sha256", "")), str(raw.get("status", "candidate")))

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self.__dict__)


@dataclass(frozen=True)
class ProjectBible:
    project_id: str
    topic_id: str
    title: str
    global_style: str = ""
    global_constraints: tuple[str, ...] = field(default_factory=tuple)
    characters: tuple[AssetReference, ...] = field(default_factory=tuple)
    locations: tuple[AssetReference, ...] = field(default_factory=tuple)
    props: tuple[AssetReference, ...] = field(default_factory=tuple)
    asset_ids: tuple[str, ...] = field(default_factory=tuple)
    workflow_profiles: tuple[ProfileReference, ...] = field(default_factory=tuple)
    review_records: tuple[ReviewRecord, ...] = field(default_factory=tuple)
    status: str = "draft"
    created_at: str = ""
    updated_at: str = ""
    schema_version: str = "project_bible.v1"

    def __post_init__(self) -> None:
        _require_nonempty(self.project_id, "project_id")
        _require_nonempty(self.topic_id, "topic_id")
        _require_nonempty(self.title, "title")
        for asset_id in self.asset_ids:
            if not _ASSET_ID.fullmatch(asset_id):
                raise ValueError(f"资产 ID 格式无效：{asset_id}")

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ProjectBible:
        data = dict(raw)
        for key in ("characters", "locations", "props"):
            data[key] = tuple(AssetReference.from_dict(x) for x in data.get(key, []))
        data["workflow_profiles"] = tuple(ProfileReference.from_dict(x) for x in data.get("workflow_profiles", []))
        data["review_records"] = tuple(ReviewRecord.from_dict(x) for x in data.get("review_records", []))
        for key in ("global_constraints", "asset_ids"):
            data[key] = tuple(str(x) for x in data.get(key, []))
        return cls(**{key: data[key] for key in cls.__dataclass_fields__ if key in data})

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self.__dict__)

    def validate_references(self, registry: AssetRegistry) -> list[str]:
        errors = []
        for asset_id in self.asset_ids + tuple(x.asset_id for x in self.characters + self.locations + self.props):
            if registry.get(asset_id) is None:
                errors.append(f"ASSET_MISSING: {asset_id}")
        return errors


@dataclass(frozen=True)
class Segment:
    """实际执行单元：一次 3-8s 的 Workflow 生成。

    `Shot` 是叙事单元（纸飞机飞过麦田），`Segment` 是运行为单元
    （单次 H3/FL2VA 生成）。当镜头时长超过 8s 时按 `split_shot_into_segments`
    拆分为连续段，段间通过桥接帧（bridge frame）保证连续性。
    """

    segment_id: str
    shot_ref: str
    duration_seconds: float
    prev_segment_id: str = ""
    next_segment_id: str = ""
    start_frame_asset_id: str = ""      # 空 = 无需帧输入（T2VA）
    end_frame_asset_id: str = ""        # 空 = 无帧输入；执行后由 extract-bridge 填充
    workflow_profile_id: str = ""
    workflow_profile_version: str = ""
    workflow_sha256: str = ""
    task_package_path: str = ""
    pipeline_state: SegmentPipelineState = "proposed"
    review_records: tuple[ReviewRecord, ...] = field(default_factory=tuple)
    start_state_derived_from: str = ""  # 上一段的段 ID 或来源说明

    def __post_init__(self) -> None:
        if not _SEGMENT_ID.fullmatch(self.segment_id):
            raise ValueError(
                f"Segment ID 格式无效：{self.segment_id}（应为 SEG<shot_number>-SH<id><letter>）"
            )
        if not _SHOT_ID.fullmatch(self.shot_ref):
            raise ValueError(f"Segment shot_ref 格式无效：{self.shot_ref}")
        if self.duration_seconds <= 0:
            raise ValueError("Segment 时长必须大于 0")
        for key in ("prev_segment_id", "next_segment_id"):
            other = getattr(self, key)
            if other and not _SEGMENT_ID.fullmatch(other):
                raise ValueError(f"{key} 格式无效：{other}")
        for asset_id in (self.start_frame_asset_id, self.end_frame_asset_id):
            if asset_id and not _ASSET_ID.fullmatch(asset_id):
                raise ValueError(f"桥接帧资产 ID 格式无效：{asset_id}")

    @property
    def shot_number(self) -> int:
        return int(_SHOT_ID.fullmatch(self.shot_ref).group(0)[2:])

    @property
    def sequence_letter(self) -> str:
        """该段在其镜头内的序号字母（a/b/c…）。"""
        return self.segment_id.rsplit("-", 1)[1][-1]

    @property
    def is_first_in_shot(self) -> bool:
        return self.sequence_letter == "a"

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Segment:
        data = dict(raw)
        data["review_records"] = tuple(ReviewRecord.from_dict(x) for x in data.get("review_records", []))
        return cls(**{key: data[key] for key in cls.__dataclass_fields__ if key in data})

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self.__dict__)

    def with_status(self, state: SegmentPipelineState, review: ReviewRecord | None = None) -> Segment:
        allowed = {
            "proposed": {"approved"},
            "approved": {"executed", "rejected"},
            "executed": {"reviewed"},
            "reviewed": {"approved"},  # 回看/重制
        }
        if state != self.pipeline_state and state not in allowed[self.pipeline_state]:
            raise ValueError(f"不允许的段状态转换：{self.pipeline_state} → {state}")
        records = self.review_records + ((review,) if review else ())
        return Segment(**{**self.to_dict(), "pipeline_state": state, "review_records": records})


def split_shot_into_segments(
    shot: Shot,
    *,
    max_segment_seconds: float = 8.0,
    min_segment_seconds: float = 3.0,
) -> tuple[Segment, ...]:
    """把单个 `Shot` 拆成连续执行段。

    - 时长 ≤ max 的镜头返回一个段（`a`）。
    - 时长超限的镜头按 `max_segment_seconds` 切分，末段至少 `min_segment_seconds`；
      若末段会小于 min，则把最后一个完整段减短并入它。
    - 段间前后指针自动接续；首段 prev_segment_id 为空。
    """
    if shot.duration_seconds <= max_segment_seconds:
        return (
            Segment(
                segment_id=_segment_id_for(shot, "a"),
                shot_ref=shot.shot_id,
                duration_seconds=shot.duration_seconds,
                workflow_profile_id=shot.workflow_profile_id,
                workflow_profile_version=shot.workflow_profile_version,
                workflow_sha256=shot.workflow_sha256,
                pipeline_state="proposed",
            ),
        )

    count = int(math.ceil(shot.duration_seconds / max_segment_seconds))
    base = shot.duration_seconds / count
    # 修正末段过短：把最后一个完整段的部分时长并入末段，保证末段 ≥ min
    if base * (count - 1) < min_segment_seconds and count > 1:
        count -= 1
        base = shot.duration_seconds / count
    remaining = shot.duration_seconds
    segments: list[Segment] = []
    for index in range(count):
        duration = max(0.0, min(base, remaining))
        remaining -= duration
        letter = chr(ord("a") + index)
        segment = Segment(
            segment_id=_segment_id_for(shot, letter),
            shot_ref=shot.shot_id,
            duration_seconds=round(duration, 3),
            workflow_profile_id=shot.workflow_profile_id,
            workflow_profile_version=shot.workflow_profile_version,
            workflow_sha256=shot.workflow_sha256,
            pipeline_state="proposed",
        )
        if segments:
            segments[-1] = Segment(**{**segments[-1].to_dict(), "next_segment_id": segment.segment_id})
            segment = Segment(**{**segment.to_dict(), "prev_segment_id": segments[-1].segment_id})
        segments.append(segment)
    return tuple(segments)


def _segment_id_for(shot: Shot, letter: str) -> str:
    return f"SEG{shot.shot_number:02d}-{shot.shot_id}{letter}"


@dataclass(frozen=True)
class Shot:
    shot_id: str
    shot_number: int
    duration_seconds: float
    start_state: str
    action: str
    end_state: str
    subject_asset_ids: tuple[str, ...] = field(default_factory=tuple)
    input_asset_ids: tuple[str, ...] = field(default_factory=tuple)
    workflow_profile_id: str = ""
    workflow_profile_version: str = ""
    workflow_sha256: str = ""
    generation_id: str = ""
    task_package_path: str = ""
    previous_shot_id: str = ""
    start_state_derived_from: str = ""
    continuity_constraints: tuple[str, ...] = field(default_factory=tuple)
    status: str = "planned"
    locked: bool = False
    review_records: tuple[ReviewRecord, ...] = field(default_factory=tuple)
    # 执行段（3-8s/段），由 split_shot_into_segments() 生成；为空表示未拆分
    segments: tuple[Segment, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not _SHOT_ID.fullmatch(self.shot_id):
            raise ValueError(f"镜头 ID 格式无效：{self.shot_id}")
        if self.shot_number < 1 or self.duration_seconds <= 0:
            raise ValueError("镜头编号必须为正数，时长必须大于 0")
        for asset_id in self.subject_asset_ids + self.input_asset_ids:
            if not _ASSET_ID.fullmatch(asset_id):
                raise ValueError(f"镜头中的资产 ID 格式无效：{asset_id}")
        if self.generation_id and not _GENERATION_ID.fullmatch(self.generation_id):
            raise ValueError(f"generation_id 格式无效：{self.generation_id}")
        _require_sha256(self.workflow_sha256, "workflow_sha256")
        if self.locked and self.status != "approved":
            raise ValueError("只有 approved 镜头才能锁定")

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Shot:
        data = dict(raw)
        for key in ("subject_asset_ids", "input_asset_ids", "continuity_constraints"):
            data[key] = tuple(str(x) for x in data.get(key, []))
        data["review_records"] = tuple(ReviewRecord.from_dict(x) for x in data.get("review_records", []))
        data["segments"] = tuple(Segment.from_dict(x) for x in data.get("segments", []))
        return cls(**{key: data[key] for key in cls.__dataclass_fields__ if key in data})

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self.__dict__)

    def with_segments(self, segments: tuple[Segment, ...]) -> Shot:
        """返回携带拆分执行段的新 Shot（复用 frozen 结构，替换 segments 字段）。"""
        return Shot(**{**self.to_dict(), "segments": segments})

    def validate(self, previous: Shot | None = None) -> list[str]:
        errors: list[str] = []
        if previous is not None:
            if self.shot_number != previous.shot_number + 1:
                errors.append("SHOT_NUMBER_SEQUENCE: 镜头编号不连续")
            if self.previous_shot_id != previous.shot_id:
                errors.append("PREVIOUS_SHOT_MISMATCH: previous_shot_id 不匹配")
            if self.start_state_derived_from != previous.shot_id:
                errors.append("START_STATE_NOT_DERIVED: Start State 必须声明由上一镜 End State 派生")
        elif self.previous_shot_id or self.start_state_derived_from:
            errors.append("FIRST_SHOT_HAS_PREDECESSOR: 第一镜不能声明上一镜")
        if not self.start_state.strip() or not self.action.strip() or not self.end_state.strip():
            errors.append("SHOT_STATE_INCOMPLETE: Start State、Action、End State 均不能为空")
        if self.locked and self.status != "approved":
            errors.append("SHOT_LOCK_REQUIRES_APPROVAL: 未通过审核的镜头不能锁定")
        return errors


@dataclass(frozen=True)
class ShotPlan:
    shot_plan_id: str
    project_id: str
    variant: str
    duration_seconds: float
    shots: tuple[Shot, ...] = field(default_factory=tuple)
    status: str = "draft"
    review_records: tuple[ReviewRecord, ...] = field(default_factory=tuple)
    schema_version: str = "shot_plan.v1"

    def __post_init__(self) -> None:
        _require_nonempty(self.shot_plan_id, "shot_plan_id")
        _require_nonempty(self.project_id, "project_id")
        if self.duration_seconds <= 0:
            raise ValueError("ShotPlan 时长必须大于 0")

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ShotPlan:
        return cls(
            shot_plan_id=str(raw.get("shot_plan_id", "")), project_id=str(raw.get("project_id", "")),
            variant=str(raw.get("variant", "")), duration_seconds=float(raw.get("duration_seconds", 0)),
            shots=tuple(Shot.from_dict(x) for x in raw.get("shots", [])),
            status=str(raw.get("status", "draft")),
            review_records=tuple(ReviewRecord.from_dict(x) for x in raw.get("review_records", [])),
            schema_version=str(raw.get("schema_version", "shot_plan.v1")),
        )

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self.__dict__)

    def validate(self) -> list[str]:
        errors: list[str] = []
        shot_ids = [shot.shot_id for shot in self.shots]
        duplicate_ids = sorted({shot_id for shot_id in shot_ids if shot_ids.count(shot_id) > 1})
        for shot_id in duplicate_ids:
            errors.append(f"DUPLICATE_SHOT_ID: 镜头 ID 重复：{shot_id}")
        shot_numbers = [shot.shot_number for shot in self.shots]
        duplicate_numbers = sorted({number for number in shot_numbers if shot_numbers.count(number) > 1})
        for number in duplicate_numbers:
            errors.append(f"DUPLICATE_SHOT_NUMBER: 镜头编号重复：{number}")
        for index, shot in enumerate(self.shots):
            expected_number = index + 1
            if shot.shot_number != expected_number:
                errors.append(
                    f"SHOT_NUMBER_ORDER: {shot.shot_id} 的镜头编号应为 {expected_number}，实际为 {shot.shot_number}"
                )
            previous = self.shots[index - 1] if index else None
            errors.extend(f"{shot.shot_id}: {error}" for error in shot.validate(previous))
        if sum(shot.duration_seconds for shot in self.shots) > self.duration_seconds + 1e-9:
            errors.append("SHOT_DURATION_EXCEEDS_PLAN: 镜头总时长超过 ShotPlan 时长")
        return errors

    def lock(self) -> ShotPlan:
        errors = self.validate()
        if errors:
            raise ValueError("ShotPlan 不能锁定：" + "; ".join(errors))
        if any(shot.status != "approved" for shot in self.shots):
            raise ValueError("所有镜头都必须 approved 后才能锁定 ShotPlan")
        locked_shots = tuple(Shot(**{**shot.to_dict(), "locked": True}) for shot in self.shots)
        return ShotPlan(self.shot_plan_id, self.project_id, self.variant, self.duration_seconds, locked_shots, "locked", self.review_records, self.schema_version)

    def all_segments(self) -> tuple[Segment, ...]:
        """按镜头顺序展开整个 ShotPlan 的所有执行段。"""
        return tuple(segment for shot in self.shots for segment in shot.segments)

    def with_shot_segments(self, shot_id: str, segments: tuple[Segment, ...]) -> ShotPlan:
        """把某个镜头的执行段写回 ShotPlan（返回新不可变实例）。"""
        shots = tuple(
            shot.with_segments(segments) if shot.shot_id == shot_id else shot
            for shot in self.shots
        )
        return ShotPlan(self.shot_plan_id, self.project_id, self.variant, self.duration_seconds, shots, self.status, self.review_records, self.schema_version)


def verify_chain(segments: tuple[Segment, ...]) -> list[str]:
    """跨段连续性检查。

    校验规则：
    - 首段不得声明 prev_segment_id；
    - 后续段必须声明 prev_segment_id 且指向前一段；
    - 前一段的 next_segment_id 必须指向后一段（双向一致）；
    - 后续段的 start_frame_asset_id（若非空）必须等于上一段的 end_frame_asset_id。
    """
    errors: list[str] = []
    for index, segment in enumerate(segments):
        if index == 0:
            if segment.prev_segment_id:
                errors.append(f"{segment.segment_id}: FIRST_SEGMENT_HAS_PREDECESSOR 首段不能声明上一段")
            continue
        previous = segments[index - 1]
        if not segment.prev_segment_id:
            errors.append(f"{segment.segment_id}: MISSING_PREV_SEGMENT 未声明上一段")
        elif segment.prev_segment_id != previous.segment_id:
            errors.append(f"{segment.segment_id}: PREV_SEGMENT_MISMATCH 指向前一段为 {segment.prev_segment_id}，实际应为 {previous.segment_id}")
        if previous.next_segment_id != segment.segment_id:
            errors.append(f"{previous.segment_id}: NEXT_SEGMENT_MISMATCH next_segment_id 应为 {segment.segment_id}")
        # 桥接帧哈希/资产 ID 衔接：若下一段声明首帧，必须等于上一段尾帧
        if segment.start_frame_asset_id and previous.end_frame_asset_id:
            if segment.start_frame_asset_id != previous.end_frame_asset_id:
                errors.append(
                    f"{segment.segment_id}: BRIDGE_FRAME_MISMATCH 首帧资产 {segment.start_frame_asset_id} 与上一段尾帧 {previous.end_frame_asset_id} 不一致"
                )
    return errors


def prompt_sha256(prompt: str) -> str:
    """统一生成任务包/资产记录使用的 Prompt SHA-256。"""
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def dumps_document(document: Any) -> str:
    return json.dumps(_jsonable(document), ensure_ascii=False, indent=2)
