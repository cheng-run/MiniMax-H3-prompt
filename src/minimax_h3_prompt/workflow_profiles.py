"""ComfyUI Workflow Profile 模型与只读静态核验工具。

本模块只读取工作流 JSON，不提交、修改或改写 ComfyUI 工作流。
``verified`` 仅表示静态结构已核验；实际运行和人工验收由外部流程记录。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

ProfileStatus = Literal["candidate", "verified", "approved", "disabled"]
EvidenceLevel = Literal[
    "format_e2e", "static_verified", "runtime_pending", "visual_approved"
]


def _tuple(value: Any) -> tuple[Any, ...]:
    if value is None:
        return ()
    return tuple(value) if isinstance(value, (list, tuple)) else (value,)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


@dataclass(frozen=True)
class WorkflowSlot:
    """一个输入槽的静态契约；semantic 不应凭节点位置猜测。"""

    slot_id: str
    node_id: str
    input_name: str
    semantic: str = ""
    data_type: str = ""
    required: bool = False
    order: int | None = None
    confirmed: bool = False
    connection: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> WorkflowSlot:
        return cls(
            slot_id=str(raw.get("slot_id", "")),
            node_id=str(raw.get("node_id", "")),
            input_name=str(raw.get("input_name", "")),
            semantic=str(raw.get("semantic", "")),
            data_type=str(raw.get("data_type", "")),
            required=bool(raw.get("required", False)),
            order=int(raw["order"]) if raw.get("order") is not None else None,
            confirmed=bool(raw.get("confirmed", False)),
            connection=str(raw.get("connection", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return _jsonable({
            "slot_id": self.slot_id,
            "node_id": self.node_id,
            "input_name": self.input_name,
            "semantic": self.semantic,
            "data_type": self.data_type,
            "required": self.required,
            "order": self.order,
            "confirmed": self.confirmed,
            "connection": self.connection,
        })


@dataclass(frozen=True)
class WorkflowOutput:
    node_id: str
    node_type: str
    output_type: str = ""
    prefix: str = ""
    format: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> WorkflowOutput:
        return cls(
            node_id=str(raw.get("node_id", "")),
            node_type=str(raw.get("node_type", "")),
            output_type=str(raw.get("output_type", "")),
            prefix=str(raw.get("prefix", "")),
            format=str(raw.get("format", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return _jsonable({
            "node_id": self.node_id,
            "node_type": self.node_type,
            "output_type": self.output_type,
            "prefix": self.prefix,
            "format": self.format,
        })


@dataclass(frozen=True)
class WorkflowProfile:
    """一个可追溯的工作流引用及其静态契约。"""

    profile_id: str
    display_name: str
    purpose: str
    model_family: str
    input_mode: str
    workflow_path: str
    workflow_sha256: str = ""
    profile_version: str = "1"
    schema_version: str = "1"
    status: ProfileStatus = "candidate"
    evidence_level: EvidenceLevel = "runtime_pending"
    workflow_format: str = ""
    prompt_node_id: str = ""
    prompt_node_type: str = ""
    prompt_input: str = ""
    generation_node_id: str = ""
    generation_node_type: str = ""
    slots: tuple[WorkflowSlot, ...] = field(default_factory=tuple)
    outputs: tuple[WorkflowOutput, ...] = field(default_factory=tuple)
    model_files: tuple[str, ...] = field(default_factory=tuple)
    custom_nodes: tuple[str, ...] = field(default_factory=tuple)
    defaults: dict[str, Any] = field(default_factory=dict)
    manual_steps: tuple[str, ...] = field(default_factory=tuple)
    risks: tuple[str, ...] = field(default_factory=tuple)
    verification_records: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    parent_profile_id: str = ""
    absolute_workflow_path: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> WorkflowProfile:
        return cls(
            profile_id=str(raw.get("profile_id", "")),
            display_name=str(raw.get("display_name", "")),
            purpose=str(raw.get("purpose", "")),
            model_family=str(raw.get("model_family", "")),
            input_mode=str(raw.get("input_mode", "")),
            workflow_path=str(raw.get("workflow_path", "")),
            workflow_sha256=str(raw.get("workflow_sha256", "")),
            profile_version=str(raw.get("profile_version", "1")),
            schema_version=str(raw.get("schema_version", "1")),
            status=raw.get("status", "candidate"),
            evidence_level=raw.get("evidence_level", "runtime_pending"),
            workflow_format=str(raw.get("workflow_format", "")),
            prompt_node_id=str(raw.get("prompt_node_id", "")),
            prompt_node_type=str(raw.get("prompt_node_type", "")),
            prompt_input=str(raw.get("prompt_input", "")),
            generation_node_id=str(raw.get("generation_node_id", "")),
            generation_node_type=str(raw.get("generation_node_type", "")),
            slots=tuple(WorkflowSlot.from_dict(x) for x in raw.get("slots", [])),
            outputs=tuple(WorkflowOutput.from_dict(x) for x in raw.get("outputs", [])),
            model_files=_tuple(raw.get("model_files")),
            custom_nodes=_tuple(raw.get("custom_nodes")),
            defaults=dict(raw.get("defaults", {}) or {}),
            manual_steps=_tuple(raw.get("manual_steps")),
            risks=_tuple(raw.get("risks")),
            verification_records=tuple(dict(x) for x in raw.get("verification_records", [])),
            parent_profile_id=str(raw.get("parent_profile_id", "")),
            absolute_workflow_path=str(raw.get("absolute_workflow_path", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return _jsonable({
            "profile_id": self.profile_id,
            "display_name": self.display_name,
            "purpose": self.purpose,
            "model_family": self.model_family,
            "input_mode": self.input_mode,
            "workflow_path": self.workflow_path,
            "workflow_sha256": self.workflow_sha256,
            "profile_version": self.profile_version,
            "schema_version": self.schema_version,
            "status": self.status,
            "evidence_level": self.evidence_level,
            "workflow_format": self.workflow_format,
            "prompt_node_id": self.prompt_node_id,
            "prompt_node_type": self.prompt_node_type,
            "prompt_input": self.prompt_input,
            "generation_node_id": self.generation_node_id,
            "generation_node_type": self.generation_node_type,
            "slots": [x.to_dict() for x in self.slots],
            "outputs": [x.to_dict() for x in self.outputs],
            "model_files": self.model_files,
            "custom_nodes": self.custom_nodes,
            "defaults": self.defaults,
            "manual_steps": self.manual_steps,
            "risks": self.risks,
            "verification_records": self.verification_records,
            "parent_profile_id": self.parent_profile_id,
            "absolute_workflow_path": self.absolute_workflow_path,
        })

    def with_status(self, status: ProfileStatus, evidence_level: EvidenceLevel | None = None) -> WorkflowProfile:
        """返回新 Profile；不允许静态方法伪造 approved。"""
        if status == "approved" and evidence_level != "visual_approved":
            raise ValueError("只有 visual_approved 证据才能将 Profile 标记为 approved")
        if status == "verified" and evidence_level not in (None, "static_verified"):
            raise ValueError("verified 必须对应 static_verified 证据")
        data = self.to_dict()
        data.update({
            "slots": self.slots,
            "outputs": self.outputs,
            "status": status,
            "evidence_level": evidence_level or self.evidence_level,
            "verification_records": self.verification_records,
        })
        return WorkflowProfile(**data)


def sha256_file(path: str | Path) -> str:
    """按原始字节计算 SHA-256，不重新序列化 JSON。"""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class WorkflowNode:
    node_id: str
    node_type: str
    title: str = ""
    inputs: tuple[str, ...] = field(default_factory=tuple)
    outputs: tuple[str, ...] = field(default_factory=tuple)
    widgets: tuple[Any, ...] = field(default_factory=tuple)
    class_type: str = ""

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self.__dict__)


@dataclass(frozen=True)
class WorkflowInspection:
    path: str
    sha256: str
    workflow_format: str
    nodes: tuple[WorkflowNode, ...]
    links: tuple[Any, ...] = field(default_factory=tuple)
    errors: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return _jsonable({
            "path": self.path, "sha256": self.sha256,
            "workflow_format": self.workflow_format,
            "nodes": [n.to_dict() for n in self.nodes],
            "links": self.links, "errors": self.errors,
        })

    def node(self, node_id: str) -> WorkflowNode | None:
        return next((n for n in self.nodes if n.node_id == str(node_id)), None)


def _is_api_workflow(raw: Any) -> bool:
    return bool(isinstance(raw, dict) and raw and all(
        isinstance(value, dict) and "class_type" in value and "inputs" in value
        for value in raw.values()
    ))


def _node_from_ui(raw: dict[str, Any]) -> WorkflowNode:
    inputs = tuple(str(x.get("name", "")) for x in raw.get("inputs", []) if isinstance(x, dict))
    outputs = tuple(str(x.get("name", "")) for x in raw.get("outputs", []) if isinstance(x, dict))
    props = raw.get("properties", {}) or {}
    title = str(props.get("Node name for S&R", props.get("title", "")))
    return WorkflowNode(
        node_id=str(raw.get("id", "")), node_type=str(raw.get("type", "")),
        title=title, inputs=inputs, outputs=outputs,
        widgets=_tuple(raw.get("widgets_values", [])),
    )


def inspect_workflow(path: str | Path) -> WorkflowInspection:
    """静态读取 UI/API 两种 ComfyUI JSON；不会执行节点。"""
    file_path = Path(path)
    errors: list[str] = []
    try:
        raw = json.loads(file_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return WorkflowInspection(str(file_path), sha256_file(file_path) if file_path.exists() else "", "unknown", (), (), (str(exc),))

    if _is_api_workflow(raw):
        nodes = tuple(
            WorkflowNode(
                node_id=str(node_id), node_type=str(value.get("class_type", "")),
                class_type=str(value.get("class_type", "")),
                inputs=tuple(str(k) for k in (value.get("inputs", {}) or {})),
            )
            for node_id, value in raw.items()
        )
        fmt = "api"
        links: tuple[Any, ...] = ()
    elif isinstance(raw, dict) and isinstance(raw.get("nodes"), list):
        nodes = tuple(_node_from_ui(x) for x in raw["nodes"] if isinstance(x, dict))
        links = tuple(raw.get("links", []) or [])
        fmt = "ui"
    else:
        errors.append("顶层结构不是 ComfyUI UI 或 API 工作流")
        nodes, links, fmt = (), (), "unknown"
    return WorkflowInspection(str(file_path), sha256_file(file_path), fmt, nodes, links, tuple(errors))


def validate_profile(profile: WorkflowProfile, inspection: WorkflowInspection) -> list[str]:
    """对 Profile 声明的静态契约进行检查，返回可展示的错误列表。"""
    errors = list(inspection.errors)
    if profile.workflow_sha256 and profile.workflow_sha256 != inspection.sha256:
        errors.append("WORKFLOW_HASH_MISMATCH: 工作流原始文件哈希已变化")
    if profile.workflow_format and profile.workflow_format != inspection.workflow_format:
        errors.append("WORKFLOW_FORMAT_MISMATCH: 工作流格式声明不一致")
    for node_id, label in ((profile.prompt_node_id, "prompt"), (profile.generation_node_id, "generation")):
        if node_id and inspection.node(node_id) is None:
            errors.append(f"NODE_MISSING: {label} 节点 {node_id} 不存在")
    prompt = inspection.node(profile.prompt_node_id) if profile.prompt_node_id else None
    if prompt and profile.prompt_input and profile.prompt_input not in prompt.inputs:
        errors.append(f"INPUT_MISSING: prompt 节点没有 input {profile.prompt_input}")
    generation = inspection.node(profile.generation_node_id) if profile.generation_node_id else None
    if generation and profile.generation_node_type and generation.node_type != profile.generation_node_type:
        errors.append("GENERATION_TYPE_MISMATCH: 核心生成节点类型不一致")
    for slot in profile.slots:
        node = inspection.node(slot.node_id)
        if node is None:
            errors.append(f"SLOT_NODE_MISSING: 输入槽 {slot.slot_id} 的节点不存在")
        elif slot.input_name and slot.input_name not in node.inputs:
            errors.append(f"SLOT_INPUT_MISSING: 输入槽 {slot.slot_id} 的 input 不存在")
    for output in profile.outputs:
        node = inspection.node(output.node_id)
        if node is None:
            errors.append(f"OUTPUT_NODE_MISSING: 输出节点 {output.node_id} 不存在")
        elif output.node_type and node.node_type != output.node_type:
            errors.append(f"OUTPUT_TYPE_MISMATCH: 输出节点 {output.node_id} 类型不一致")
    return errors


def dump_profile(profile: WorkflowProfile, path: str | Path, *, format: str | None = None) -> None:
    target = Path(path)
    fmt = (format or target.suffix.lstrip(".") or "json").lower()
    target.parent.mkdir(parents=True, exist_ok=True)
    if fmt in ("yaml", "yml"):
        target.write_text(yaml.safe_dump(profile.to_dict(), allow_unicode=True, sort_keys=False), encoding="utf-8")
    else:
        target.write_text(json.dumps(profile.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


def load_profile(path: str | Path) -> WorkflowProfile:
    target = Path(path)
    if target.suffix.lower() in (".yaml", ".yml"):
        raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    else:
        raw = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Profile 文件顶层必须是 mapping")
    return WorkflowProfile.from_dict(raw)
