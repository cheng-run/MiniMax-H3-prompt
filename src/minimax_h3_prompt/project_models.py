"""长视频项目、资产与镜头计划的结构化模型。

这些模型只描述项目和可追溯任务，不执行 ComfyUI，也不改写原始工作流或输出。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

AssetStatus = Literal["planned", "candidate", "approved", "rejected"]
ReviewSeverity = Literal["info", "warning", "error"]
ReviewKind = Literal["static", "manual", "visual", "audio"]

_ASSET_ID = re.compile(r"^[CSP E]\d{2}$".replace(" ", ""))
_SHOT_ID = re.compile(r"^SH\d{3}$")
_VERSION_ID = re.compile(r"^(?:[A-Z]+\d{2}|SH\d{3})-v\d{3}$")
_GENERATION_ID = re.compile(r"^(?:[A-Z]+\d{2}|SH\d{3})-G\d{3}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


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
        return cls(**{key: data[key] for key in cls.__dataclass_fields__ if key in data})

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self.__dict__)

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


def prompt_sha256(prompt: str) -> str:
    """统一生成任务包/资产记录使用的 Prompt SHA-256。"""
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def dumps_document(document: Any) -> str:
    return json.dumps(_jsonable(document), ensure_ascii=False, indent=2)
