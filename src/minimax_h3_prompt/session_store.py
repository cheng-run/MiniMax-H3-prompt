"""两阶段生产流的会话持久化：阶段 1 产物落盘，阶段 2 断点续接。

阶段 1 结束后用户要离开终端去 ComfyUI 生图（几十分钟到数小时），因此完整 state
必须可序列化落盘；重启后 load 回来即可直接跑阶段 2，不需要重跑任何角色 agent。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .brief_parser import Brief, RefItem

SESSION_FILENAME = "session-state.json"
_SCHEMA_VERSION = "wizard_session.v1"

# status: awaiting_frames = 阶段 1 完成、等待用户提交帧图；completed = 视频提示词已产出
STATUS_AWAITING_FRAMES = "awaiting_frames"
STATUS_COMPLETED = "completed"

# state 中允许持久化的键；brief 对象单独序列化
_STATE_KEYS = (
    "production_plan", "director_brief", "creative_lock", "script",
    "character_design", "background_design", "prop_design", "art_design",
    "character_image_prompts", "prop_image_prompts", "scene_image_prompts",
    "identity_lock", "shot_table", "shot_review_lock", "visual_design",
    "fl2va_prompt_bundle", "subject_defs", "sound_design", "music",
    "final_prompt", "final_report",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _brief_to_dict(brief: Brief) -> dict[str, Any]:
    return {
        "mode": brief.mode,
        "variant": brief.variant,
        "duration": brief.duration,
        "style": brief.style,
        "language": brief.language,
        "plot": brief.plot,
        "refs": [
            {"picture": r.picture, "name": r.name, "description": r.description, "path": r.path}
            for r in brief.refs
        ],
        "draft": brief.draft,
    }


def _brief_from_dict(raw: dict[str, Any]) -> Brief:
    brief = Brief(
        mode=str(raw.get("mode", "base")),
        variant=str(raw.get("variant", "T2VA")),
        duration=float(raw.get("duration", 5.0)),
        style=str(raw.get("style", "")),
        language=str(raw.get("language", "Chinese")),
        plot=str(raw.get("plot", "")),
        draft=str(raw.get("draft", "")),
    )
    for item in raw.get("refs", []):
        brief.refs.append(RefItem(
            picture=int(item.get("picture", 1)),
            name=str(item.get("name", "")),
            description=str(item.get("description", "")),
            path=str(item.get("path", "")),
        ))
    return brief


@dataclass(frozen=True)
class SessionState:
    """一个 generation 的两阶段会话。"""

    directory: Path
    brief: Brief
    stage_state: dict[str, Any]
    status: str
    variant_downgraded: str = ""
    frame_images: tuple[dict[str, Any], ...] = ()
    created_at: str = ""
    updated_at: str = ""

    @property
    def awaiting_frames(self) -> bool:
        return self.status == STATUS_AWAITING_FRAMES

    def save(self) -> Path:
        payload: dict[str, Any] = {
            "schema_version": _SCHEMA_VERSION,
            "status": self.status,
            "brief": _brief_to_dict(self.brief),
            "stage_state": {k: self.stage_state[k] for k in _STATE_KEYS if k in self.stage_state},
            "variant_downgraded": self.variant_downgraded,
            "frame_images": [dict(item) for item in self.frame_images],
            "created_at": self.created_at or _now(),
            "updated_at": _now(),
        }
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self.directory / SESSION_FILENAME
        if not self.created_at and target.exists():
            old = json.loads(target.read_text(encoding="utf-8"))
            payload["created_at"] = str(old.get("created_at") or payload["created_at"])
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return target


def save_session(directory: str | Path, brief: Brief, stage_state: dict[str, Any], *,
                 status: str = STATUS_AWAITING_FRAMES) -> Path:
    """保存或更新会话；directory 为 generation 目录。"""
    session = SessionState(Path(directory), brief, stage_state, status)
    return session.save()


def load_session(directory: str | Path) -> SessionState | None:
    """加载会话；文件不存在或损坏返回 None（调用方决定回退到新建流程）。"""
    target = Path(directory) / SESSION_FILENAME
    if not target.is_file():
        return None
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if raw.get("schema_version") != _SCHEMA_VERSION:
        return None
    return SessionState(
        directory=Path(directory),
        brief=_brief_from_dict(raw.get("brief", {})),
        stage_state=dict(raw.get("stage_state", {})),
        status=str(raw.get("status", STATUS_AWAITING_FRAMES)),
        variant_downgraded=str(raw.get("variant_downgraded", "")),
        frame_images=tuple(dict(x) for x in raw.get("frame_images", [])),
        created_at=str(raw.get("created_at", "")),
        updated_at=str(raw.get("updated_at", "")),
    )


def find_awaiting_sessions(root_dir: str | Path) -> list[SessionState]:
    """扫描会话根目录下所有待续接的会话（供向导恢复入口）。只读，不猜测内容归属。"""
    root = Path(root_dir)
    results: list[SessionState] = []
    if not root.is_dir():
        return results
    for session_file in sorted(root.glob("**/" + SESSION_FILENAME)):
        session = load_session(session_file.parent)
        if session is not None and session.awaiting_frames:
            results.append(session)
    return results


__all__ = [
    "SessionState", "SESSION_FILENAME", "STATUS_AWAITING_FRAMES", "STATUS_COMPLETED",
    "save_session", "load_session", "find_awaiting_sessions",
]
