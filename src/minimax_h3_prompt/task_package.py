"""生成任务包与资产导入协议。

导入器只复制已关联的输出，不移动、不修改 ComfyUI 原始结果。
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image

from .workflow_profiles import WorkflowProfile


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class AssetInput:
    asset_id: str
    path: str
    slot_id: str = ""
    picture: int | None = None
    sha256: str = ""

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class TaskPackage:
    generation_id: str
    task_type: str
    profile_id: str
    profile_version: str
    workflow_path: str
    workflow_sha256: str
    prompt: str
    inputs: tuple[AssetInput, ...] = field(default_factory=tuple)
    project_id: str = ""
    topic_id: str = ""
    expected_output_type: str = ""
    expected_width: int | None = None
    expected_height: int | None = None
    expected_frames: int | None = None
    expected_fps: float | None = None
    output_prefix: str = ""
    profile_status_at_creation: str = "candidate"
    created_at: str = ""
    schema_version: str = "1"
    status: str = "planned"
    manual_steps: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_profile(
        cls,
        generation_id: str,
        task_type: str,
        profile: WorkflowProfile,
        prompt: str,
        *,
        inputs: tuple[AssetInput, ...] = (),
        **overrides: Any,
    ) -> TaskPackage:
        return cls(
            generation_id=generation_id,
            task_type=task_type,
            profile_id=profile.profile_id,
            profile_version=profile.profile_version,
            workflow_path=profile.workflow_path,
            workflow_sha256=profile.workflow_sha256,
            prompt=prompt,
            inputs=inputs,
            created_at=_now(),
            profile_status_at_creation=profile.status,
            manual_steps=profile.manual_steps,
            **overrides,
        )

    def to_dict(self) -> dict[str, Any]:
        data = {
            "schema_version": self.schema_version,
            "generation_id": self.generation_id,
            "created_at": self.created_at,
            "project_id": self.project_id,
            "topic_id": self.topic_id,
            "task_type": self.task_type,
            "profile_id": self.profile_id,
            "profile_version": self.profile_version,
            "profile_status_at_creation": self.profile_status_at_creation,
            "workflow_path": self.workflow_path,
            "workflow_sha256": self.workflow_sha256,
            "prompt": self.prompt,
            "prompt_sha256": hashlib.sha256(self.prompt.encode("utf-8")).hexdigest(),
            "inputs": [item.to_dict() for item in self.inputs],
            "expected_output_type": self.expected_output_type,
            "expected_width": self.expected_width,
            "expected_height": self.expected_height,
            "expected_frames": self.expected_frames,
            "expected_fps": self.expected_fps,
            "output_prefix": self.output_prefix,
            "manual_steps": list(self.manual_steps),
            "status": self.status,
        }
        return data

    def write(self, directory: str | Path) -> Path:
        target = Path(directory)
        target.mkdir(parents=True, exist_ok=True)
        (target / "prompt.md").write_text(self.prompt, encoding="utf-8")
        task_path = target / "task.json"
        task_path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return task_path


@dataclass(frozen=True)
class ImportedAsset:
    asset_id: str
    generation_id: str
    source_path: str
    target_path: str
    sha256: str
    size: int
    mime_type: str
    status: str = "inbox"
    width: int | None = None
    height: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def resolve_input_path(raw_path: str | Path, *, base_dir: str | Path | None = None) -> Path:
    """在 Windows 上优先解析本机路径，同时兼容 brief 中的 /mnt/<drive>/ 写法。"""
    text = str(raw_path).strip().strip('"\'')
    if text.startswith("/mnt/") and len(text) > 6 and text[5].isalpha():
        text = f"{text[5].upper()}:\\{text[7:].replace('/', chr(92))}"
    candidate = Path(text)
    if not candidate.is_absolute() and base_dir:
        candidate = Path(base_dir) / candidate
    return candidate.resolve()


def inspect_asset(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(source)
    result: dict[str, Any] = {
        "path": str(source),
        "size": source.stat().st_size,
        "sha256": _sha256(source),
        "mime_type": mimetypes.guess_type(source.name)[0] or "application/octet-stream",
    }
    try:
        with Image.open(source) as image:
            result["width"], result["height"] = image.size
            result["mime_type"] = Image.MIME.get(image.format, result["mime_type"])
    except (OSError, Image.UnidentifiedImageError):
        pass
    return result


def import_output(
    source_path: str | Path,
    task: TaskPackage,
    destination_dir: str | Path,
    *,
    asset_id: str | None = None,
    allow_non_output_source: bool = False,
) -> ImportedAsset:
    """复制一个已由任务明确关联的输出到 inbox，并校验副本哈希。"""
    source = Path(source_path).resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    if not task.generation_id:
        raise ValueError("缺少 generation_id，禁止导入无关联结果")
    if not task.profile_id or not task.workflow_sha256:
        raise ValueError("任务包缺少 Profile 或 workflow SHA-256")
    if not allow_non_output_source and "output" not in {part.lower() for part in source.parts}:
        raise ValueError("源文件不在 ComfyUI output 路径下")

    info = inspect_asset(source)
    digest = str(info["sha256"])
    asset_id = asset_id or f"{task.generation_id}-{digest[:12]}"
    destination = Path(destination_dir)
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / source.name
    if target.exists():
        if _sha256(target) != digest:
            raise FileExistsError(f"目标已存在且内容不同：{target}")
    else:
        shutil.copy2(source, target)
    if _sha256(target) != digest:
        raise IOError("复制后的资产 SHA-256 与源文件不一致")

    imported = ImportedAsset(
        asset_id=asset_id,
        generation_id=task.generation_id,
        source_path=str(source),
        target_path=str(target),
        sha256=digest,
        size=int(info["size"]),
        mime_type=str(info["mime_type"]),
        width=info.get("width"),
        height=info.get("height"),
    )
    (destination / "source.json").write_text(
        json.dumps({
            "asset": imported.to_dict(),
            "generation_id": task.generation_id,
            "profile_id": task.profile_id,
            "profile_version": task.profile_version,
            "workflow_sha256": task.workflow_sha256,
            "copied_at": _now(),
        }, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return imported
