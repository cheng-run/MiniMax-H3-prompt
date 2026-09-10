"""人工 ComfyUI 执行闭环编排层。

只读解析项目 / Profile / TaskPackage 生成人工执行指引，并受控落盘
执行回传、验收与 Profile 升级。本模块不自动运行 ComfyUI，不扫描
output 猜测归属，也不把任何结果伪装成正式资产。
"""
from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .project_models import (
    ReviewRecord,
    Segment,
    ShotPlan,
    split_shot_into_segments,
    verify_chain,
)
from .project_store import ProjectStore
from .task_package import TaskPackage, import_output, inspect_asset
from .workflow_profiles import (
    WorkflowProfile,
    dump_profile,
    inspect_workflow,
    load_profile,
    validate_profile,
)

PROFILES_DIR = Path(__file__).resolve().parents[2] / "docs" / "profiles"
WORKFLOW_ROOT = Path(r"D:\Comfyui\ComfyUI\user\default\workflows")

# verified 阶段可接受的已知静态风险：不阻塞升级，但保留在验证记录里。
# 非默认 mode（NODE_DISABLED_OR_SPECIAL_MODE）在工作流中常对应 bypass 分支，
# 是人工确认的静态风险而不是结构契约错误。
_NON_FATAL_VERIFIED_ERRORS = ("NODE_DISABLED_OR_SPECIAL_MODE",)


def resolve_profile_path(profile_id: str) -> Path:
    """Profile 的权威文件路径（docs/profiles/<id>.json，git 追踪）。"""
    return PROFILES_DIR / f"{profile_id}.json"


def _load_task_for_shot(store: ProjectStore, topic_id: str, project_id: str, shot: Any) -> TaskPackage:
    """按镜头定位并加载任务包；优先 task_package_path，回退 tasks/<generation>。"""
    if shot.task_package_path:
        return TaskPackage.load(shot.task_package_path)
    document = store.load_project(topic_id, project_id)
    return TaskPackage.load(document.directory / "tasks" / shot.generation_id)


# ---------------------------------------------------------------------------
# 帧图片入库：两阶段向导的关键帧复制与追溯
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def copy_frame_image(
    source: str | Path,
    *,
    topic_id: str,
    generation_id: str,
    role: str,
    assets_root: str | Path | None = None,
) -> dict[str, Any]:
    """把一张关键帧图片（first/last）显式复制进资产库 inbox 并写 source.json。

    只复制不移动、不修改 ComfyUI output 原始文件；同哈希副本已存在时跳过复制。
    返回 dict（含 source/target/sha256/width/height/role），供 session-state 追溯。
    """
    if role not in ("first", "last"):
        raise ValueError(f"帧角色必须是 first 或 last：{role}")
    src = Path(source).resolve()
    if not src.is_file():
        raise FileNotFoundError(src)
    info = inspect_asset(src)
    digest = str(info["sha256"])
    root = (Path(assets_root) if assets_root else store_root_default()) / "inbox" / topic_id / generation_id / "frames"
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{role}{src.suffix.lower()}"
    if not target.exists() or _file_sha256(target) != digest:
        shutil.copy2(src, target)
    record = {
        "role": role,
        "generation_id": generation_id,
        "source_path": str(src),
        "target_path": str(target),
        "sha256": digest,
        "size": int(info["size"]),
        "mime_type": str(info["mime_type"]),
        "width": info.get("width"),
        "height": info.get("height"),
        "copied_at": _now_iso(),
    }
    source_file = root / "source.json"
    existing: dict[str, Any] = {}
    if source_file.exists():
        try:
            existing = json.loads(source_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = {}
    frames = {item.get("role"): item for item in existing.get("frames", [])}
    frames[role] = record
    merged = {"schema_version": "frame-import.v1", "topic_id": topic_id,
              "generation_id": generation_id, "frames": list(frames.values())}
    source_file.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    return record


def store_root_default() -> Path:
    """Assets 根目录默认值（与 ProjectStore 一致）。"""
    return Path(r"D:\笔记\Assets")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# project plan：生成人工执行指引操作卡
# ---------------------------------------------------------------------------

def build_execution_card(
    store: ProjectStore,
    topic_id: str,
    project_id: str,
    shot_id: str,
    *,
    workflow_root: Path = WORKFLOW_ROOT,
) -> dict[str, Any]:
    """只读组装一个镜头的人工执行操作卡，不落盘。"""
    from .project_context import ProjectContext

    document = store.load_project(topic_id, project_id)
    shot = next((s for s in document.shot_plan.shots if s.shot_id == shot_id), None)
    if shot is None:
        raise ValueError(f"镜头不存在：{shot_id}")
    if not shot.generation_id:
        raise ValueError(f"镜头 {shot_id} 没有 generation_id，无法生成执行卡")
    task = _load_task_for_shot(store, topic_id, project_id, shot)
    profile_path = resolve_profile_path(task.profile_id)
    if not profile_path.is_file():
        raise FileNotFoundError(f"Profile 文件不存在：{profile_path}")
    task_dir = _task_dir_for_shot(store, topic_id, project_id, shot)
    # 身份/哈希一致性校验；error 级问题直接拒绝生成执行卡
    ProjectContext.load(
        store=store, topic_id=topic_id, project_id=project_id,
        profile_path=profile_path, task_package_path=task_dir,
    )
    profile = load_profile(profile_path)
    workflow_path = (Path(workflow_root) / profile.workflow_path).resolve()

    blockers = [
        {
            "code": "INPUT_PLACEHOLDER",
            "message": f"输入资产 {item.asset_id} 无真实文件（{item.path}）",
            "suggest": "首轮可不传首尾帧（T2VA 式）验证工作流可运行；正式镜头需先提供真实输入图",
        }
        for item in task.inputs
        if item.path.startswith("planned://")
    ]
    return {
        "topic_id": topic_id,
        "project_id": project_id,
        "shot_id": shot_id,
        "generation_id": shot.generation_id,
        "prompt": task.prompt,
        "workflow_path": str(workflow_path),
        "workflow_sha256": task.workflow_sha256,
        "profile": {
            "id": profile.profile_id,
            "status": profile.status,
            "evidence_level": profile.evidence_level,
        },
        "prompt_node": {
            "node_id": profile.prompt_node_id,
            "input": profile.prompt_input,
            "action": "粘贴下方提示词全文",
        },
        "input_slots": [
            {
                "slot_id": slot.slot_id,
                "node_id": slot.node_id,
                "required": slot.required,
                "semantic": slot.semantic,
                "connection": slot.connection,
            }
            for slot in profile.slots
        ],
        "generation_node": {
            "node_id": profile.generation_node_id,
            "node_type": profile.generation_node_type,
        },
        "output_node": {
            "node_id": profile.outputs[0].node_id if profile.outputs else "",
            "node_type": profile.outputs[0].node_type if profile.outputs else "",
        },
        "expected": {
            "output_type": task.expected_output_type,
            "width": task.expected_width,
            "height": task.expected_height,
            "frames": task.expected_frames,
            "fps": task.expected_fps,
        },
        "output_prefix": task.output_prefix,
        "manual_steps": list(task.manual_steps),
        "notes": list(profile.risks),
        "blockers": blockers,
    }


def _task_dir_for_shot(store: ProjectStore, topic_id: str, project_id: str, shot: Any) -> Path:
    """按镜头返回任务包目录（task_package_path 或 tasks/<generation>）。"""
    if shot.task_package_path:
        return Path(shot.task_package_path)
    document = store.load_project(topic_id, project_id)
    return document.directory / "tasks" / shot.generation_id


def render_card(card: dict[str, Any]) -> str:
    """把操作卡渲染成可照做的 Markdown 指引。"""
    lines: list[str] = []
    lines.append(f"# {card['shot_id']} 人工执行指引（{card['generation_id']}）")
    lines.append("")
    lines.append(f"- 主题: `{card['topic_id']}` / 项目: `{card['project_id']}`")
    lines.append(f"- 工作流: `{card['workflow_path']}`")
    lines.append(f"- 工作流 SHA-256: `{card['workflow_sha256']}`")
    profile = card["profile"]
    lines.append(f"- Profile: `{profile['id']}`（{profile['status']}/{profile['evidence_level']}）")
    expected = [f"{key}={value}" for key, value in card["expected"].items() if value not in (None, "")]
    if expected:
        lines.append(f"- 预期输出: {', '.join(expected)}")
    if card["output_prefix"]:
        lines.append(f"- 输出前缀: `{card['output_prefix']}`")
    lines.append("")
    prompt_node = card["prompt_node"]
    lines.append(f"## 提示词（粘贴到节点 {prompt_node['node_id']} 的 {prompt_node['input']}）")
    lines.append("")
    lines.append("```")
    lines.append(card["prompt"])
    lines.append("```")
    if card["input_slots"]:
        lines.append("")
        lines.append("## 输入槽")
        for slot in card["input_slots"]:
            required = "必填" if slot["required"] else "可选"
            lines.append(f"- `{slot['slot_id']}` → 节点 {slot['node_id']}（{required}）：{slot['semantic']}")
    lines.append("")
    lines.append("## 操作步骤")
    for index, step in enumerate(card["manual_steps"], 1):
        lines.append(f"{index}. {step}")
    generation = card["generation_node"]
    output = card["output_node"]
    lines.append("")
    lines.append("## 输出节点")
    lines.append(f"- 生成节点: {generation['node_id']}（{generation['node_type']}）")
    lines.append(f"- 输出节点: {output['node_id']}（{output['node_type']}）")
    if card["notes"]:
        lines.append("")
        lines.append("## 注意事项")
        lines.extend(f"- {note}" for note in card["notes"])
    if card["blockers"]:
        lines.append("")
        lines.append("## [!] 输入缺口")
        for blocker in card["blockers"]:
            lines.append(f"- [{blocker['code']}] {blocker['message']}")
            lines.append(f"  - 建议：{blocker['suggest']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# project import-output：接线 TaskPackage.import_output
# ---------------------------------------------------------------------------

def import_generation(
    store: ProjectStore,
    topic_id: str,
    project_id: str,
    generation_id: str,
    source: str | Path,
    *,
    inbox_root: Path | None = None,
) -> dict[str, Any]:
    """把人工 ComfyUI 执行输出导入 inbox，并把任务标记为 executed。"""
    document = store.load_project(topic_id, project_id)
    shot = next((s for s in document.shot_plan.shots if s.generation_id == generation_id), None)
    if shot is None:
        raise ValueError(f"没有镜头关联 generation_id：{generation_id}")
    task_dir = _task_dir_for_shot(store, topic_id, project_id, shot)
    task = TaskPackage.load(task_dir)
    source_path = Path(source).resolve()
    if not source_path.is_file():
        raise FileNotFoundError(source_path)

    info = inspect_asset(source_path)
    digest = str(info["sha256"])
    destination = (inbox_root or (store.asset_store.root / "inbox")) / topic_id / generation_id / digest[:12]
    imported = import_output(source_path, task, destination)

    # ① 标记 executed 并写回任务包
    executed = task.with_status("executed")
    executed.write(task_dir)

    # ② 给对应镜头追加 OUTPUT_IMPORTED 记录
    record = ReviewRecord(
        kind="manual", severity="info", code="OUTPUT_IMPORTED",
        message=f"人工 ComfyUI 执行输出已导入 {imported.target_path}",
        entity_id=generation_id,
        evidence=(imported.source_path, imported.target_path, imported.sha256),
        outcome="imported",
    )
    updated_shot = replace(shot, review_records=shot.review_records + (record,))
    updated_plan = replace(
        document.shot_plan,
        shots=tuple(updated_shot if s.shot_id == shot.shot_id else s for s in document.shot_plan.shots),
    )
    store.update_shot_plan(topic_id, project_id, updated_plan, overwrite=True)

    return {
        "generation_id": generation_id,
        "shot_id": shot.shot_id,
        "task_status": executed.status,
        "asset": imported.to_dict(),
    }


# ---------------------------------------------------------------------------
# project review：人工视觉/听觉验收
# ---------------------------------------------------------------------------

def apply_review(
    store: ProjectStore,
    topic_id: str,
    project_id: str,
    entity_id: str,
    kind: str,
    outcome: str,
    *,
    reviewer: str = "",
) -> dict[str, Any]:
    """人工验收资产或镜头；approved 需要 visual/audio 验收记录。"""
    if kind not in ("visual", "audio"):
        raise ValueError("验收 kind 必须是 visual 或 audio")
    if outcome not in ("approved", "rejected"):
        raise ValueError("验收 outcome 必须是 approved 或 rejected")
    document = store.load_project(topic_id, project_id)
    review = ReviewRecord(
        kind=kind, severity="info",
        code=f"{kind.upper()}_ACCEPTED" if outcome == "approved" else f"{kind.upper()}_REJECTED",
        message=f"人工{'视觉' if kind == 'visual' else '听觉'}验收：{outcome}",
        entity_id=entity_id, reviewer=reviewer, outcome=outcome,
    )

    if entity_id.startswith("SH"):  # 镜头流
        shot = next((s for s in document.shot_plan.shots if s.shot_id == entity_id), None)
        if shot is None:
            raise ValueError(f"镜头不存在：{entity_id}")
        new_status = "approved" if outcome == "approved" else shot.status
        updated_shot = replace(shot, status=new_status, review_records=shot.review_records + (review,))
        updated_plan = replace(
            document.shot_plan,
            shots=tuple(updated_shot if s.shot_id == shot.shot_id else s for s in document.shot_plan.shots),
        )
        store.update_shot_plan(topic_id, project_id, updated_plan, overwrite=True)
        return {"entity_type": "shot", "entity_id": entity_id, "status": updated_shot.status}

    if entity_id.startswith("SEG"):  # 执行段流：proposed→approved→executed→reviewed
        shot, segment = _find_segment(document, entity_id)
        if outcome == "approved" and segment.pipeline_state in ("proposed", "executed"):
            target_state = "approved" if segment.pipeline_state == "proposed" else "reviewed"
        elif outcome == "rejected" and segment.pipeline_state == "approved":
            target_state = "rejected"
        else:
            raise ValueError(
                f"段 {entity_id} 当前为 {segment.pipeline_state}，不能标记 {outcome}；"
                "请先完成前一步骤（plan / import-output / extract-bridge）"
            )
        updated_segment = segment.with_status(target_state, review)
        new_segments = tuple(
            updated_segment if item.segment_id == segment.segment_id else item
            for item in shot.segments
        )
        updated_plan = document.shot_plan.with_shot_segments(shot.shot_id, new_segments)
        store.update_shot_plan(topic_id, project_id, updated_plan, overwrite=True)
        return {"entity_type": "segment", "entity_id": entity_id, "status": updated_segment.pipeline_state}

    # 资产流
    asset = document.registry.get(entity_id)
    if asset is None:
        raise ValueError(f"资产不存在：{entity_id}")
    current = asset
    if outcome == "approved" and current.status == "planned":
        current = current.with_status("candidate")
    updated = current.with_status(outcome, review)
    updated_registry = replace(
        document.registry,
        assets=tuple(updated if item.asset_id == entity_id else item for item in document.registry.assets),
    )
    store.update_registry(topic_id, project_id, updated_registry, overwrite=True)
    return {"entity_type": "asset", "entity_id": entity_id, "status": updated.status}


# ---------------------------------------------------------------------------
# project verify：一键闸门检查
# ---------------------------------------------------------------------------

def _issue_dict(issue: Any) -> dict[str, str]:
    return {"code": issue.code, "message": issue.message, "severity": issue.severity}


def check_executability(
    store: ProjectStore,
    topic_id: str,
    project_id: str,
    *,
    shot_id: str | None = None,
    workflow_root: Path = WORKFLOW_ROOT,
) -> dict[str, Any]:
    """离线检查项目文档与每个镜头的可执行资格。"""
    from .project_context import ProjectContext

    document = store.load_project(topic_id, project_id)
    shots = [s for s in document.shot_plan.shots if shot_id is None or s.shot_id == shot_id]
    if not shots:
        raise ValueError(f"镜头不存在：{shot_id}")

    shot_reports: list[dict[str, Any]] = []
    for shot in shots:
        if not shot.generation_id:
            shot_reports.append({
                "shot_id": shot.shot_id, "can_execute": False,
                "issues": [{"code": "NO_GENERATION_ID", "message": "镜头没有 generation_id", "severity": "error"}],
                "input_gaps": [],
            })
            continue
        try:
            task = _load_task_for_shot(store, topic_id, project_id, shot)
            profile_path = resolve_profile_path(task.profile_id)
            if not profile_path.is_file():
                raise FileNotFoundError(f"Profile 文件不存在：{profile_path}")
            context = ProjectContext.load(
                store=store, topic_id=topic_id, project_id=project_id,
                profile_path=profile_path, task_package_path=_task_dir_for_shot(store, topic_id, project_id, shot),
            )
            issues = [_issue_dict(issue) for issue in context.validate()]
            can_execute = context.can_execute
        except Exception as exc:  # noqa: BLE001 - 收集式检查，逐镜头报告
            issues = [{"code": "CONTEXT_INVALID", "message": str(exc), "severity": "error"}]
            can_execute = False
        input_gaps = [
            {"code": "INPUT_FILE_MISSING", "asset_id": item.asset_id, "path": item.path}
            for item in task.inputs
            if item.path.startswith("planned://")
        ]
        shot_reports.append({
            "shot_id": shot.shot_id,
            "can_execute": can_execute,
            "issues": issues,
            "input_gaps": input_gaps,
        })

    return {
        "topic_id": topic_id,
        "project_id": project_id,
        "document_issues": [_issue_dict(issue) for issue in store.validate(topic_id, project_id)],
        "shots": shot_reports,
    }


# ---------------------------------------------------------------------------
# project profile：Profile 状态升级
# ---------------------------------------------------------------------------

def _collect_execution_evidence(
    store: ProjectStore,
    topic_id: str,
    project_id: str,
    profile_id: str,
) -> dict[str, Any]:
    """收集 Profile approved 的证据：executed 任务 + visual/audio 验收记录。"""
    document = store.load_project(topic_id, project_id)
    executed_task = False
    executed_generation = ""
    for shot in document.shot_plan.shots:
        if not shot.generation_id:
            continue
        try:
            task = _load_task_for_shot(store, topic_id, project_id, shot)
        except Exception:  # noqa: BLE001 - 任务包损坏不阻断证据收集
            continue
        if task.profile_id == profile_id and task.status == "executed":
            executed_task = True
            executed_generation = task.generation_id

    def has_visual_approval(records: Any) -> bool:
        return any(rec.outcome == "approved" and rec.kind in ("visual", "audio") for rec in records)

    visual_approved = any(
        has_visual_approval(shot.review_records) for shot in document.shot_plan.shots
    ) or any(
        has_visual_approval(asset.review_records) for asset in document.registry.assets
    )
    return {
        "executed_task": executed_task,
        "executed_generation": executed_generation,
        "visual_approved": visual_approved,
    }


def promote_profile(
    store: ProjectStore,
    topic_id: str,
    project_id: str,
    profile_id: str,
    status: str,
    *,
    reviewer: str = "",
    note: str = "",
    generation_id: str = "",
) -> dict[str, Any]:
    """Profile 状态升级：verified（静态核验）→ approved（真实执行 + 人工验收证据）。"""
    profile_path = resolve_profile_path(profile_id)
    if not profile_path.is_file():
        raise FileNotFoundError(f"Profile 文件不存在：{profile_path}")
    profile = load_profile(profile_path)

    if status == "verified":
        errors = validate_profile(profile, inspect_workflow(Path(WORKFLOW_ROOT) / profile.workflow_path))
        fatal = [error for error in errors if not error.startswith(_NON_FATAL_VERIFIED_ERRORS)]
        if fatal:
            raise ValueError("Profile 静态契约校验未通过，不能升级 verified：\n" + "\n".join(fatal))
        updated = profile.record_verification(
            {
                "type": "static_verified",
                "workflow_sha256": profile.workflow_sha256,
                "errors": errors,  # 保留全部错误，包括已知静态风险
                "reviewer": reviewer,
                "note": note,
            },
            status="verified",
            evidence_level="static_verified",
        )
    elif status == "approved":
        evidence = _collect_execution_evidence(store, topic_id, project_id, profile_id)
        if not evidence["executed_task"]:
            raise ValueError("缺少已 executed 的任务包：Profile approved 必须基于真实 ComfyUI 执行")
        if not evidence["visual_approved"]:
            raise ValueError("缺少视觉/听觉验收记录：Profile approved 必须基于人工验收 outcome=approved")
        updated = profile.record_verification(
            {
                "type": "visual_approved",
                "generation_id": generation_id or evidence["executed_generation"],
                "reviewer": reviewer,
                "outcome": "approved",
                "note": note,
            },
            status="approved",
            evidence_level="visual_approved",
        )
    else:
        raise ValueError(f"不支持的 Profile 升级状态：{status}（仅支持 verified/approved）")

    dump_profile(updated, profile_path)
    return {
        "profile_id": profile_id,
        "status": updated.status,
        "evidence_level": updated.evidence_level,
        "verification_records": [dict(record) for record in updated.verification_records],
    }


__all__ = [
    "PROFILES_DIR",
    "WORKFLOW_ROOT",
    "resolve_profile_path",
    "build_execution_card",
    "render_card",
    "import_generation",
    "apply_review",
    "check_executability",
    "promote_profile",
    "plan_segments",
    "extract_bridge_frame",
    "verify_segment_chain",
]


# ---------------------------------------------------------------------------
# 分段流水线：Shot（叙事单元）→ Segment（3-8s 实际执行单元）
# ---------------------------------------------------------------------------

def _link_segment_chain(segments: list[Segment]) -> tuple[Segment, ...]:
    """按列表顺序重建 prev/next 双向指针（跨镜头段链也在此接续）。"""
    linked: list[Segment] = []
    for index, segment in enumerate(segments):
        data = segment.to_dict()
        data["prev_segment_id"] = segments[index - 1].segment_id if index else ""
        data["next_segment_id"] = segments[index + 1].segment_id if index + 1 < len(segments) else ""
        linked.append(Segment.from_dict(data))
    return tuple(linked)


def _synthesize_shots_from_latest_prompt(document: Any) -> list[Any]:
    """向导路径的项目 ShotPlan 为空：从最新生成的视频提示词反推镜头表。"""
    from .segment_prompts import shots_from_prompt

    generations_dir = document.directory / "generations"
    candidates = sorted(
        generations_dir.glob("*/video-prompt.md"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    ) if generations_dir.is_dir() else []
    if not candidates:
        return []
    return list(shots_from_prompt(candidates[0].read_text(encoding="utf-8"), document.shot_plan.duration_seconds))


def plan_segments(
    store: ProjectStore,
    topic_id: str,
    project_id: str,
    *,
    max_segment_seconds: float | None = None,
    min_segment_seconds: float | None = None,
) -> dict[str, Any]:
    """把每个未拆分的 Shot 拆成执行段并写回 ShotPlan（幂等：已拆分镜头跳过）。

    段间 prev/next 指针按镜头顺序全局接续；桥接帧资产仍为空，待 extract-bridge 填充。
    """
    from .config import SEGMENT_CONFIG

    max_seconds = float(max_segment_seconds or SEGMENT_CONFIG["max_segment_seconds"])
    min_seconds = float(min_segment_seconds or SEGMENT_CONFIG["min_segment_seconds"])

    document = store.load_project(topic_id, project_id)
    plan = document.shot_plan
    synthesized_from_prompt = False
    if not plan.shots:
        shots = _synthesize_shots_from_latest_prompt(document)
        if not shots:
            raise ValueError(
                "该项目尚无镜头且找不到已生成的视频提示词："
                "请先运行向导（uv run launch.py）或 project generate-prompts 生成带分镜的提示词，再重试 segment-plan"
            )
        plan = ShotPlan(
            plan.shot_plan_id, plan.project_id, plan.variant, plan.duration_seconds, tuple(shots),
        )
        store.update_shot_plan(topic_id, project_id, plan, overwrite=True)
        synthesized_from_prompt = True
        document = store.load_project(topic_id, project_id)
        plan = document.shot_plan

    per_shot: list[tuple[Segment, ...]] = []
    split_shots: list[str] = []
    skipped_shots: list[str] = []
    for shot in plan.shots:
        if shot.segments:
            per_shot.append(shot.segments)
            skipped_shots.append(shot.shot_id)
            continue
        segments = split_shot_into_segments(
            shot, max_segment_seconds=max_seconds, min_segment_seconds=min_seconds,
        )
        per_shot.append(segments)
        split_shots.append(shot.shot_id)

    flat = list(_link_segment_chain([segment for segments in per_shot for segment in segments]))
    updated_plan = plan
    cursor = 0
    for shot, segments in zip(plan.shots, per_shot):
        updated_plan = updated_plan.with_shot_segments(
            shot.shot_id, tuple(flat[cursor:cursor + len(segments)]),
        )
        cursor += len(segments)
    store.update_shot_plan(topic_id, project_id, updated_plan, overwrite=True)

    warnings: list[str] = []
    warn_below = int(SEGMENT_CONFIG["warn_shot_count_below"])
    if plan.duration_seconds > 30 and len(plan.shots) < warn_below:
        warnings.append(
            f"SHOT_COUNT_LOW: {plan.duration_seconds:.0f}s 视频只有 {len(plan.shots)} 个镜头"
            f"（建议 ≥{warn_below}），可能导致内容密度不足"
        )
    return {
        "topic_id": topic_id,
        "project_id": project_id,
        "synthesized_from_prompt": synthesized_from_prompt,
        "split_shots": split_shots,
        "skipped_shots": skipped_shots,
        "segment_count": len(flat),
        "segments": [
            {"segment_id": s.segment_id, "shot_ref": s.shot_ref, "duration_seconds": s.duration_seconds}
            for s in flat
        ],
        "warnings": warnings,
    }


def _find_segment(document: Any, segment_id: str) -> tuple[Any, Segment]:
    """在 ShotPlan 中定位一个执行段，返回 (所属 Shot, Segment)。"""
    for shot in document.shot_plan.shots:
        for segment in shot.segments:
            if segment.segment_id == segment_id:
                return shot, segment
    raise ValueError(f"执行段不存在：{segment_id}（请先运行 project segment-plan）")


def _resolve_segment_video(store: ProjectStore, topic_id: str, shot: Any) -> Path:
    """缺省从该镜头 generation 的 inbox 目录取最新视频文件。"""
    if not shot.generation_id:
        raise ValueError(f"镜头 {shot.shot_id} 没有 generation_id，请用 --source 显式指定视频路径")
    inbox = store.asset_store.root / "inbox" / topic_id / shot.generation_id
    videos = [
        path for path in inbox.rglob("*")
        if path.suffix.lower() in (".mp4", ".mov", ".webm", ".mkv") and path.is_file()
    ] if inbox.is_dir() else []
    if not videos:
        raise FileNotFoundError(
            f"未找到镜头 {shot.shot_id} 的已导入视频（{inbox}）；"
            "请先 project import-output，或用 --source 显式指定视频路径"
        )
    return max(videos, key=lambda path: path.stat().st_mtime)


def extract_bridge_frame(
    store: ProjectStore,
    topic_id: str,
    project_id: str,
    segment_id: str,
    source: str | Path | None = None,
) -> dict[str, Any]:
    """剥出执行段输出视频的尾帧，登记为 bridge_frame 资产并接线到下一段首帧。

    只动 project 文档和 bridge_frames/ 目录；不改变段的人工验收状态，
    桥接帧资产永远以 planned 入库（不自动 approved）。
    """
    from .tools.frame_auditor import extract_last_frame

    document = store.load_project(topic_id, project_id)
    shot, segment = _find_segment(document, segment_id)
    video_path = Path(source).resolve() if source else _resolve_segment_video(store, topic_id, shot)

    frame_path = extract_last_frame(video_path, store.asset_store.bridge_frame_path(topic_id, project_id, segment_id))

    # 幂等：同一目标路径已登记过则复用原资产 ID，避免重复资产。
    existing = [a for a in document.registry.assets if a.target_path == str(frame_path)]
    if existing:
        record = existing[0]
    else:
        record = store.asset_store.register_bridge_frame(
            topic_id, project_id, segment_id, frame_path,
            existing_asset_ids=tuple(a.asset_id for a in document.registry.assets),
            source_video=video_path,
        )
        store.update_registry(
            topic_id, project_id, document.registry.add(record), overwrite=True,
        )

    # 接线：本段记 end_frame；下一段（若存在）记 start_frame + 派生来源。
    segment = Segment.from_dict({**segment.to_dict(), "end_frame_asset_id": record.asset_id})
    new_segments = []
    for item in shot.segments:
        if item.segment_id == segment.segment_id:
            new_segments.append(segment)
        elif segment.next_segment_id and item.segment_id == segment.next_segment_id:
            new_segments.append(Segment.from_dict({
                **item.to_dict(),
                "start_frame_asset_id": record.asset_id,
                "start_state_derived_from": segment.segment_id,
            }))
        else:
            new_segments.append(item)
    document = store.load_project(topic_id, project_id)  # registry 已更新，重载保持一致
    updated_plan = document.shot_plan.with_shot_segments(shot.shot_id, tuple(new_segments))
    store.update_shot_plan(topic_id, project_id, updated_plan, overwrite=True)

    return {
        "segment_id": segment_id,
        "shot_id": shot.shot_id,
        "video": str(video_path),
        "frame_path": str(frame_path),
        "asset": record.to_dict(),
        "next_segment_id": segment.next_segment_id,
        "next_segment_ready": bool(segment.next_segment_id),
    }


def verify_segment_chain(
    store: ProjectStore,
    topic_id: str,
    project_id: str,
    shot_id: str | None = None,
) -> dict[str, Any]:
    """跨段连续性检查：结构指针（verify_chain）+ 桥接帧资产完整性与哈希一致性。"""
    document = store.load_project(topic_id, project_id)
    shots = [s for s in document.shot_plan.shots if shot_id is None or s.shot_id == shot_id]
    if not shots:
        raise ValueError(f"镜头不存在：{shot_id}")
    segments = tuple(segment for shot in shots for segment in shot.segments)
    if not segments:
        raise ValueError(
            f"{'镜头 ' + shot_id if shot_id else '项目'}尚未拆分执行段：请先运行 project segment-plan"
        )

    errors = list(verify_chain(segments))
    for index, segment in enumerate(segments):
        if index == 0:
            continue
        previous = segments[index - 1]
        if not previous.end_frame_asset_id:
            errors.append(
                f"{previous.segment_id}: BRIDGE_FRAME_PENDING 尾帧未剥出"
                f"（先执行并 project extract-bridge {previous.segment_id}）"
            )
            continue
        if not segment.start_frame_asset_id:
            errors.append(f"{segment.segment_id}: BRIDGE_FRAME_MISSING 未声明首帧资产")
        previous_asset = document.registry.get(previous.end_frame_asset_id)
        current_asset = document.registry.get(segment.start_frame_asset_id) if segment.start_frame_asset_id else None
        if previous_asset is None:
            errors.append(f"{previous.segment_id}: BRIDGE_FRAME_MISSING 尾帧资产 {previous.end_frame_asset_id} 不在注册表")
        if current_asset is None and segment.start_frame_asset_id:
            errors.append(f"{segment.segment_id}: BRIDGE_FRAME_MISSING 首帧资产 {segment.start_frame_asset_id} 不在注册表")
        if previous_asset is not None and current_asset is not None:
            if previous_asset.sha256 != current_asset.sha256:
                errors.append(
                    f"{segment.segment_id}: BRIDGE_FRAME_HASH_MISMATCH "
                    f"首帧资产 {current_asset.asset_id} 与尾帧资产 {previous_asset.asset_id} 内容不一致"
                )
    return {
        "topic_id": topic_id,
        "project_id": project_id,
        "shot_id": shot_id,
        "segment_count": len(segments),
        "errors": errors,
        "chain_ok": not errors,
    }
