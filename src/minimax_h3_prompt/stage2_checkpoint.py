"""阶段 2 断点（stage2 checkpoint）：把 prompt 组装初稿落盘，崩溃后从质检精修阶段续跑。

位置：``<generation_dir>/stage2-checkpoint.json``。阶段 2 完整结束后删除，避免后续误续。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

FILENAME = "stage2-checkpoint.json"
_SCHEMA_VERSION = "stage2_checkpoint.v1"


def checkpoint_path(directory: str | Path) -> Path:
    return Path(directory) / FILENAME


def save_checkpoint(directory: str | Path, *, prompt_draft: str, status: str = "assemble_done") -> Path:
    path = checkpoint_path(directory)
    payload = {
        "schema_version": _SCHEMA_VERSION,
        "status": status,
        "prompt_draft": prompt_draft,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_checkpoint(directory: str | Path) -> dict | None:
    """读取断点；文件缺失/损坏/版本不符返回 None。"""
    path = checkpoint_path(directory)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema_version") != _SCHEMA_VERSION:
        return None
    if not data.get("prompt_draft"):
        return None
    return data


def clear_checkpoint(directory: str | Path) -> None:
    try:
        checkpoint_path(directory).unlink(missing_ok=True)
    except OSError:
        pass
