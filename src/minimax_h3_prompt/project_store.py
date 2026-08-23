"""长视频项目文档的离线存储与确定性校验。

本模块只读写 Assets 主题目录中的项目文档，不调用 LLM、ComfyUI，也不猜测
output 归属。项目文档是后续资产设计、镜头计划和任务包的稳定边界。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .asset_store import AssetStore
from .project_models import AssetRegistry, ProjectBible, ShotPlan

_PROJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


@dataclass(frozen=True)
class ProjectDocument:
    """一个项目目录及其三个结构化文档。"""

    topic_id: str
    project_id: str
    directory: Path
    bible: ProjectBible
    registry: AssetRegistry
    shot_plan: ShotPlan

    @property
    def project_path(self) -> Path:
        return self.directory / "project.json"

    @property
    def bible_path(self) -> Path:
        return self.directory / "bible.json"

    @property
    def registry_path(self) -> Path:
        return self.directory / "asset-registry.json"

    @property
    def shot_plan_path(self) -> Path:
        return self.directory / "shot-plan.json"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "project_document.v1",
            "topic_id": self.topic_id,
            "project_id": self.project_id,
            "bible": self.bible.to_dict(),
            "asset_registry": self.registry.to_dict(),
            "shot_plan": self.shot_plan.to_dict(),
        }


@dataclass(frozen=True)
class ProjectIssue:
    code: str
    message: str
    severity: str = "error"

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "severity": self.severity}


def _validate_project_id(project_id: str) -> None:
    if not isinstance(project_id, str) or not _PROJECT_ID.fullmatch(project_id):
        raise ValueError(f"project_id 格式无效：{project_id}")


def _safe_project_path(root: Path, topic_id: str, project_id: str) -> Path:
    candidate = (root / topic_id / "projects" / project_id).resolve()
    topic_root = (root / topic_id).resolve()
    try:
        candidate.relative_to(topic_root)
    except ValueError as exc:
        raise ValueError(f"项目路径越出主题目录：{candidate}") from exc
    return candidate


def _write_json(path: Path, value: Any, *, overwrite: bool = False) -> None:
    text = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if path.exists() and not overwrite:
        old = path.read_text(encoding="utf-8")
        if old != text:
            raise FileExistsError(f"目标已存在且内容不同：{path}")
        return
    path.write_text(text, encoding="utf-8")


class ProjectStore:
    """在 ``Assets/<topic>/projects/<project>`` 管理项目文档。"""

    def __init__(self, root: str | Path = r"D:\笔记\Assets") -> None:
        self.asset_store = AssetStore(root)
        self.root = self.asset_store.root

    def project_directory(self, topic_id: str, project_id: str) -> Path:
        self.asset_store.topic_root(topic_id)
        _validate_project_id(project_id)
        return _safe_project_path(self.root, topic_id, project_id)

    def init_project(
        self,
        topic_id: str,
        project_id: str,
        title: str,
        *,
        duration_seconds: float = 60.0,
        variant: str = "FL2VA",
        global_style: str = "",
        overwrite: bool = False,
    ) -> ProjectDocument:
        """创建空项目；不会创建任何虚构资产或生成结果。"""
        if not title.strip():
            raise ValueError("项目标题不能为空")
        if duration_seconds <= 0:
            raise ValueError("项目时长必须大于 0")
        topic_root = self.asset_store.ensure_topic(topic_id)
        directory = self.project_directory(topic_id, project_id)
        if directory.exists() and any(directory.iterdir()) and not overwrite:
            raise FileExistsError(f"项目目录已存在且非空：{directory}")
        directory.mkdir(parents=True, exist_ok=True)

        bible = ProjectBible(
            project_id=project_id,
            topic_id=topic_id,
            title=title.strip(),
            global_style=global_style.strip(),
        )
        registry = AssetRegistry(topic_id=topic_id, project_id=project_id)
        shot_plan = ShotPlan(
            shot_plan_id=f"{project_id}-shots",
            project_id=project_id,
            variant=variant.upper(),
            duration_seconds=float(duration_seconds),
        )
        document = ProjectDocument(topic_id, project_id, directory, bible, registry, shot_plan)
        _write_json(document.bible_path, bible.to_dict(), overwrite=overwrite)
        _write_json(document.registry_path, registry.to_dict(), overwrite=overwrite)
        _write_json(document.shot_plan_path, shot_plan.to_dict(), overwrite=overwrite)
        _write_json(
            document.project_path,
            {
                "schema_version": "project_document.v1",
                "topic_id": topic_id,
                "project_id": project_id,
                "title": title.strip(),
                "duration_seconds": float(duration_seconds),
                "variant": variant.upper(),
                "status": "draft",
            },
            overwrite=overwrite,
        )
        # 仅用于让用户知道各文件用途，不包含密钥或运行结果。
        readme = directory / "README.md"
        if not readme.exists() or overwrite:
            readme.write_text(
                f"# {title.strip()}\n\n"
                f"- topic_id: `{topic_id}`\n- project_id: `{project_id}`\n\n"
                "编辑 bible.json、asset-registry.json 和 shot-plan.json 后运行项目校验。\n"
                "本目录只保存提示词、任务包和追溯元数据，不自动运行 ComfyUI。\n",
                encoding="utf-8",
            )
        return document

    def save_project(
        self,
        document: ProjectDocument,
        *,
        overwrite: bool = False,
    ) -> ProjectDocument:
        """显式保存完整项目文档；不调用外部服务，也不推进审核状态。"""
        _validate_project_id(document.project_id)
        expected_directory = self.project_directory(document.topic_id, document.project_id).resolve()
        if document.directory.resolve() != expected_directory:
            raise ValueError("ProjectDocument.directory 与项目身份不一致")
        if document.bible.topic_id != document.topic_id or document.bible.project_id != document.project_id:
            raise ValueError("ProjectBible 的 topic_id/project_id 与项目不一致")
        if document.registry.topic_id != document.topic_id or document.registry.project_id != document.project_id:
            raise ValueError("AssetRegistry 的 topic_id/project_id 与项目不一致")
        if document.shot_plan.project_id != document.project_id:
            raise ValueError("ShotPlan 的 project_id 与项目不一致")
        shot_errors = document.shot_plan.validate()
        if shot_errors:
            raise ValueError("ShotPlan 不能保存：" + "; ".join(shot_errors))

        document.directory.mkdir(parents=True, exist_ok=True)
        _write_json(document.bible_path, document.bible.to_dict(), overwrite=overwrite)
        _write_json(document.registry_path, document.registry.to_dict(), overwrite=overwrite)
        _write_json(document.shot_plan_path, document.shot_plan.to_dict(), overwrite=overwrite)
        _write_json(
            document.project_path,
            {
                "schema_version": "project_document.v1",
                "topic_id": document.topic_id,
                "project_id": document.project_id,
                "title": document.bible.title,
                "duration_seconds": document.shot_plan.duration_seconds,
                "variant": document.shot_plan.variant,
                "status": "draft",
            },
            overwrite=overwrite,
        )
        return document

    def update_bible(
        self,
        topic_id: str,
        project_id: str,
        bible: ProjectBible,
        *,
        overwrite: bool = False,
    ) -> ProjectDocument:
        """替换 Bible，其他项目文档保持不变。"""
        current = self.load_project(topic_id, project_id)
        updated = ProjectDocument(topic_id, project_id, current.directory, bible, current.registry, current.shot_plan)
        return self.save_project(updated, overwrite=overwrite)

    def update_registry(
        self,
        topic_id: str,
        project_id: str,
        registry: AssetRegistry,
        *,
        overwrite: bool = False,
    ) -> ProjectDocument:
        """替换 AssetRegistry，其他项目文档保持不变。"""
        current = self.load_project(topic_id, project_id)
        updated = ProjectDocument(topic_id, project_id, current.directory, current.bible, registry, current.shot_plan)
        return self.save_project(updated, overwrite=overwrite)

    def update_shot_plan(
        self,
        topic_id: str,
        project_id: str,
        shot_plan: ShotPlan,
        *,
        overwrite: bool = False,
    ) -> ProjectDocument:
        """替换 ShotPlan，其他项目文档保持不变。"""
        current = self.load_project(topic_id, project_id)
        updated = ProjectDocument(topic_id, project_id, current.directory, current.bible, current.registry, shot_plan)
        return self.save_project(updated, overwrite=overwrite)

    def load_project(self, topic_id: str, project_id: str) -> ProjectDocument:
        directory = self.project_directory(topic_id, project_id)
        if not directory.is_dir():
            raise FileNotFoundError(f"项目不存在：{directory}")
        required = ("bible.json", "asset-registry.json", "shot-plan.json")
        missing = [name for name in required if not (directory / name).is_file()]
        if missing:
            raise ValueError(f"项目文档缺失：{', '.join(missing)}")
        bible = ProjectBible.from_dict(_read_mapping(directory / "bible.json"))
        registry = AssetRegistry.from_dict(_read_mapping(directory / "asset-registry.json"))
        shot_plan = ShotPlan.from_dict(_read_mapping(directory / "shot-plan.json"))
        if bible.topic_id != topic_id or bible.project_id != project_id:
            raise ValueError("ProjectBible 的 topic_id/project_id 与目录不一致")
        if registry.topic_id != topic_id or registry.project_id != project_id:
            raise ValueError("AssetRegistry 的 topic_id/project_id 与目录不一致")
        if shot_plan.project_id != project_id:
            raise ValueError("ShotPlan 的 project_id 与目录不一致")
        return ProjectDocument(topic_id, project_id, directory, bible, registry, shot_plan)

    def validate(self, topic_id: str, project_id: str) -> list[ProjectIssue]:
        """返回可展示的结构/引用/审核问题，不执行任何外部动作。"""
        try:
            document = self.load_project(topic_id, project_id)
        except (OSError, ValueError) as exc:
            return [ProjectIssue("PROJECT_DOCUMENT_INVALID", str(exc))]

        issues: list[ProjectIssue] = []
        for error in document.bible.validate_references(document.registry):
            issues.append(ProjectIssue(error.split(":", 1)[0], error))
        for error in document.shot_plan.validate():
            issues.append(ProjectIssue(error.split(":", 1)[0], error))
        for profile in document.bible.workflow_profiles:
            if profile.status != "approved":
                issues.append(ProjectIssue(
                    "PROFILE_NOT_APPROVED",
                    f"Workflow Profile {profile.profile_id} 当前为 {profile.status}，不能视为人工验收通过",
                    "warning",
                ))
        for asset in document.registry.assets:
            if asset.status != "approved":
                issues.append(ProjectIssue(
                    "ASSET_NOT_APPROVED",
                    f"资产 {asset.asset_id} 当前为 {asset.status}",
                    "warning",
                ))
        if document.shot_plan.status != "locked":
            issues.append(ProjectIssue("SHOT_PLAN_NOT_LOCKED", "ShotPlan 尚未锁定", "warning"))
        return issues

    def show(self, topic_id: str, project_id: str) -> dict[str, Any]:
        """返回不含任何秘密的项目摘要。"""
        document = self.load_project(topic_id, project_id)
        return {
            "topic_id": document.topic_id,
            "project_id": document.project_id,
            "directory": str(document.directory),
            "title": document.bible.title,
            "duration_seconds": document.shot_plan.duration_seconds,
            "variant": document.shot_plan.variant,
            "asset_count": len(document.registry.assets),
            "shot_count": len(document.shot_plan.shots),
            "status": {
                "bible": document.bible.status,
                "asset_registry": [asset.status for asset in document.registry.assets],
                "shot_plan": document.shot_plan.status,
            },
        }


def _read_mapping(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"文档顶层必须是对象：{path}")
    return raw


__all__ = ["ProjectDocument", "ProjectIssue", "ProjectStore"]
