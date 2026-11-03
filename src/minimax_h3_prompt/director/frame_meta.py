"""按需词（段首帧／尾帧）的 sidecar：**不进可复制正文**的机器字段。

为什么单独落盘：``composition`` / ``lighting`` / ``negative_prompt`` 与模型 provenance
都不是给生图模型读的正文——粘进 ``firstframe-NN.md`` 会让用户复制走两份口径，而且
「生成后核对字段」与「拿去出图的正文」本来就该分开（尾帧的三段式文档同理，锁死清单
只留在文件里）。但字段又必须与正文**同生共死**：正文换了、字段不换，就成了「挂着
一档的牌子、字段按另一档写」。

sidecar 与正文同名同目录：``firstframe-02.md`` ↔ ``firstframe-02.meta.json``。
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
    """正文路径 → sidecar 路径（同名换扩展名，不另立一套编号）。"""
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
