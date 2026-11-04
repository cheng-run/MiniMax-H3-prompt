"""按需词（段首帧／尾帧）的 sidecar：**不进可复制正文**的机器字段。

为什么单独落盘：``composition`` / ``lighting`` / ``negative_prompt`` 与模型 provenance
都不是给生图模型读的正文——粘进 ``firstframe-NN.md`` 会让用户复制走两份口径，而且
「生成后核对字段」与「拿去出图的正文」本来就该分开（尾帧的三段式文档同理，锁死清单
只留在文件里）。但字段又必须与正文**同生共死**：正文换了、字段不换，就成了「挂着
一档的牌子、字段按另一档写」。

sidecar 与被描述文件同名同目录：``firstframe-02.md`` ↔ ``firstframe-02.meta.json``。
被描述文件既可能是提示词正文（段首帧词），也可能是图片（全片尾帧的读图结果，
``film-end.png`` ↔ ``film-end.meta.json``）——本模块只按路径换算文件名，不假设内容类型。

sidecar 是**累积记录**：给已有 sidecar 补字段用 ``merge_frame_meta``（同名覆盖、
未知字段保留），只有「整份重置」才用 ``write_frame_meta`` 并写明意图。
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path

META_SUFFIX = ".meta.json"


def sha256_text(text: str | None) -> str | None:
    """文本指纹；``None`` 原样返回（「这段没落盘」与空文件是两件事）。"""
    if text is None:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: str | Path | None) -> str | None:
    """文件指纹；文件不存在返回 ``None``，不猜也不抛（参考图是可选输入时要用）。"""
    if path is None:
        return None
    try:
        data = Path(path).read_bytes()
    except FileNotFoundError:
        return None
    return hashlib.sha256(data).hexdigest()


def frame_meta_path(prompt_path: str | Path) -> Path:
    """被描述文件的路径 → sidecar 路径（同名换扩展名，不另立一套编号）。

    入参名沿用 ``prompt_path`` 是历史命名：调用方也会传**图片路径**（全片尾帧读图），
    所以本函数只看文件名，不看后缀语义。
    """
    path = Path(prompt_path)
    return path.with_name(path.stem + META_SUFFIX)


def write_frame_meta(prompt_path: str | Path,
                     payload: Mapping[str, object]) -> Path:
    """写 sidecar；内容按 JSON 原样落盘（调用方决定字段，本模块不猜）。"""
    path = frame_meta_path(prompt_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(dict(payload), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    return path


def read_frame_meta(prompt_path: str | Path) -> dict | None:
    """读 sidecar；缺失或损坏返回 ``None``（旧产物没有它，不能因此读崩）。"""
    path = frame_meta_path(prompt_path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        # 坏 sidecar 不能把续跑拦死：provenance 是**提示**，正文才是产物。
        print(f"[警告] sidecar 读不回来，按没有 provenance 处理：{path}", flush=True)
        return None
    return data if isinstance(data, dict) else None


def merge_frame_meta(prompt_path: str | Path,
                     payload: Mapping[str, object], *,
                     defaults: Mapping[str, object] | None = None) -> Path:
    """在既有 sidecar 上合并字段：``defaults`` 只补缺，``payload`` 覆盖同名键。

    为什么默认不是整份覆盖：sidecar 是**累积记录**，同一份文件可能有两个写口——
    段首帧词写 provenance，陪跑又问到用户挂的首帧图路径（#158）。重写正文时整份
    覆盖会静默抹掉另一个写口的字段（2026-11-04 #157 复审实测：续跑时只剩「元信息
    缺失」，画幅核对回落到关键帧画布）。整份重置仍用 ``write_frame_meta``（例如全片
    尾帧换图后重读描述），意图要显式写出来。
    """
    merged: dict[str, object] = dict(read_frame_meta(prompt_path) or {})
    if defaults:
        for key, value in defaults.items():
            merged.setdefault(key, value)
    merged.update(dict(payload))
    return write_frame_meta(prompt_path, merged)
