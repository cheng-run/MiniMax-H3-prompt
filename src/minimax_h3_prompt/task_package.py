"""生成任务包与资产导入协议。

导入器只复制已关联的输出，不移动、不修改 ComfyUI 原始结果。
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import shutil
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GENERATION_ID_RE = re.compile(r"^(?:G\d{3}|[A-Z][A-Z0-9]*-G\d{3})$")
_TASK_SCHEMA_VERSION = "1"
_TASK_FIELDS = {
    "schema_version", "generation_id", "created_at", "project_id", "topic_id",
    "task_type", "profile_id", "profile_version", "profile_status_at_creation",
    "workflow_path", "workflow_sha256", "prompt", "prompt_sha256", "inputs",
    "expected_output_type", "expected_width", "expected_height", "expected_frames",
    "expected_fps", "output_prefix", "manual_steps", "status",
}


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

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> AssetInput:
        if not isinstance(raw, dict):
            raise ValueError("TaskPackage 的 inputs 项必须是对象")
        required = ("asset_id", "path")
        missing = [key for key in required if not isinstance(raw.get(key), str) or not raw[key].strip()]
        if missing:
            raise ValueError(f"AssetInput 缺少必填字段：{', '.join(missing)}")
        picture = raw.get("picture")
        if picture is not None and (isinstance(picture, bool) or not isinstance(picture, int) or picture < 1):
            raise ValueError("AssetInput.picture 必须是正整数或 null")
        sha256 = raw.get("sha256", "")
        if not isinstance(sha256, str) or (sha256 and not _SHA256_RE.fullmatch(sha256)):
            raise ValueError("AssetInput.sha256 必须是 64 位小写 SHA-256")
        unknown = set(raw) - {"asset_id", "path", "slot_id", "picture", "sha256"}
        if unknown:
            raise ValueError(f"AssetInput 包含未知字段：{', '.join(sorted(unknown))}")
        return cls(
            asset_id=raw["asset_id"].strip(),
            path=raw["path"].strip(),
            slot_id=str(raw.get("slot_id", "")),
            picture=picture,
            sha256=sha256,
        )

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
    def from_dict(cls, raw: dict[str, Any]) -> TaskPackage:
        """严格从 task.json 对象反序列化，不读取文件或执行外部操作。"""
        if not isinstance(raw, dict):
            raise ValueError("TaskPackage 顶层必须是对象")
        unknown = set(raw) - _TASK_FIELDS
        if unknown:
            raise ValueError(f"TaskPackage 包含未知字段：{', '.join(sorted(unknown))}")
        if raw.get("schema_version") != _TASK_SCHEMA_VERSION:
            raise ValueError(f"不支持的 TaskPackage schema_version：{raw.get('schema_version')!r}")

        required = (
            "generation_id", "task_type", "profile_id", "profile_version",
            "workflow_path", "workflow_sha256", "prompt", "prompt_sha256",
        )
        missing = [key for key in required if not isinstance(raw.get(key), str) or not raw[key].strip()]
        if missing:
            raise ValueError(f"TaskPackage 缺少必填字段：{', '.join(missing)}")
        generation_id = raw["generation_id"]
        if not _GENERATION_ID_RE.fullmatch(generation_id):
            raise ValueError(f"generation_id 格式无效：{generation_id}")
        workflow_sha256 = raw["workflow_sha256"]
        prompt_sha256 = raw["prompt_sha256"]
        if not _SHA256_RE.fullmatch(workflow_sha256):
            raise ValueError("workflow_sha256 必须是 64 位小写 SHA-256")
        if not _SHA256_RE.fullmatch(prompt_sha256):
            raise ValueError("prompt_sha256 必须是 64 位小写 SHA-256")
        actual_prompt_sha256 = hashlib.sha256(raw["prompt"].encode("utf-8")).hexdigest()
        if prompt_sha256 != actual_prompt_sha256:
            raise ValueError("prompt_sha256 与 prompt 内容不一致")

        inputs = raw.get("inputs", [])
        if not isinstance(inputs, list):
            raise ValueError("TaskPackage.inputs 必须是数组")
        manual_steps = raw.get("manual_steps", [])
        if not isinstance(manual_steps, list) or not all(isinstance(item, str) for item in manual_steps):
            raise ValueError("TaskPackage.manual_steps 必须是字符串数组")
        for key in ("project_id", "topic_id", "created_at", "profile_status_at_creation", "status"):
            if key in raw and not isinstance(raw[key], str):
                raise ValueError(f"TaskPackage.{key} 必须是字符串")

        return cls(
            generation_id=generation_id,
            task_type=raw["task_type"],
            profile_id=raw["profile_id"],
            profile_version=raw["profile_version"],
            workflow_path=raw["workflow_path"],
            workflow_sha256=workflow_sha256,
            prompt=raw["prompt"],
            inputs=tuple(AssetInput.from_dict(item) for item in inputs),
            project_id=raw.get("project_id", ""),
            topic_id=raw.get("topic_id", ""),
            expected_output_type=raw.get("expected_output_type", ""),
            expected_width=raw.get("expected_width"),
            expected_height=raw.get("expected_height"),
            expected_frames=raw.get("expected_frames"),
            expected_fps=raw.get("expected_fps"),
            output_prefix=raw.get("output_prefix", ""),
            profile_status_at_creation=raw.get("profile_status_at_creation", "candidate"),
            created_at=raw.get("created_at", ""),
            schema_version=raw["schema_version"],
            status=raw.get("status", "planned"),
            manual_steps=tuple(manual_steps),
        )

    @classmethod
    def load(cls, path: str | Path) -> TaskPackage:
        """从任务包目录或 task.json 加载，并校验同目录 prompt.md。"""
        target = Path(path)
        directory = target if target.is_dir() else target.parent
        task_path = directory / "task.json" if target.is_dir() else target
        prompt_path = directory / "prompt.md"
        if not task_path.is_file():
            raise FileNotFoundError(f"TaskPackage 缺少 task.json：{task_path}")
        if not prompt_path.is_file():
            raise FileNotFoundError(f"TaskPackage 缺少 prompt.md：{prompt_path}")
        try:
            raw = json.loads(task_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"TaskPackage task.json 无法读取：{task_path}") from exc
        task = cls.from_dict(raw)
        prompt = prompt_path.read_text(encoding="utf-8")
        if prompt != task.prompt:
            raise ValueError("TaskPackage 的 prompt.md 与 task.json.prompt 不一致")
        return task

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

    def with_status(self, status: str) -> TaskPackage:
        """返回状态已更新的新任务包；``write()`` 负责持久化。"""
        return replace(self, status=status)


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
