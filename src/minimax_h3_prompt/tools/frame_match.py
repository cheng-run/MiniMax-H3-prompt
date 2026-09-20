"""帧读取与比对原语。

所有涉及视频/图片读取、灰度化、帧间差异的操作都收在这里，原因有二：

1. Windows 上 ``cv2.imread`` 打不开非 ASCII 路径（中文目录），会返回 None；
   必须改走 ``np.fromfile`` + ``cv2.imdecode``。把这个坑关在一个模块里。
   （``cv2.VideoCapture`` 反而正常，不要给它加编码转换。）
2. 尾帧用 ``CAP_PROP_POS_FRAMES`` 精确 seek 会因 H.264 关键帧对齐而偏移，
   所以往回多退若干帧再顺序读到尾。

比对一律用**窗口最小 mad**（默认各取 5 帧），而不是单帧——因为抽帧脚本
抽取的未必是精确的首/尾帧。
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

ANALYZE_W = 160
ANALYZE_H = 92
WINDOW = 5

# 尾帧 seek 的回退量，避开关键帧对齐造成的偏移
_TAIL_REWIND = 12


def imread_unicode(path: Path, flags: int = cv2.IMREAD_COLOR) -> np.ndarray | None:
    """读图，支持中文路径。读不到返回 None（不抛异常）。"""
    try:
        buf = np.fromfile(str(path), dtype=np.uint8)
    except OSError:
        return None
    if buf.size == 0:
        return None
    return cv2.imdecode(buf, flags)


def to_gray(bgr: np.ndarray) -> np.ndarray:
    """BGR 图 → 降采样灰度（分析用）。已经是单通道时只做缩放。"""
    small = cv2.resize(bgr, (ANALYZE_W, ANALYZE_H), interpolation=cv2.INTER_AREA)
    if small.ndim == 3:
        small = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    return small.astype(np.float32)


def mad(a: np.ndarray, b: np.ndarray) -> float:
    """平均绝对差（0–255 刻度）。"""
    return float(np.mean(np.abs(a - b)))


def video_meta(path: Path) -> dict:
    """帧数 / fps / 宽高 / 时长。读不到时返回空 dict。"""
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        cap.release()
        return {}
    meta = {
        "frames": int(cap.get(cv2.CAP_PROP_FRAME_COUNT)),
        "fps": float(cap.get(cv2.CAP_PROP_FPS) or 0.0),
        "width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
        "height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
    }
    cap.release()
    meta["duration_s"] = meta["frames"] / meta["fps"] if meta["fps"] else 0.0
    return meta


def read_frames_gray(path: Path) -> list[np.ndarray]:
    """整段视频读成灰度小图列表。读不到返回空列表。"""
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        cap.release()
        return []
    frames: list[np.ndarray] = []
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        frames.append(to_gray(frame))
    cap.release()
    return frames


def read_window(path: Path, *, window: str, k: int = WINDOW) -> list[np.ndarray]:
    """取首 k 帧或尾 k 帧的灰度图。

    ``window`` 必须是 ``"head"`` 或 ``"tail"``；非法值抛 ValueError。
    视频读不到时返回空列表。
    """
    if window not in ("head", "tail"):
        raise ValueError(f'window 必须是 "head" 或 "tail"，收到 {window!r}')

    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        cap.release()
        return []

    if window == "head":
        out: list[np.ndarray] = []
        for _ in range(k):
            ok, frame = cap.read()
            if not ok:
                break
            out.append(to_gray(frame))
        cap.release()
        return out

    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, total - k - _TAIL_REWIND))
    buf: list[np.ndarray] = []
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        buf.append(to_gray(frame))
    cap.release()
    return buf[-k:] if len(buf) >= k else buf
