"""主题隔离的 Assets 资产包存储协议。

本模块只负责离线目录和元数据落盘，不执行 ComfyUI，也不扫描或猜测 output 归属。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .project_models import AssetRecord
from .task_package import TaskPackage


# topic_id 允许中文等 Unicode 词字符（主题摘要命名，如 `雨夜旧信-a3f2b1c0`）；
# asset/generation/version 保持 ASCII 严格格式。
_TOPIC_ID = re.compile(r"^[\w][\w-]{0,63}$", re.UNICODE)
_PROJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_ASSET_ID = re.compile(r"^[CSPE]\d{2}$")
_GENERATION_ID = re.compile(r"^(?:[CSPE]\d{2}|SH\d{3})-G\d{3}$")
_VERSION_ID = re.compile(r"^(?:[CSPE]\d{2}|SH\d{3})-v\d{3}$")

_TOPIC_SUBDIRECTORIES = ("projects", "shared", "catalog", "schemas")
_INBOX_DIRECTORY = "inbox"


@dataclass(frozen=True)
class AssetBundlePlan:
    """一个候选资产包的确定性路径计划。"""

    topic_id: str
    project_id: str
    asset_id: str
    generation_id: str
    version: str
    directory: Path

    def to_dict(self) -> dict[str, Any]:
        return {
            "topic_id": self.topic_id,
            "project_id": self.project_id,
            "asset_id": self.asset_id,
            "generation_id": self.generation_id,
            "version": self.version,
            "directory": str(self.directory),
        }


def _validate_token(value: str, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"{label} 格式无效：{value}")
    return value


def _safe_child(root: Path, *parts: str) -> Path:
    """只允许在 root 下生成路径，防止路径穿越和跨主题写入。"""
    candidate = (root.joinpath(*parts)).resolve()
    root = root.resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"目标路径越出 Assets 根目录：{candidate}") from exc
    return candidate


def _write_json(path: Path, data: Any) -> None:
    if path.exists():
        old = path.read_text(encoding="utf-8")
        new = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        if old != new:
            raise FileExistsError(f"目标已存在且内容不同：{path}")
        return
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class AssetStore:
    """管理 Assets 根目录下的主题目录和候选资产包。"""

    def __init__(self, root: str | Path = r"D:\笔记\Assets") -> None:
        self.root = Path(root).resolve()

    def topic_root(self, topic_id: str) -> Path:
        _validate_token(topic_id, _TOPIC_ID, "topic_id")
        return _safe_child(self.root, topic_id)

    def ensure_topic(self, topic_id: str, *, display_name: str = "") -> Path:
        topic_root = self.topic_root(topic_id)
        topic_root.mkdir(parents=True, exist_ok=True)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / _INBOX_DIRECTORY).mkdir(exist_ok=True)
        for name in _TOPIC_SUBDIRECTORIES:
            (topic_root / name).mkdir(exist_ok=True)
        readme = topic_root / "README.md"
        if not readme.exists():
            title = display_name.strip() or topic_id
            readme.write_text(
                f"# {title}\n\n"
                f"- topic_id: `{topic_id}`\n"
                "- `projects/`：该主题下的项目资产。\n"
                "- `shared/`：该主题内已审核通过的共享资产。\n"
                "- `catalog/`：主题级索引。\n"
                "- `schemas/`：主题级数据协议。\n",
                encoding="utf-8",
            )
        return topic_root

    def plan_bundle(
        self,
        topic_id: str,
        project_id: str,
        asset_id: str,
        generation_id: str,
        version: str,
    ) -> AssetBundlePlan:
        _validate_token(topic_id, _TOPIC_ID, "topic_id")
        _validate_token(project_id, _PROJECT_ID, "project_id")
        _validate_token(asset_id, _ASSET_ID, "asset_id")
        _validate_token(generation_id, _GENERATION_ID, "generation_id")
        _validate_token(version, _VERSION_ID, "version")
        if not version.startswith(f"{asset_id}-"):
            raise ValueError("资产版本必须以 asset_id 开头")
        directory = _safe_child(self.root, _INBOX_DIRECTORY, topic_id, asset_id, generation_id, version)
        return AssetBundlePlan(topic_id, project_id, asset_id, generation_id, version, directory)

    def write_bundle(
        self,
        task: TaskPackage,
        asset: AssetRecord,
        *,
        version: str,
        dry_run: bool = False,
    ) -> AssetBundlePlan:
        """写入 task.json、prompt.md、asset.json 和主题 catalog。"""
        if not task.topic_id or not task.project_id:
            raise ValueError("TaskPackage 必须包含 topic_id 和 project_id")
        if task.generation_id != asset.generation_id:
            raise ValueError("TaskPackage 与 AssetRecord 的 generation_id 不一致")
        if task.workflow_sha256 != asset.workflow_sha256:
            raise ValueError("TaskPackage 与 AssetRecord 的 workflow_sha256 不一致")
        if asset.workflow_profile_id != task.profile_id:
            raise ValueError("TaskPackage 与 AssetRecord 的 profile_id 不一致")
        if asset.workflow_profile_version != task.profile_version:
            raise ValueError("TaskPackage 与 AssetRecord 的 profile_version 不一致")
        expected_prompt_hash = str(task.to_dict()["prompt_sha256"])
        if asset.prompt_sha256 != expected_prompt_hash:
            raise ValueError("TaskPackage 与 AssetRecord 的 prompt_sha256 不一致")
        if asset.version and asset.version != version:
            raise ValueError("AssetRecord 的 version 与资产包 version 不一致")

        plan = self.plan_bundle(task.topic_id, task.project_id, asset.asset_id, task.generation_id, version)
        if dry_run:
            return plan
        self.ensure_topic(task.topic_id)
        plan.directory.parent.mkdir(parents=True, exist_ok=True)
        plan.directory.mkdir(exist_ok=False)
        task.write(plan.directory)
        _write_json(plan.directory / "asset.json", asset.to_dict())
        catalog_entry = {
            "schema_version": "catalog-entry.v1",
            "topic_id": plan.topic_id,
            "project_id": plan.project_id,
            "asset_id": plan.asset_id,
            "generation_id": plan.generation_id,
            "version": plan.version,
            "status": asset.status,
            "asset_type": asset.asset_type,
            "path": str(plan.directory),
            "profile_id": task.profile_id,
            "profile_version": task.profile_version,
            "workflow_sha256": task.workflow_sha256,
            "prompt_sha256": expected_prompt_hash,
        }
        catalog_path = self.topic_root(plan.topic_id) / "catalog" / "entries.jsonl"
        catalog_path.parent.mkdir(parents=True, exist_ok=True)
        existing = catalog_path.read_text(encoding="utf-8") if catalog_path.exists() else ""
        line = json.dumps(catalog_entry, ensure_ascii=False, sort_keys=True)
        if line not in existing.splitlines():
            with catalog_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        return plan


def create_topic_template(root: str | Path = r"D:\笔记\Assets") -> Path:
    """创建根 README 和可复制的主题模板；不创建正式主题。"""
    root_path = Path(root).resolve()
    root_path.mkdir(parents=True, exist_ok=True)
    (root_path / "_template").mkdir(exist_ok=True)
    for name in _TOPIC_SUBDIRECTORIES:
        (root_path / "_template" / name).mkdir(parents=True, exist_ok=True)
    (root_path / _INBOX_DIRECTORY).mkdir(parents=True, exist_ok=True)
    readme = root_path / "README.md"
    if not readme.exists():
        readme.write_text(
            "# 长视频主题资产库\n\n"
            "一级目录按长视频主题隔离；`_template/` 仅用于创建新主题。\n\n"
            "ComfyUI 原始 workflow 和 `D:\\Comfyui\\ComfyUI\\output` 只读，结果只能复制。\n"
            "没有 Prompt、TaskPackage、Profile 与 workflow SHA-256 关联的试玩结果不得导入。\n"
            "资产导入后默认为 candidate/inbox，不能自动标记 approved；修订必须创建新版本。\n"
            "API key 不得写入 Assets。\n",
            encoding="utf-8",
        )
    template_readme = root_path / "_template" / "README.md"
    if not template_readme.exists():
        template_readme.write_text(
            "# 长视频主题模板\n\n"
            "复制本目录并改名为 topic_id 后使用；不要把 `_template` 本身当作正式主题。\n\n"
            "本资产库的 `inbox/` 位于 Assets 根目录，用于接收已关联任务的 ComfyUI output；审核后再按主题归档到 `projects/` 或 `shared/`。\n"
            "本主题目录只保留 `projects/`、`shared/`、`catalog/` 和 `schemas/`。\n\n"
            "资产 ID：`C01` 人物、`S01` 场景、`P01` 道具、`E01` 背景元素。\n"
            "生成 ID：`C01-G001`；版本：`C01-v001`。\n"
            "流程：`Assets/inbox → <topic_id>/projects|shared`，必须经过人工审核；candidate 不等于 approved。\n",
            encoding="utf-8",
        )
    return root_path
