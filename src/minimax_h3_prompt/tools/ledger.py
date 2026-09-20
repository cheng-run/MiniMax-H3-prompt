"""生成台账：从磁盘重建「镜头 → 视频 → 输入帧」的链路。

背景：会话目录里只有 plan.json / progress.json / session-state.json，
桥接帧文件（``shot-0N-start.png``）与视频文件编号之间没有显式映射，
废弃版本与成品混在同一目录且无任何标记。结果是"哪个镜头用了哪张帧"
在磁盘上无法还原——2026-09-20 一次诊断中，分析者正是因此把废弃初版
``00003`` 当成了真实镜头。

本模块的做法是**建边而不是猜身份**：每张桥接帧对全部视频的尾窗口和首窗口
各做一次 argmin 比对，得到 ``src -> dst`` 的边，再从起点顺着边走。消歧靠
「链延续性 > 生成时间 > 命名自洽」，判不出来的一律进 `review_needed`，不猜。

人写的内容放 ``ledger.override.json``，本模块永不覆盖它。
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .frame_match import (
    MAD_MAYBE,
    imread_unicode,
    match_video_all,
)

SCHEMA = "h3-ledger/1"


@dataclass
class InputFrame:
    """某个镜头的输入帧。"""

    file: str
    kind: str            # "generated"（生图）| "extracted"（视频抽帧）| "unknown"
    source: dict | None  # {"video": "...", "frame": "head" | "tail"}；生图为 None
    match_mad: float | None
    confidence: str      # "high" | "medium" | "low"


@dataclass
class Shot:
    """一个进入正片的镜头。"""

    shot: int
    video: str
    video_dir: str
    frames: int
    duration_s: float
    generated_at: str
    input_frame: InputFrame
    status: str
    evidence: list[str] = field(default_factory=list)


@dataclass
class Discarded:
    """被判定为废弃初版、不进正片的视频。"""

    video: str
    video_dir: str
    reason: str
    replaced_by: str | None
    confidence: str
    evidence: list[str] = field(default_factory=list)


@dataclass
class Interruption:
    """流程中断：plan 期望的某一段没有产出。"""

    at_shot: int
    type: str
    evidence: list[str] = field(default_factory=list)
    confidence: str = "medium"
    hint: str = ""


@dataclass
class Ledger:
    """整套台账。用 to_dict / from_dict 做唯一的序列化出入口。"""

    schema: str
    session: dict
    scan: dict
    shots: list[Shot]
    discarded: list[Discarded]
    interruptions: list[Interruption]
    review_needed: list[str]
    warnings: list[str]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Ledger":
        schema = data.get("schema")
        if schema != SCHEMA:
            raise ValueError(
                f"不认识的 ledger schema: {schema!r}（本工具只支持 {SCHEMA!r}）"
            )
        return cls(
            schema=schema,
            session=dict(data.get("session") or {}),
            scan=dict(data.get("scan") or {}),
            shots=[
                Shot(input_frame=InputFrame(**s["input_frame"]), **{
                    k: v for k, v in s.items() if k != "input_frame"
                })
                for s in data.get("shots") or []
            ],
            discarded=[Discarded(**d) for d in data.get("discarded") or []],
            interruptions=[Interruption(**i) for i in data.get("interruptions") or []],
            review_needed=list(data.get("review_needed") or []),
            warnings=list(data.get("warnings") or []),
        )


# --- 建边与链路重建 --------------------------------------------------------

_BRIDGE_RE = re.compile(r"^shot-(\d+)-start\.png$")


@dataclass
class BridgeRef:
    """一张桥接帧，以及它连出的那条边。

    ``src_video`` 是这张帧的来源（其尾窗口与帧最像的视频）。
    ``dst_candidates`` 是**全部**在阈值内的消费者候选 —— 必须保留多个：
    2026-09-18 实测里 ``shot-03-start.png`` 同时像 ``00003`` 和 ``00004``
    的首帧（mad=0.88），只留一个会让链路在那里断掉。
    """

    shot_no: int
    path: Path
    src_video: Path | None
    src_mad: float | None
    dst_candidates: list[tuple[Path, float]] = field(default_factory=list)


def collect_videos(output_dirs: list[Path]) -> list[Path]:
    """收集输出目录里的 mp4。

    同名视频同时存在带音轨与不带音轨两份时，只保留带音轨的那份。
    不存在的目录静默跳过。
    """
    found: dict[str, Path] = {}
    for directory in output_dirs:
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*.mp4")):
            stem = path.name[:-4]
            base = stem[:-6] if stem.endswith("-audio") else stem
            key = f"{path.parent}::{base}"
            existing = found.get(key)
            if existing is None or path.name.endswith("-audio.mp4"):
                found[key] = path
    return sorted(found.values(), key=lambda p: (str(p.parent), p.name))


def collect_bridges(session_dir: Path) -> list[BridgeRef]:
    """解析 ``bridge_frames/shot-0N-start.png``，按镜头号排序。"""
    bridge_dir = session_dir / "bridge_frames"
    if not bridge_dir.is_dir():
        return []
    refs: list[BridgeRef] = []
    for path in sorted(bridge_dir.glob("shot-*-start.png")):
        matched = _BRIDGE_RE.match(path.name)
        if not matched:
            continue
        refs.append(BridgeRef(shot_no=int(matched.group(1)), path=path,
                              src_video=None, src_mad=None))
    return sorted(refs, key=lambda r: r.shot_no)


def build_edges(bridges: list[BridgeRef], videos: list[Path], *,
                threshold: float = MAD_MAYBE) -> list[BridgeRef]:
    """为每张桥接帧算出 src 与全部 dst 候选。读不到的帧原样留下，不抛异常。"""
    for ref in bridges:
        image = imread_unicode(ref.path)
        if image is None:
            continue
        src = match_video_all(image, videos, window="tail", threshold=threshold)
        if src:
            ref.src_video, ref.src_mad = src[0]
        ref.dst_candidates = match_video_all(image, videos, window="head",
                                             threshold=threshold)
    return bridges


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _usable_edges(edges: list[BridgeRef], threshold: float) -> list[BridgeRef]:
    """只保留 src 可信（mad < threshold）的边。"""
    return [e for e in edges
            if e.src_video is not None and e.src_mad is not None
            and e.src_mad < threshold]


def pick_start(videos: list[Path], edges: list[BridgeRef], *,
               threshold: float = MAD_MAYBE) -> Path | None:
    """起点 = 是别人的 src、但从不是任何边的 dst 的那个视频。

    这比"按尺寸猜"可靠：每一段（除第一段）都是某张桥接帧的消费者，
    所以只有第一段从不出现在 dst 集合里。
    """
    usable = _usable_edges(edges, threshold)
    srcs = {e.src_video for e in usable}
    dsts = {dst for e in usable for dst, _ in e.dst_candidates}
    roots = [v for v in videos if v in srcs and v not in dsts]
    return roots[0] if roots else None


def resolve_chain(edges: list[BridgeRef], videos: list[Path], start: Path, *,
                  threshold: float = MAD_MAYBE) -> list[tuple[int, Path]]:
    """从起点顺着边走，返回 ``[(镜头号, 视频路径), ...]``。

    消歧优先级：
      1. 链延续性——候选的尾帧还被别的桥接帧引用（即它有出边）就优先选它
      2. 生成时间——同分时取 mtime 更晚的
    走不动就停。
    """
    usable = _usable_edges(edges, threshold)
    continuers = {e.src_video for e in usable}

    by_src: dict[Path, list[BridgeRef]] = {}
    for edge in usable:
        by_src.setdefault(edge.src_video, []).append(edge)  # type: ignore[arg-type]

    chain: list[tuple[int, Path]] = [(1, start)]
    visited = {start}
    current = start
    while True:
        options: list[tuple[int, Path, bool, float]] = []
        for edge in by_src.get(current, []):
            for dst, distance in edge.dst_candidates:
                if distance < threshold and dst not in visited:
                    options.append((edge.shot_no, dst, dst in continuers,
                                    _safe_mtime(dst)))
        if not options:
            break
        continuing = [o for o in options if o[2]]
        pool = continuing or options
        pool.sort(key=lambda o: o[3], reverse=True)   # 同分取 mtime 更晚的
        shot_no, chosen, _, _ = pool[0]
        visited.add(chosen)
        chain.append((shot_no, chosen))
        current = chosen
    return chain
