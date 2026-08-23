"""只读加载并校验项目、Workflow Profile 与 TaskPackage 的上下文。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .project_store import ProjectDocument, ProjectStore
from .task_package import TaskPackage
from .workflow_profiles import WorkflowProfile, load_profile


@dataclass(frozen=True)
class ContextIssue:
    code: str
    message: str
    severity: str = "error"


@dataclass(frozen=True)
class ProjectContext:
    """一个可规划的项目执行上下文；本对象不执行任何外部动作。"""

    project: ProjectDocument
    profile: WorkflowProfile
    task_package: TaskPackage

    @classmethod
    def load(
        cls,
        *,
        store: ProjectStore,
        topic_id: str,
        project_id: str,
        profile_path: str | Path,
        task_package_path: str | Path,
    ) -> ProjectContext:
        project = store.load_project(topic_id, project_id)
        profile = load_profile(profile_path)
        task_package = TaskPackage.load(task_package_path)
        context = cls(project, profile, task_package)
        errors = [issue for issue in context.validate() if issue.severity == "error"]
        if errors:
            detail = "; ".join(f"{issue.code}: {issue.message}" for issue in errors)
            raise ValueError(f"ProjectContext 校验失败：{detail}")
        return context

    @property
    def can_execute(self) -> bool:
        """只有人工视觉/听觉验收后的 approved Profile 才具备未来执行资格。"""
        return self.profile.status == "approved" and self.profile.evidence_level == "visual_approved"

    def validate(self) -> tuple[ContextIssue, ...]:
        project = self.project
        task = self.task_package
        profile = self.profile
        issues: list[ContextIssue] = []

        if task.topic_id != project.topic_id:
            issues.append(ContextIssue("TASK_TOPIC_MISMATCH", "TaskPackage.topic_id 与项目不一致"))
        if task.project_id != project.project_id:
            issues.append(ContextIssue("TASK_PROJECT_MISMATCH", "TaskPackage.project_id 与项目不一致"))
        if task.profile_id != profile.profile_id:
            issues.append(ContextIssue("PROFILE_ID_MISMATCH", "TaskPackage.profile_id 与 Profile 不一致"))
        if task.profile_version != profile.profile_version:
            issues.append(ContextIssue("PROFILE_VERSION_MISMATCH", "TaskPackage.profile_version 与 Profile 不一致"))
        if task.workflow_path != profile.workflow_path:
            issues.append(ContextIssue("WORKFLOW_PATH_MISMATCH", "TaskPackage.workflow_path 与 Profile 不一致"))
        if task.workflow_sha256 != profile.workflow_sha256:
            issues.append(ContextIssue("WORKFLOW_HASH_MISMATCH", "TaskPackage.workflow_sha256 与 Profile 不一致"))

        registry_ids = {asset.asset_id for asset in project.registry.assets}
        for item in task.inputs:
            if item.asset_id not in registry_ids:
                issues.append(ContextIssue(
                    "INPUT_ASSET_MISSING",
                    f"TaskPackage 输入资产不存在于 AssetRegistry：{item.asset_id}",
                ))

        if profile.status != "approved" or profile.evidence_level != "visual_approved":
            issues.append(ContextIssue(
                "PROFILE_NOT_EXECUTABLE",
                f"Profile 当前为 {profile.status}/{profile.evidence_level}，仅可用于规划",
                "warning",
            ))
        return tuple(issues)


__all__ = ["ContextIssue", "ProjectContext"]
