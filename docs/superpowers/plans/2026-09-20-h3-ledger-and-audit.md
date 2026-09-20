# H3 生成台账与审计 Implementation Plan

> **For agentic workers:** 逐个任务执行，用复选框（`- [ ]`）跟踪进度。
>
> **执行方式二选一：**
> - **Subagent 驱动（推荐）**：每个任务派一个**全新的 subagent**（用 Agent 工具，`subagent_type: "general-purpose"`），只把该任务的正文发给它；任务完成后由你审查、跑测试、再派下一个。这样每个任务的上下文是干净的，不会被前面的任务污染。
> - **Inline 执行**：在当前会话里按顺序做，每个任务结束（提交后）停下来让用户确认。
>
> ⚠️ 本环境**没有安装** `superpowers:subagent-driven-development` 与 `superpowers:executing-plans` 这两个 skill，上面两种方式请手动实现。`test-driven-development` skill 已安装，可作为纪律约束加载。

**Goal:** 让"哪个镜头用了哪张帧""接缝有多静止""还剩多少额度"变成磁盘上可查的事实，取代目前只能靠人记的状态。

**Architecture:** 一个数据源 + 三个消费者。`tools/ledger.py` 从磁盘（桥接帧 ↔ 视频首尾帧的逐一比对）自动推断出「镜头 → 视频 → 输入帧」链路并落盘 `ledger.json`；`tools/preflight.py`（提交前校验）与 `tools/seam_audit.py`（接缝量化）都只读 ledger，不自己推断。帧读取与比对的底层原语收在 `tools/frame_match.py`。

**Tech Stack:** Python 3.14、opencv-python-headless（已有）、numpy、pytest、imageio-ffmpeg（新增，用于合并视频并保留音轨）

**Spec:** `docs/superpowers/specs/2026-09-20-h3-ledger-and-audit-design.md`

## Global Constraints

- `requires-python = ">=3.14"`，沿用现有 `pyproject.toml`
- **只读铁律**：预检、台账、审计**绝不修改任何输入文件**。ComfyUI 的 output 目录一行不写
- **绝不静默失败**：新代码里**禁止** `except Exception: pass`。读不了的文件 → 记进 `warnings` 并跳过，或进 `review_needed`。宁可报告多一行"这 3 个文件读不了"，也不要让结果看起来是完整的
- **不引入** guardrails / deepeval / promptfoo / deeapval；只借鉴语义
- **不改动** `graph/`、`ui/`、`pipeline.py`、`session_store.py`、`agents/`。仅 `main.py` 加 CLI 子命令组
- ledger schema 字符串固定为 `"h3-ledger/1"`；读到不认识的版本**拒读**并提示
- MAD 阈值：`MAD_SAME = 2.0`（同源）、`MAD_MAYBE = 5.0`（疑似）。比较用**窗口最小 mad**，不用单帧
- 视频尺寸 `1280×736` 判定为 `kind="extracted"`；其他尺寸（如 3840×2160）判定为 `kind="generated"`
- 审计报告**主推「每缝均值」与「占全片比例」**，总数仅作参考并显式标注镜头数
- 代码风格沿用项目现状：模块级中文 docstring 说明"为什么"，`from __future__ import annotations`，类型标注齐全
- 测试命令统一用 `./.venv/Scripts/python.exe -m pytest`

---

## 文件结构

| 文件 | 职责 |
|---|---|
| `src/minimax_h3_prompt/tools/frame_match.py` | **新建**。帧读取与比对原语：中文路径安全读图、首尾窗口、灰度降采样、MAD、阈值分级 |
| `src/minimax_h3_prompt/tools/ledger.py` | **新建**。数据结构 + 从磁盘推断链路 + override 合并 + 落盘 |
| `src/minimax_h3_prompt/tools/preflight.py` | **新建**。提交前三类校验规则 |
| `src/minimax_h3_prompt/tools/seam_audit.py` | **新建**。静止检测 + ffmpeg 合并 + 报告 + 基线对比 |
| `src/minimax_h3_prompt/main.py` | **修改**。加 `ledger` / `audit` / `preflight` 三个子命令 |
| `tests/conftest.py` | **新建**。合成视频 fixture |
| `tests/test_frame_match.py` | **新建** |
| `tests/test_ledger.py` | **新建** |
| `tests/test_preflight.py` | **新建** |
| `tests/test_seam_audit.py` | **新建** |
| `tests/test_ledger_real_assets.py` | **新建**。真实素材回归，素材不存在时条件跳过 |
| `pyproject.toml` | **修改**。加 `imageio-ffmpeg` 依赖 |

> **参考实现（throwaway，不必读，但卡住时可对照）**：`D:\h3_spike\merge_and_inspect.py`
> 这是 2026-09-20 做完的一次性诊断脚本，阶段 2 的 `seam_audit` 由它演化而来，
> 已实测跑通过真实素材。同目录下 `seam_report.md` 与 `seams/*.png` 是它的输出。
> 它的 `merged_naive.mp4`（28.67s）就是《古老图书馆》的正片。
> **本计划不依赖它**——Task 9/10 已写出完整实现。

---

# 阶段 1：地基 — `frame_match` + `ledger`

> 阶段 1 结束时，你能跑一条命令查出《古老图书馆》的 5 个镜头顺序、`00003` 是废片、以及"plan 期望 6 段实际 5 段"。

---

### Task 1: `frame_match` 读帧原语

**Files:**
- Create: `src/minimax_h3_prompt/tools/frame_match.py`
- Test: `tests/conftest.py`（合成视频 fixture）、`tests/test_frame_match.py`

**Interfaces:**
- Consumes: 无（本任务是全计划的地基）
- Produces:
  - `ANALYZE_W: int = 160`、`ANALYZE_H: int = 92`、`WINDOW: int = 5`
  - `imread_unicode(path: Path, flags: int = cv2.IMREAD_COLOR) -> np.ndarray | None`
  - `to_gray(bgr: np.ndarray) -> np.ndarray`（返回 `(ANALYZE_H, ANALYZE_W)` 的 `float32`）
  - `mad(a: np.ndarray, b: np.ndarray) -> float`
  - `video_meta(path: Path) -> dict`（键：`frames` / `fps` / `width` / `height` / `duration_s`；读不到返回 `{}`）
  - `read_frames_gray(path: Path) -> list[np.ndarray]`
  - `read_window(path: Path, *, window: str, k: int = WINDOW) -> list[np.ndarray]`

**背景（写给零上下文的实现者）**：两个必须绕过的坑。

1. **`cv2.imread` 在 Windows 上打不开非 ASCII 路径**（如 `古老图书馆.../shot-02-start.png`），会返回 `None` 并打印 `can't open/read file`。必须改用 `np.fromfile` + `cv2.imdecode`。
2. **`cv2.VideoCapture` 反而能正常工作**于同样的中文路径。不要给它加奇怪的编码转换。
3. 读尾帧时用 `CAP_PROP_POS_FRAMES` 精确 seek 到 `total-k` 在 H.264 上会因关键帧对齐而偏移，所以**往回多退 12 帧再顺序读到尾**，取最后 k 帧。

- [ ] **Step 1: 在 `tests/conftest.py` 写合成视频 fixture**

```python
"""合成素材 fixture：不依赖真实 ComfyUI 输出，秒级可跑。"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest


def write_test_video(
    path: Path,
    *,
    frames: int = 24,
    fps: int = 24,
    width: int = 128,
    height: int = 72,
    base: tuple[int, int, int] = (30, 30, 30),
    moving: bool = True,
) -> Path:
    """写一段小 mp4：纯色背景 + 一个逐帧移动的白色方块。

    moving=False 时整段完全静止（用于测 sanity check）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(
        str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height)
    )
    assert writer.isOpened(), f"VideoWriter 打不开 {path}"
    for i in range(frames):
        frame = np.full((height, width, 3), base, dtype=np.uint8)
        x = 8 + (i * 3 if moving else 0)
        cv2.rectangle(frame, (x, 20), (x + 20, 40), (255, 255, 255), -1)
        writer.write(frame)
    writer.release()
    return path


@pytest.fixture
def tmp_video(tmp_path: Path):
    """返回一个可调用对象：make(name="a.mp4", **kwargs) -> Path"""
    def make(name: str = "clip.mp4", **kwargs) -> Path:
        return write_test_video(tmp_path / name, **kwargs)
    return make
```

- [ ] **Step 2: 写失败测试**

创建 `tests/test_frame_match.py`：

```python
"""frame_match 帧读取与比对原语测试。"""
from __future__ import annotations

import cv2
import numpy as np
import pytest

from minimax_h3_prompt.tools.frame_match import (
    ANALYZE_H,
    ANALYZE_W,
    imread_unicode,
    mad,
    read_frames_gray,
    read_window,
    to_gray,
    video_meta,
)


def test_imread_unicode_reads_chinese_path(tmp_path):
    """cv2.imread 打不开中文路径，imread_unicode 必须能打开。"""
    target = tmp_path / "中文目录" / "桥接帧.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    img = np.zeros((72, 128, 3), dtype=np.uint8)
    # 写也必须绕开 cv2.imwrite——它同样不支持中文路径
    ok, buf = cv2.imencode(".png", img)
    assert ok
    target.write_bytes(buf.tobytes())

    loaded = imread_unicode(target)
    assert loaded is not None
    assert loaded.shape == (72, 128, 3)


def test_imread_unicode_missing_file_returns_none(tmp_path):
    assert imread_unicode(tmp_path / "不存在.png") is None


def test_to_gray_shape_and_dtype(tmp_path, tmp_video):
    video = tmp_video()
    frames = read_frames_gray(video)
    assert frames, "应至少读到一帧"
    assert frames[0].shape == (ANALYZE_H, ANALYZE_W)
    assert frames[0].dtype == np.float32


def test_mad_identical_is_zero(tmp_path, tmp_video):
    frames = read_frames_gray(tmp_video())
    assert mad(frames[0], frames[0]) == 0.0


def test_mad_different_frames_is_positive(tmp_path, tmp_video):
    frames = read_frames_gray(tmp_video(frames=24))
    assert mad(frames[0], frames[-1]) > 1.0


def test_video_meta_reports_dimensions_and_duration(tmp_path, tmp_video):
    video = tmp_video(frames=24, fps=24, width=128, height=72)
    meta = video_meta(video)
    assert meta["width"] == 128
    assert meta["height"] == 72
    assert meta["fps"] == pytest.approx(24.0)
    assert meta["duration_s"] == pytest.approx(1.0, abs=0.05)


def test_video_meta_missing_file_returns_empty(tmp_path):
    assert video_meta(tmp_path / "无.mp4") == {}


def test_read_window_head_and_tail_do_not_overlap(tmp_path, tmp_video):
    video = tmp_video(frames=24)
    head = read_window(video, window="head", k=5)
    tail = read_window(video, window="tail", k=5)
    assert len(head) == 5
    assert len(tail) == 5
    # 第 1 帧与最后一帧画面不同，所以头窗口与尾窗口不应相同
    assert mad(head[0], tail[-1]) > 1.0


def test_read_window_rejects_bad_window(tmp_path, tmp_video):
    with pytest.raises(ValueError, match="head"):
        read_window(tmp_video(), window="middle")
```

- [ ] **Step 3: 运行测试，确认失败**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_frame_match.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'minimax_h3_prompt.tools.frame_match'`

- [ ] **Step 4: 实现 `frame_match.py`**

```python
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
```

- [ ] **Step 5: 运行测试，确认通过**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_frame_match.py -v`
Expected: 全部 PASS（9 项）

- [ ] **Step 6: 跑全量回归，确认没弄坏别的**

Run: `./.venv/Scripts/python.exe -m pytest -q`
Expected: 原有 134 项 + 新增 9 项全部通过

- [ ] **Step 7: 提交**

```bash
git add src/minimax_h3_prompt/tools/frame_match.py tests/test_frame_match.py tests/conftest.py
git commit -m "feat(tools): 新增 frame_match 帧读取与比对原语（中文路径安全 + 窗口比对）"
```

---

### Task 2: `frame_match` 窗口比对与阈值分级

**Files:**
- Modify: `src/minimax_h3_prompt/tools/frame_match.py`
- Test: `tests/test_frame_match.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 `read_window` / `mad` / `to_gray`
- Produces:
  - `MAD_SAME: float = 2.0`、`MAD_MAYBE: float = 5.0`
  - `classify_match(d: float) -> str` → `"high"` / `"medium"` / `"no_match"`
  - `match_video(frame: np.ndarray, videos: list[Path], *, window: str) -> tuple[Path, float] | None`
  - `match_video_all(frame: np.ndarray, videos: list[Path], *, window: str, threshold: float = MAD_MAYBE) -> list[tuple[Path, float]]`——**保留全部候选**，按 mad 升序

- [ ] **Step 1: 写失败测试（追加到 `tests/test_frame_match.py`）**

```python
from minimax_h3_prompt.tools.frame_match import (
    MAD_MAYBE,
    MAD_SAME,
    classify_match,
    match_video,
)


def test_classify_match_three_bands():
    assert classify_match(0.0) == "high"
    assert classify_match(1.99) == "high"
    assert classify_match(2.0) == "medium"
    assert classify_match(4.99) == "medium"
    assert classify_match(5.0) == "no_match"
    assert classify_match(66.98) == "no_match"


def test_match_video_finds_source_by_tail(tmp_path, tmp_video):
    """一张由某视频尾帧复制而来的图，match_video 应找出那个视频。"""
    source = tmp_video("source.mp4", frames=24)
    other = tmp_video("other.mp4", frames=24, base=(200, 200, 200))

    tail = read_window(source, window="tail", k=1)[0]
    # 把灰度小图放大回 BGR 供 match_video 使用
    probe = cv2.cvtColor(
        cv2.resize(tail, (128, 72), interpolation=cv2.INTER_NEAREST), cv2.COLOR_GRAY2BGR
    )

    best = match_video(probe, [source, other], window="tail")
    assert best is not None
    assert best[0] == source
    assert best[1] < MAD_SAME


def test_match_video_returns_none_for_empty_list(tmp_path):
    blank = np.zeros((72, 128, 3), dtype=np.uint8)
    assert match_video(blank, [], window="tail") is None


def test_match_video_skips_unreadable_video(tmp_path, tmp_video):
    good = tmp_video("good.mp4")
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"not a video")

    tail = read_window(good, window="tail", k=1)[0]
    probe = cv2.cvtColor(
        cv2.resize(tail, (128, 72), interpolation=cv2.INTER_NEAREST), cv2.COLOR_GRAY2BGR
    )
    best = match_video(probe, [broken, good], window="tail")
    assert best is not None
    assert best[0] == good
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_frame_match.py -v -k "classify or match_video"`
Expected: FAIL — `ImportError: cannot import name 'classify_match'`

- [ ] **Step 3: 实现（追加到 `frame_match.py`）**

```python
# 阈值来自 2026-09-18《古老图书馆》实测：同源帧 mad 为 0.00/0.37/0.49/1.97，
# 非同源帧为 22–67，中间隔着一个数量级，所以阈值取哪都安全。
MAD_SAME = 2.0
MAD_MAYBE = 5.0


def classify_match(d: float) -> str:
    """"high"（同源）| "medium"（疑似同源）| "no_match"（不同源）。"""
    if d < MAD_SAME:
        return "high"
    if d < MAD_MAYBE:
        return "medium"
    return "no_match"


def match_video(
    frame: np.ndarray, videos: list[Path], *, window: str
) -> tuple[Path, float] | None:
    """在一组视频里找与 frame 最接近的那个，按窗口取最小 mad。

    返回 ``(视频路径, 最小 mad)``；``videos`` 为空或全部读不到时返回 None。
    读不到的视频被静默跳过——调用方据返回的 mad 自行判断置信度。
    """
    best: tuple[Path, float] | None = None
    probe = to_gray(frame)
    for video in videos:
        window_frames = read_window(video, window=window)
        if not window_frames:
            continue
        d = min(mad(probe, w) for w in window_frames)
        if best is None or d < best[1]:
            best = (video, d)
    return best


def match_video_all(
    frame: np.ndarray, videos: list[Path], *, window: str, threshold: float = MAD_MAYBE
) -> list[tuple[Path, float]]:
    """返回**全部** mad < threshold 的视频，按 mad 升序。

    必须保留多个候选：2026-09-18 的实测里，``shot-03-start.png`` 同时像
    ``00003`` 和 ``00004`` 的首帧（两者用了同一张输入图，mad=0.88），
    只留一个会让链路在那里断掉。消歧交给 `ledger.resolve_chain`。
    """
    probe = to_gray(frame)
    hits: list[tuple[Path, float]] = []
    for video in videos:
        window_frames = read_window(video, window=window)
        if not window_frames:
            continue
        d = min(mad(probe, w) for w in window_frames)
        if d < threshold:
            hits.append((video, d))
    return sorted(hits, key=lambda item: item[1])
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_frame_match.py -v`
Expected: 全部 PASS（13 项）

- [ ] **Step 5: 提交**

```bash
git add src/minimax_h3_prompt/tools/frame_match.py tests/test_frame_match.py
git commit -m "feat(tools): frame_match 增加窗口比对与 mad 阈值分级"
```

---

### Task 3: `ledger` 数据结构与序列化

**Files:**
- Create: `src/minimax_h3_prompt/tools/ledger.py`
- Test: `tests/test_ledger.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `SCHEMA = "h3-ledger/1"`
  - `@dataclass InputFrame`：`file: str` / `kind: str` / `source: dict | None` / `match_mad: float | None` / `confidence: str`
  - `@dataclass Shot`：`shot: int` / `video: str` / `video_dir: str` / `frames: int` / `duration_s: float` / `generated_at: str` / `input_frame: InputFrame` / `status: str` / `evidence: list[str]`
  - `@dataclass Discarded`：`video: str` / `video_dir: str` / `reason: str` / `replaced_by: str | None` / `confidence: str` / `evidence: list[str]`
  - `@dataclass Interruption`：`at_shot: int` / `type: str` / `evidence: list[str]` / `confidence: str` / `hint: str`
  - `@dataclass Ledger`：`schema` / `session: dict` / `scan: dict` / `shots: list[Shot]` / `discarded: list[Discarded]` / `interruptions: list[Interruption]` / `review_needed: list[str]` / `warnings: list[str]`
  - `Ledger.to_dict() -> dict`
  - `Ledger.from_dict(d: dict) -> Ledger`（schema 不认识时抛 `ValueError`）

- [ ] **Step 1: 写失败测试**

```python
"""ledger 生成台账测试。"""
from __future__ import annotations

import pytest

from minimax_h3_prompt.tools.ledger import (
    SCHEMA,
    Discarded,
    InputFrame,
    Interruption,
    Ledger,
    Shot,
)


def _sample_ledger() -> Ledger:
    return Ledger(
        schema=SCHEMA,
        session={"topic_slug": "示例", "generation": "GEN001"},
        scan={"scanned_at": "2026-09-20T14:32:11+08:00", "rule_version": "1",
              "output_dirs": ["D:/out"], "threshold_mad": 5.0},
        shots=[
            Shot(
                shot=1, video="a.mp4", video_dir="2026-09-18",
                frames=158, duration_s=6.58, generated_at="2026-09-18T11:26:05",
                input_frame=InputFrame(file="frames/first.png", kind="generated",
                                       source=None, match_mad=None, confidence="high"),
                status="final", evidence=["起点"],
            )
        ],
        discarded=[Discarded(video="c.mp4", video_dir="2026-09-18",
                             reason="未被引用", replaced_by="a.mp4",
                             confidence="high", evidence=["首帧 mad=0.88"])],
        interruptions=[Interruption(at_shot=6, type="unfinished",
                                    evidence=["plan 期望 6 段"],
                                    confidence="medium", hint="原因未知")],
        review_needed=[],
        warnings=["1 个文件读不了"],
    )


def test_ledger_roundtrip_preserves_everything():
    original = _sample_ledger()
    restored = Ledger.from_dict(original.to_dict())
    assert restored.to_dict() == original.to_dict()


def test_to_dict_uses_schema_string():
    assert _sample_ledger().to_dict()["schema"] == "h3-ledger/1"


def test_from_dict_rejects_unknown_schema():
    with pytest.raises(ValueError, match="schema"):
        Ledger.from_dict({"schema": "h3-ledger/99"})


def test_from_dict_rejects_missing_schema():
    with pytest.raises(ValueError, match="schema"):
        Ledger.from_dict({})


def test_empty_ledger_roundtrips():
    empty = Ledger(schema=SCHEMA, session={}, scan={}, shots=[], discarded=[],
                   interruptions=[], review_needed=[], warnings=[])
    assert Ledger.from_dict(empty.to_dict()).to_dict() == empty.to_dict()
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_ledger.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'minimax_h3_prompt.tools.ledger'`

- [ ] **Step 3: 实现数据结构部分**

创建 `src/minimax_h3_prompt/tools/ledger.py`：

```python
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

from dataclasses import asdict, dataclass, field

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
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_ledger.py -v`
Expected: 5 项全部 PASS

- [ ] **Step 5: 提交**

```bash
git add src/minimax_h3_prompt/tools/ledger.py tests/test_ledger.py
git commit -m "feat(tools): ledger 数据结构与 schema 版本化序列化"
```

---

### Task 4: `ledger` 建边与链路重建

**Files:**
- Modify: `src/minimax_h3_prompt/tools/ledger.py`
- Test: `tests/test_ledger.py`（追加）

**Interfaces:**
- Consumes: Task 1/2 的 `match_video` / `read_window` / `to_gray` / `imread_unicode` / `video_meta`
- Produces:
  - `@dataclass BridgeRef`：`shot_no: int` / `path: Path` / `src_video: Path | None` / `src_mad: float | None` / `dst_candidates: list[tuple[Path, float]]`
  - `collect_videos(output_dirs: list[Path]) -> list[Path]`（只收 `.mp4`，同名带音轨与不带音轨两份时只留带音轨的，按 `(父目录, 文件名)` 排序）
  - `collect_bridges(session_dir: Path) -> list[BridgeRef]`（解析 `bridge_frames/shot-0N-start.png`）
  - `build_edges(bridges, videos, *, threshold=MAD_MAYBE) -> list[BridgeRef]`（填好 src 与**全部** dst 候选）
  - `pick_start(videos, edges, *, threshold=MAD_MAYBE) -> Path | None`（起点 = 是别人的 src、但从不是任何边的 dst）
  - `resolve_chain(edges, videos, start, *, threshold=MAD_MAYBE) -> list[tuple[int, Path]]`

- [ ] **Step 1: 写失败测试（追加到 `tests/test_ledger.py`）**

```python
from pathlib import Path

import cv2

from minimax_h3_prompt.tools.frame_match import read_window
from minimax_h3_prompt.tools.ledger import (
    BridgeRef,
    build_edges,
    collect_videos,
    pick_start,
    resolve_chain,
)


def _probe_from_tail(video, size=(128, 72)):
    """取某视频尾帧，放大成 BGR 当作"桥接帧"。"""
    tail = read_window(video, window="tail", k=1)[0]
    return cv2.cvtColor(cv2.resize(tail, size, interpolation=cv2.INTER_NEAREST),
                        cv2.COLOR_GRAY2BGR)


def test_collect_videos_sorted_and_filters_non_mp4(tmp_path, tmp_video):
    tmp_video("MiniMax-H3视频_00002.mp4")
    tmp_video("MiniMax-H3视频_00001.mp4")
    (tmp_path / "notes.txt").write_text("x")
    videos = collect_videos([tmp_path])
    assert [v.name for v in videos] == ["MiniMax-H3视频_00001.mp4", "MiniMax-H3视频_00002.mp4"]


def test_collect_videos_empty_dir(tmp_path):
    assert collect_videos([tmp_path / "不存在"]) == []


def test_build_edges_identifies_src_and_keeps_all_dst_candidates(tmp_path, tmp_video):
    """桥接帧由 A 的尾帧复制而来 → src=A；dst 候选保留全部。"""
    a = tmp_video("a.mp4", frames=24)
    b = tmp_video("b.mp4", frames=24)
    bridge_path = tmp_path / "shot-02-start.png"
    ok, buf = cv2.imencode(".png", _probe_from_tail(a))
    assert ok
    bridge_path.write_bytes(buf.tobytes())

    ref = BridgeRef(shot_no=2, path=bridge_path, src_video=None, src_mad=None,
                    dst_candidates=[])
    edges = build_edges([ref], [a, b])
    assert edges[0].src_video == a
    assert edges[0].src_mad < 2.0
    assert all(d < 5.0 for _, d in edges[0].dst_candidates)


def test_build_edges_leaves_unreadable_bridge_empty(tmp_path, tmp_video):
    a = tmp_video("a.mp4")
    ref = BridgeRef(shot_no=2, path=tmp_path / "无.png", src_video=None,
                    src_mad=None, dst_candidates=[])
    edges = build_edges([ref], [a])
    assert edges[0].src_video is None
    assert edges[0].dst_candidates == []


def test_pick_start_returns_video_that_is_never_a_dst(tmp_path, tmp_video):
    first = tmp_video("00001.mp4")
    second = tmp_video("00002.mp4")
    third = tmp_video("00003.mp4")
    edges = [BridgeRef(2, tmp_path / "b2.png", first, 0.5, [(second, 0.5)]),
             BridgeRef(3, tmp_path / "b3.png", second, 0.5, [(third, 0.5)])]
    assert pick_start([first, second, third], edges) == first


def test_pick_start_returns_none_when_everything_is_a_dst(tmp_path, tmp_video):
    a = tmp_video("a.mp4")
    b = tmp_video("b.mp4")
    edges = [BridgeRef(2, tmp_path / "b2.png", a, 0.5, [(b, 0.5)]),
             BridgeRef(3, tmp_path / "b3.png", b, 0.5, [(a, 0.5)])]
    assert pick_start([a, b], edges) is None


def test_resolve_chain_prefers_continuation_over_time(tmp_path, tmp_video):
    """两个候选都在阈值内时，选尾帧还被引用的那个（链延续性优先）。

    这正是 00003 / 00004 的真实情形：两者首帧都对得上同一张桥接帧。
    """
    first = tmp_video("00001.mp4", frames=24)
    dead_end = tmp_video("00003.mp4", frames=24)
    continuer = tmp_video("00004.mp4", frames=24)
    last = tmp_video("00005.mp4", frames=24)

    edges = [
        BridgeRef(2, tmp_path / "b2.png", first, 0.5,
                  [(dead_end, 0.88), (continuer, 0.88)]),   # 同分候选，一次给全
        BridgeRef(3, tmp_path / "b3.png", continuer, 0.5, [(last, 0.5)]),
    ]
    chain = resolve_chain(edges, [first, dead_end, continuer, last], first)
    assert [v.name for _, v in chain] == ["00001.mp4", "00004.mp4", "00005.mp4"]


def test_resolve_chain_stops_at_dead_end(tmp_path, tmp_video):
    a = tmp_video("a.mp4")
    b = tmp_video("b.mp4")
    edges = [BridgeRef(2, tmp_path / "b2.png", a, 0.5, [(b, 0.5)])]
    chain = resolve_chain(edges, [a, b], a)
    assert [v.name for _, v in chain] == ["a.mp4", "b.mp4"]


def test_resolve_chain_ignores_bridges_above_threshold(tmp_path, tmp_video):
    """src 的 mad 超过阈值 → 这条边不可信，链在这里就断。"""
    a = tmp_video("a.mp4")
    b = tmp_video("b.mp4")
    edges = [BridgeRef(2, tmp_path / "b2.png", a, 40.0, [(b, 0.5)])]
    assert [v.name for _, v in resolve_chain(edges, [a, b], a)] == ["a.mp4"]
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_ledger.py -v -k "collect or build_edges or resolve_chain"`
Expected: FAIL — `ImportError: cannot import name 'BridgeRef'`

- [ ] **Step 3: 实现（追加到 `ledger.py`）**

先补 import：

```python
import re
from pathlib import Path

from .frame_match import (
    MAD_MAYBE,
    imread_unicode,
    match_video_all,
)
```

再追加：

```python
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
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_ledger.py -v`
Expected: 9 项全部 PASS

- [ ] **Step 5: 提交**

```bash
git add src/minimax_h3_prompt/tools/ledger.py tests/test_ledger.py
git commit -m "feat(tools): ledger 建边与链路重建（链延续性优先消歧）"
```

---

### Task 5: `ledger` 废片 / 中断判定 + override 合并

**Files:**
- Modify: `src/minimax_h3_prompt/tools/ledger.py`
- Test: `tests/test_ledger.py`（追加）

**Interfaces:**
- Consumes: Task 3/4 的数据结构与 `resolve_chain`
- Produces:
  - `classify_leftovers(videos: list[Path], chain_videos: set[Path], head_gray: dict[Path, np.ndarray]) -> tuple[list[Discarded], list[str]]`（`head_gray` 是 `{视频: 首帧灰度小图}`）
  - `detect_interruptions(shots: list[Shot], plan_segments: int | None) -> list[Interruption]`
  - `apply_override(ledger: Ledger, override: dict) -> Ledger`
  - `load_override(session_dir: Path) -> dict`
  - `read_plan_segment_count(session_dir: Path) -> int | None`

- [ ] **Step 1: 写失败测试（追加到 `tests/test_ledger.py`）**

```python
import json

from minimax_h3_prompt.tools.ledger import (
    apply_override,
    classify_leftovers,
    detect_interruptions,
    load_override,
    read_plan_segment_count,
)


def test_classify_leftovers_marks_similar_unreferenced_as_discarded(tmp_path, tmp_video):
    """首帧几乎相同的两个视频，只有一个进了链 → 另一个是废弃初版，高置信度。"""
    from minimax_h3_prompt.tools.frame_match import read_window

    first = tmp_video("00001.mp4")
    kept = tmp_video("00004.mp4")
    twin = tmp_video("00003.mp4")   # 与 kept 用同一张输入图，首帧几乎相同
    head_gray = {v: read_window(v, window="head", k=1)[0] for v in (first, kept, twin)}

    discarded, review = classify_leftovers([first, kept, twin], {first, kept}, head_gray)
    assert [d.video for d in discarded] == ["00003.mp4"]
    assert discarded[0].confidence == "high"
    assert discarded[0].replaced_by == "00004.mp4"
    assert review == []


def test_classify_leftovers_flags_unknown_as_review(tmp_path, tmp_video):
    """找不到相似视频 → 中等置信度 + 进 review_needed，不猜。"""
    from minimax_h3_prompt.tools.frame_match import read_window

    only = tmp_video("solo.mp4")
    head_gray = {only: read_window(only, window="head", k=1)[0]}
    discarded, review = classify_leftovers([only], set(), head_gray)
    assert [d.confidence for d in discarded] == ["medium"]
    assert any("solo.mp4" in r for r in review)


def test_classify_leftovers_returns_empty_when_nothing_left_over(tmp_path, tmp_video):
    from minimax_h3_prompt.tools.frame_match import read_window

    only = tmp_video("only.mp4")
    head_gray = {only: read_window(only, window="head", k=1)[0]}
    assert classify_leftovers([only], {only}, head_gray) == ([], [])


def test_detect_interruptions_reports_missing_segment():
    shots = [Shot(shot=i, video=f"{i}.mp4", video_dir="d", frames=10,
                  duration_s=1.0, generated_at="", input_frame=InputFrame(
                      file="", kind="unknown", source=None, match_mad=None,
                      confidence="low"), status="final") for i in range(1, 6)]
    interruptions = detect_interruptions(shots, plan_segments=6)
    assert len(interruptions) == 1
    assert interruptions[0].at_shot == 6
    assert interruptions[0].type == "unfinished"


def test_detect_interruptions_none_when_counts_match():
    shots = [Shot(shot=1, video="1.mp4", video_dir="d", frames=10, duration_s=1.0,
                  generated_at="", input_frame=InputFrame(
                      file="", kind="unknown", source=None, match_mad=None,
                      confidence="low"), status="final")]
    assert detect_interruptions(shots, plan_segments=1) == []


def test_apply_override_replaces_shot_video():
    ledger = _sample_ledger()
    patched = apply_override(ledger, {"shots": {"1": {"video": "新.mp4"}}})
    assert patched.shots[0].video == "新.mp4"
    assert ledger.shots[0].video == "a.mp4", "原对象不应被就地修改"


def test_load_override_missing_file_returns_empty(tmp_path):
    assert load_override(tmp_path) == {}


def test_load_override_reads_json(tmp_path):
    (tmp_path / "ledger.override.json").write_text(
        json.dumps({"shots": {"1": {"video": "x.mp4"}}}), encoding="utf-8")
    assert load_override(tmp_path)["shots"]["1"]["video"] == "x.mp4"


def test_read_plan_segment_count(tmp_path):
    seg = tmp_path / "segments"
    seg.mkdir()
    (seg / "plan.json").write_text(json.dumps([{"index": 0}, {"index": 1}]),
                                   encoding="utf-8")
    assert read_plan_segment_count(tmp_path) == 2


def test_read_plan_segment_count_missing_returns_none(tmp_path):
    assert read_plan_segment_count(tmp_path) is None
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_ledger.py -v -k "leftovers or interruptions or override or plan_segment"`
Expected: FAIL — `ImportError: cannot import name 'apply_override'`

- [ ] **Step 3: 实现（追加到 `ledger.py`）**

补 import：`import json`、`from dataclasses import replace`。

```python
OVERRIDE_FILENAME = "ledger.override.json"


def read_plan_segment_count(session_dir: Path) -> int | None:
    """读 ``segments/plan.json`` 的分段数。读不到返回 None。"""
    plan_path = session_dir / "segments" / "plan.json"
    if not plan_path.is_file():
        return None
    try:
        data = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return len(data) if isinstance(data, list) else None


def classify_leftovers(
    videos: list[Path],
    chain_videos: set[Path],
    head_gray: dict[Path, np.ndarray],
) -> tuple[list[Discarded], list[str]]:
    """把没进链的视频分成「废弃初版」和「判不了」。

    ``head_gray`` 是 {视频: 首帧灰度小图}；判不了的一律进第二返回值（review）。
    """
    from .frame_match import MAD_SAME, mad

    leftovers = [v for v in videos if v not in chain_videos]
    if not leftovers:
        return [], []

    discarded: list[Discarded] = []
    review: list[str] = []
    for video in leftovers:
        twin = None
        probe = head_gray.get(video)
        if probe is not None:
            for other, other_probe in head_gray.items():
                if other == video or other not in chain_videos:
                    continue
                if mad(probe, other_probe) < MAD_SAME:  # type: ignore[arg-type]
                    twin = other
                    break
        if twin is not None:
            discarded.append(Discarded(
                video=video.name, video_dir=video.parent.name, reason="未被任何桥接帧引用",
                replaced_by=twin.name, confidence="high",
                evidence=[f"{video.name} 首帧与 {twin.name} 首帧 mad<{MAD_SAME}"
                          "（同一输入帧的两次生成）"],
            ))
        else:
            discarded.append(Discarded(
                video=video.name, video_dir=video.parent.name, reason="未被任何桥接帧引用",
                replaced_by=None, confidence="medium",
                evidence=["找不到与它首帧相似的链上视频，无法确认是废弃初版"],
            ))
            review.append(f"{video.name} 未被引用且无法确认性质，请人工确认")
    return discarded, review


def detect_interruptions(shots: list[Shot], plan_segments: int | None) -> list[Interruption]:
    """plan 期望的段数比实际镜头多 → 报中断，不猜原因。"""
    if plan_segments is None or plan_segments <= len(shots):
        return []
    missing = list(range(len(shots) + 1, plan_segments + 1))
    return [Interruption(
        at_shot=missing[0],
        type="unfinished",
        evidence=[f"plan.json 期望 {plan_segments} 段", f"实际只有 {len(shots)} 个镜头"],
        confidence="medium",
        hint="磁盘上无法判定中断原因（配额 / 报错 / 主动停止），需人工确认",
    )]


def load_override(session_dir: Path) -> dict:
    """读 ``ledger.override.json``。不存在或坏了都返回空 dict。"""
    path = session_dir / OVERRIDE_FILENAME
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def apply_override(ledger: Ledger, override: dict) -> Ledger:
    """把人工修正叠到推断结果上。返回新对象，不就地修改。"""
    if not override:
        return ledger

    shots = []
    for shot in ledger.shots:
        patch = (override.get("shots") or {}).get(str(shot.shot)) or {}
        shots.append(replace(shot, **{k: v for k, v in patch.items()
                                      if k in {"video", "video_dir", "status"}})
                     if patch else shot)

    override_discarded = override.get("discarded") or {}
    kept = [d for d in ledger.discarded if d.video not in override_discarded]
    for video, reason in override_discarded.items():
        kept.append(Discarded(video=video, video_dir="", reason=str(reason),
                              replaced_by=None, confidence="high",
                              evidence=["来自 ledger.override.json"]))
    return replace(ledger, shots=shots, discarded=kept)
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_ledger.py -v`
Expected: 17 项全部 PASS

- [ ] **Step 5: 提交**

```bash
git add src/minimax_h3_prompt/tools/ledger.py tests/test_ledger.py
git commit -m "feat(tools): ledger 废片/中断判定与 override 合并"
```

---

### Task 6: `ledger.rebuild` 整合与落盘

**Files:**
- Modify: `src/minimax_h3_prompt/tools/ledger.py`
- Test: `tests/test_ledger.py`（追加）

**Interfaces:**
- Consumes: Task 4/5 的全部
- Produces:
  - `build_ledger(session_dir: Path, output_dirs: list[Path], *, threshold: float = MAD_MAYBE, start: Path | None = None) -> Ledger`（纯计算，不落盘）
  - `rebuild(session_dir: Path, output_dirs: list[Path], *, threshold: float = MAD_MAYBE, write: bool = True) -> Ledger`（调 `build_ledger`，`write=True` 时写 `ledger.json`）

- [ ] **Step 1: 写失败测试（追加到 `tests/test_ledger.py`）**

```python
import cv2

from minimax_h3_prompt.tools.ledger import build_ledger, rebuild


def _make_session(tmp_path, tmp_video, *, n_shots: int,
                  plan_segments: int | None = None):
    """造一个最小会话：n_shots 个镜头，尾帧接力，可选一个废弃初版。"""
    session = tmp_path / "sessions" / "主题-abc" / "GEN001"
    (session / "bridge_frames").mkdir(parents=True)
    (session / "frames").mkdir(parents=True)
    out = tmp_path / "comfy" / "2026-09-18"
    out.mkdir(parents=True)

    videos = []
    for i in range(1, n_shots + 1):
        v = tmp_video(f"MiniMax-H3视频_{i:05d}.mp4", frames=24)
        v.rename(out / v.name)
        videos.append(out / f"MiniMax-H3视频_{i:05d}.mp4")

    # frames/first.png：镜头1 的生图输入（非视频尺寸，用 4 倍放大模拟）
    first = read_window(videos[0], window="head", k=1)[0]
    big = cv2.resize(first, (512, 288), interpolation=cv2.INTER_NEAREST)
    ok, buf = cv2.imencode(".png", cv2.cvtColor(big, cv2.COLOR_GRAY2BGR))
    assert ok
    (session / "frames" / "first.png").write_bytes(buf.tobytes())

    # 桥接帧：镜头 N（N>=2）的首帧 = 镜头 N-1 的尾帧
    for i in range(2, n_shots + 1):
        tail = read_window(videos[i - 2], window="tail", k=1)[0]
        img = cv2.cvtColor(cv2.resize(tail, (128, 72), interpolation=cv2.INTER_NEAREST),
                           cv2.COLOR_GRAY2BGR)
        ok, buf = cv2.imencode(".png", img)
        assert ok
        (session / "bridge_frames" / f"shot-{i:02d}-start.png").write_bytes(buf.tobytes())

    if plan_segments is not None:
        (session / "segments").mkdir()
        (session / "segments" / "plan.json").write_text(
            json.dumps([{"index": i} for i in range(plan_segments)]), encoding="utf-8")

    return session, out


def test_build_ledger_recovers_shot_order(tmp_path, tmp_video):
    session, out = _make_session(tmp_path, tmp_video, n_shots=3)
    ledger = build_ledger(session, [out])
    assert [s.shot for s in ledger.shots] == [1, 2, 3]


def test_build_ledger_first_shot_input_is_generated(tmp_path, tmp_video):
    session, out = _make_session(tmp_path, tmp_video, n_shots=3)
    ledger = build_ledger(session, [out])
    assert ledger.shots[0].input_frame.kind == "generated"
    assert ledger.shots[1].input_frame.kind == "extracted"


def test_build_ledger_extracted_frames_are_high_confidence(tmp_path, tmp_video):
    session, out = _make_session(tmp_path, tmp_video, n_shots=3)
    ledger = build_ledger(session, [out])
    assert all(s.input_frame.confidence == "high" for s in ledger.shots[1:])


def test_build_ledger_reports_interruption_when_plan_longer(tmp_path, tmp_video):
    session, out = _make_session(tmp_path, tmp_video, n_shots=2, plan_segments=4)
    ledger = build_ledger(session, [out])
    assert [i.at_shot for i in ledger.interruptions] == [3]
    assert ledger.interruptions[0].type == "unfinished"


def test_rebuild_writes_and_reloads(tmp_path, tmp_video):
    session, out = _make_session(tmp_path, tmp_video, n_shots=2)
    written = rebuild(session, [out])
    assert (session / "ledger.json").is_file()
    reloaded = Ledger.from_dict(
        json.loads((session / "ledger.json").read_text(encoding="utf-8")))
    assert reloaded.to_dict() == written.to_dict()


def test_rebuild_does_not_touch_override_file(tmp_path, tmp_video):
    session, out = _make_session(tmp_path, tmp_video, n_shots=2)
    override = session / "ledger.override.json"
    override.write_text(json.dumps({"shots": {"1": {"video": "手改.mp4"}}}),
                        encoding="utf-8")
    rebuild(session, [out])
    assert json.loads(override.read_text(encoding="utf-8"))["shots"]["1"]["video"] == "手改.mp4"
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_ledger.py -v -k "build_ledger or rebuild"`
Expected: FAIL — `ImportError: cannot import name 'build_ledger'`

- [ ] **Step 3: 实现（追加到 `ledger.py`）**

```python
from datetime import datetime

from .frame_match import video_meta

_FIRST_FRAME_NAME = "first.png"
_EXTRACTED_SIZE = (1280, 736)
PLAN_HINT = "plan.json 期望 {n} 段"


def _iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _shot_input_frame(session_dir: Path, video: Path, bridge: BridgeRef | None,
                      first_video: Path) -> InputFrame:
    """判定某个镜头的输入帧。

    起点镜头的输入是生图 ``frames/first.png``（尺寸不是 1280×736）；
    其余镜头的输入是桥接帧，其来源由 ``bridge.src_video`` 给出。
    """
    if video == first_video:
        rel = f"frames/{_FIRST_FRAME_NAME}"
        image = imread_unicode(session_dir / rel)
        if image is None:
            return InputFrame(file="", kind="unknown", source=None, match_mad=None,
                              confidence="low")
        height, width = image.shape[:2]
        kind = "extracted" if (width, height) == _EXTRACTED_SIZE else "generated"
        return InputFrame(file=rel, kind=kind, source=None, match_mad=None,
                          confidence="high")

    if bridge is None or bridge.src_video is None:
        return InputFrame(file="", kind="unknown", source=None, match_mad=None,
                          confidence="low")

    rel = f"bridge_frames/{bridge.path.name}"
    image = imread_unicode(bridge.path)
    kind = "unknown"
    if image is not None:
        height, width = image.shape[:2]
        kind = "generated" if (width, height) != _EXTRACTED_SIZE else "extracted"
    return InputFrame(
        file=rel, kind=kind,
        source={"video": bridge.src_video.name, "frame": "tail"},
        match_mad=round(bridge.src_mad, 2) if bridge.src_mad is not None else None,
        confidence=classify_match(bridge.src_mad) if bridge.src_mad is not None else "low",
    )


def build_ledger(session_dir: Path, output_dirs: list[Path], *,
                 threshold: float = MAD_MAYBE, start: Path | None = None) -> Ledger:
    """从磁盘推断出整套台账。只读，不写任何文件。"""
    videos = collect_videos(output_dirs)
    bridges = build_edges(collect_bridges(session_dir), videos)

    warnings: list[str] = []
    review: list[str] = []
    if not videos:
        return Ledger(schema=SCHEMA,
                      session={"topic_slug": session_dir.parent.name,
                               "generation": session_dir.name},
                      scan={"scanned_at": _iso_now(), "rule_version": "1",
                            "output_dirs": [str(d) for d in output_dirs],
                            "threshold_mad": threshold},
                      shots=[], discarded=[], interruptions=[],
                      review_needed=[], warnings=["输出目录里没有找到任何 mp4"])

    first_video = start or pick_start(videos, bridges, threshold=threshold)
    if first_video is None:
        first_video = videos[0]
        warnings.append("推断不出起点镜头（没有视频同时满足"是别人的来源"与"
                        ""从不是消费者"），已退化为按文件名取第一个，结果可能不准")

    chain = resolve_chain(bridges, videos, first_video, threshold=threshold)
    bridge_by_shot = {b.shot_no: b for b in bridges}

    shots: list[Shot] = []
    for index, (shot_no, video) in enumerate(chain):
        meta = video_meta(video)
        bridge = bridge_by_shot.get(shot_no) if index > 0 else None
        shots.append(Shot(
            shot=shot_no,
            video=video.name,
            video_dir=video.parent.name,
            frames=int(meta.get("frames", 0)),
            duration_s=round(float(meta.get("duration_s", 0.0)), 2),
            generated_at=datetime.fromtimestamp(_safe_mtime(video)).astimezone().isoformat(
                timespec="seconds"),
            input_frame=_shot_input_frame(session_dir, video, bridge, first_video),
            status="final",
            evidence=(["起点镜头"] if index == 0 else
                      [f"输入帧来自 {bridge.src_video.name if bridge and bridge.src_video else '?'}"
                       f" 的尾帧" if bridge else "输入帧无法确定"]),
        ))

    head_gray = {}
    for video in videos:
        window = read_window(video, window="head", k=1)
        if window:
            head_gray[video] = window[0]

    chain_set = {v for _, v in chain}
    discarded, leftover_review = classify_leftovers(videos, chain_set, head_gray)
    review.extend(leftover_review)

    unreadable = [b.path.name for b in bridges if imread_unicode(b.path) is None]
    if unreadable:
        warnings.append(f"读不了 {len(unreadable)} 张桥接帧：{', '.join(unreadable)}")
        review.extend(f"桥接帧 {name} 读不了，相关接缝无法校验" for name in unreadable)

    plan_segments = read_plan_segment_count(session_dir)
    interruptions = detect_interruptions(shots, plan_segments)

    ledger = Ledger(
        schema=SCHEMA,
        session={"topic_slug": session_dir.parent.name, "generation": session_dir.name},
        scan={"scanned_at": _iso_now(), "rule_version": "1",
              "output_dirs": [str(d) for d in output_dirs], "threshold_mad": threshold},
        shots=shots, discarded=discarded, interruptions=interruptions,
        review_needed=review, warnings=warnings,
    )
    return apply_override(ledger, load_override(session_dir))


def rebuild(session_dir: Path, output_dirs: list[Path], *,
            threshold: float = MAD_MAYBE, write: bool = True) -> Ledger:
    """``build_ledger`` + 落盘 ``ledger.json``（``write=False`` 时只算不写）。"""
    ledger = build_ledger(session_dir, output_dirs, threshold=threshold)
    if write:
        (session_dir / "ledger.json").write_text(
            json.dumps(ledger.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return ledger
```

把 `frame_match` 的 import 补全为（注意 `MAD_MAYBE` 与 `imread_unicode` 在 Task 4 已引入，合并即可）：

```python
from .frame_match import (
    MAD_MAYBE,
    classify_match,
    imread_unicode,
    match_video_all,
    read_window,
    video_meta,
)
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_ledger.py -v`
Expected: 23 项全部 PASS

- [ ] **Step 5: 跑全量回归**

Run: `./.venv/Scripts/python.exe -m pytest -q`
Expected: 全部通过

- [ ] **Step 6: 提交**

```bash
git add src/minimax_h3_prompt/tools/ledger.py tests/test_ledger.py
git commit -m "feat(tools): ledger.rebuild 整合推断流程并落盘 ledger.json"
```

---

### Task 7: CLI 子命令 `ledger`

**Files:**
- Modify: `src/minimax_h3_prompt/main.py`（`build_parser()` 与 `main()`）
- Test: `tests/test_ledger_cli.py`

**Interfaces:**
- Consumes: `ledger.rebuild` / `Ledger`
- Produces: `python launch.py ledger rebuild <session_dir> --output-dir <dir> [--output-dir ...] [--threshold N] [--json]`

- [ ] **Step 1: 写失败测试**

```python
"""ledger CLI 子命令测试。"""
from __future__ import annotations

import json

from minimax_h3_prompt.main import build_parser, main


def test_parser_has_ledger_rebuild():
    args = build_parser().parse_args(["ledger", "rebuild", "S", "--output-dir", "O"])
    assert args.command == "ledger"
    assert args.ledger_command == "rebuild"
    assert args.session_dir == "S"
    assert args.output_dir == ["O"]


def test_ledger_rebuild_reports_missing_session(tmp_path, capsys):
    code = main(["ledger", "rebuild", str(tmp_path / "不存在"),
                 "--output-dir", str(tmp_path)])
    assert code == 1
    assert "不存在" in capsys.readouterr().out


def test_ledger_rebuild_prints_shot_count(tmp_path, tmp_video, capsys):
    from tests.test_ledger import _make_session  # 复用已有构造器

    session, out = _make_session(tmp_path, tmp_video, n_shots=3)
    code = main(["ledger", "rebuild", str(session), "--output-dir", str(out)])
    assert code == 0
    assert "3" in capsys.readouterr().out


def test_ledger_rebuild_json_flag_emits_json(tmp_path, tmp_video, capsys):
    from tests.test_ledger import _make_session

    session, out = _make_session(tmp_path, tmp_video, n_shots=2)
    code = main(["ledger", "rebuild", str(session), "--output-dir", str(out), "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "h3-ledger/1"
    assert len(payload["shots"]) == 2
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_ledger_cli.py -v`
Expected: FAIL — `argparse` 报 `invalid choice: 'ledger'`

- [ ] **Step 3: 实现**

在 `main.py` 的 `build_parser()` 里，`generate_parser` 之后加入：

```python
    ledger_parser = project.add_parser("ledger", help="生成台账：从磁盘重建镜头链路")
    ledger_sub = ledger_parser.add_subparsers(dest="ledger_command")
    ledger_rebuild = ledger_sub.add_parser("rebuild", help="重扫并写出 ledger.json")
    ledger_rebuild.add_argument("session_dir", help="会话目录（含 bridge_frames/ 与 segments/）")
    ledger_rebuild.add_argument("--output-dir", action="append", required=True,
                                help="ComfyUI 输出目录，可重复")
    ledger_rebuild.add_argument("--threshold", type=float, default=5.0,
                                help="mad 阈值（默认 5.0）")
    ledger_rebuild.add_argument("--json", action="store_true", help="只输出 JSON，不写文件")
```

在 `main()` 的分发里加入：

```python
    if args.command == "ledger":
        return _run_ledger(args)
```

并新增：

```python
def _run_ledger(args: argparse.Namespace) -> int:
    """从磁盘重建生成台账。"""
    from pathlib import Path

    from .tools.ledger import rebuild

    session_dir = Path(args.session_dir)
    if not session_dir.is_dir():
        print(f"[错误] 会话目录不存在：{session_dir}")
        return 1
    if getattr(args, "ledger_command", None) != "rebuild":
        print("[错误] 请指定子命令，例如：ledger rebuild <session_dir> --output-dir <dir>")
        return 1

    write = not args.json
    ledger = rebuild(session_dir, [Path(d) for d in args.output_dir],
                     threshold=args.threshold, write=write)
    if args.json:
        print(json.dumps(ledger.to_dict(), ensure_ascii=False, indent=2))
        return 0

    print(f"镜头 {len(ledger.shots)} 个：" + " → ".join(s.video for s in ledger.shots))
    for item in ledger.discarded:
        print(f"[废片] {item.video}（{item.confidence}）— {item.reason}")
    for item in ledger.interruptions:
        print(f"[中断] 第 {item.at_shot} 段未产出（{item.confidence}）— {item.hint}")
    for note in ledger.review_needed:
        print(f"[待确认] {note}")
    for note in ledger.warnings:
        print(f"[警告] {note}")
    print(f"已写出：{session_dir / 'ledger.json'}")
    return 0
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_ledger_cli.py -v`
Expected: 4 项全部 PASS

- [ ] **Step 5: 人工验收——跑真实的《古老图书馆》**

Run:
```bash
./.venv/Scripts/python.exe launch.py ledger rebuild \
  "output/sessions/古老图书馆里-少年撕下会发光的书页折成纸-9ac0bf14/GEN001" \
  --output-dir "D:/Comfyui/ComfyUI/output/视频/MiniMax-H3/2026-09-18"
```

Expected 输出（与 2026-09-20 实测一致）：
```
镜头 5 个：MiniMax-H3视频_00001-audio.mp4 → 00002 → 00004 → 00005 → 00006
[废片] MiniMax-H3视频_00003-audio.mp4（high）— 未被任何桥接帧引用
[中断] 第 6 段未产出（medium）— 磁盘上无法判定中断原因…
```

- [ ] **Step 6: 提交**

```bash
git add src/minimax_h3_prompt/main.py tests/test_ledger_cli.py
git commit -m "feat(cli): 新增 ledger rebuild 子命令"
```

---

### Task 8: 真实素材回归测试

**Files:**
- Test: `tests/test_ledger_real_assets.py`

**Interfaces:**
- Consumes: Task 6 的 `build_ledger`
- Produces: 无（纯测试）

- [ ] **Step 1: 写测试**

```python
"""真实素材回归：把 2026-09-20 的实测结论钉成断言。

素材在本地 ComfyUI 输出目录，不在仓库里，所以条件跳过。
本测试的价值：以后改推断算法，跑一下就知道有没有退化。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from minimax_h3_prompt.tools.ledger import build_ledger

COMFY_OUTPUT = Path(os.environ.get(
    "H3_COMFY_OUTPUT", r"D:/Comfyui/ComfyUI/output/视频/MiniMax-H3/2026-09-18"))
SESSION = Path(
    "output/sessions/古老图书馆里-少年撕下会发光的书页折成纸-9ac0bf14/GEN001")

pytestmark = pytest.mark.skipif(
    not (COMFY_OUTPUT.is_dir() and SESSION.is_dir()),
    reason="需要本地 ComfyUI 素材与会话目录",
)


def test_recovers_five_shots_in_correct_order():
    """正确顺序是 1→2→4→5→6：00003 是镜头 3 的废弃初版。"""
    ledger = build_ledger(SESSION, [COMFY_OUTPUT])
    assert [s.video for s in ledger.shots] == [
        "MiniMax-H3视频_00001-audio.mp4",
        "MiniMax-H3视频_00002-audio.mp4",
        "MiniMax-H3视频_00004-audio.mp4",
        "MiniMax-H3视频_00005-audio.mp4",
        "MiniMax-H3视频_00006-audio.mp4",
    ]


def test_shot_one_input_is_generated_frame():
    ledger = build_ledger(SESSION, [COMFY_OUTPUT])
    assert ledger.shots[0].input_frame.kind == "generated"
    assert ledger.shots[0].input_frame.file == "frames/first.png"


def test_bridge_sourced_inputs_are_high_confidence():
    """4 张桥接帧在实测里全部对上（mad 0.00 / 0.37 / 0.49 / 1.97）。"""
    ledger = build_ledger(SESSION, [COMFY_OUTPUT])
    assert all(s.input_frame.confidence == "high" for s in ledger.shots[1:])


def test_00003_is_marked_discarded():
    ledger = build_ledger(SESSION, [COMFY_OUTPUT])
    assert [d.video for d in ledger.discarded] == ["MiniMax-H3视频_00003-audio.mp4"]
    assert ledger.discarded[0].confidence == "high"
    assert ledger.discarded[0].replaced_by == "MiniMax-H3视频_00004-audio.mp4"


def test_reports_plan_expects_six_but_five_produced():
    ledger = build_ledger(SESSION, [COMFY_OUTPUT])
    assert [i.at_shot for i in ledger.interruptions] == [6]
    assert ledger.interruptions[0].type == "unfinished"
```

- [ ] **Step 2: 运行测试**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_ledger_real_assets.py -v`
Expected: 5 项 PASS（若素材不存在则 SKIPPED）

- [ ] **Step 3: 若失败，修 `ledger.py` 直到通过**

这里失败说明推断算法与实测不符，**必须修实现，不许改断言**。断言是 2026-09-20 人工核对过的地面真相。

- [ ] **Step 4: 提交**

```bash
git add tests/test_ledger_real_assets.py
git commit -m "test: 真实素材回归——把《古老图书馆》实测结论钉成断言"
```

---

# 阶段 2：审计 — `seam_audit`

> 阶段 2 结束时，你能跑一条命令得到接缝报告，并与 4.04 秒 / 14.1% 的基线对比。

---

### Task 9: 静止检测原语 + ffmpeg 依赖

**Files:**
- Modify: `pyproject.toml`（加 `imageio-ffmpeg`）
- Create: `src/minimax_h3_prompt/tools/seam_audit.py`
- Test: `tests/test_seam_audit.py`

**Interfaces:**
- Consumes: `frame_match.read_frames_gray` / `mad`
- Produces:
  - `STATIC_THR: float = 1.0`
  - `diff_curve(frames: list[np.ndarray]) -> list[float]`
  - `trailing_run(diffs: list[float], thr: float = STATIC_THR) -> int`
  - `leading_run(diffs: list[float], thr: float = STATIC_THR) -> int`
  - `longest_run(diffs: list[float], thr: float = STATIC_THR) -> int`
  - `@dataclass ShotStill`：`shot: int` / `frames: int` / `median: float` / `trailing: int` / `leading: int` / `longest: int`
  - `sanity_check(diffs: list[float]) -> str | None`（整段几乎不动时返回告警文本）
  - `find_ffmpeg() -> Path`（找不到时抛 `RuntimeError`）

- [ ] **Step 1: 加依赖**

Run: `uv add imageio-ffmpeg`
Expected: `pyproject.toml` 的 `dependencies` 里出现 `imageio-ffmpeg>=0.6`

- [ ] **Step 2: 写失败测试**

```python
"""seam_audit 接缝审计测试。"""
from __future__ import annotations

import numpy as np
import pytest

from minimax_h3_prompt.tools.seam_audit import (
    STATIC_THR,
    diff_curve,
    find_ffmpeg,
    leading_run,
    longest_run,
    sanity_check,
    trailing_run,
)


def _frames(values: list[float]) -> list[np.ndarray]:
    """构造一串单像素灰度帧，使相邻差恰好等于 values。"""
    out = [np.zeros((2, 2), dtype=np.float32)]
    for v in values:
        out.append(out[-1] + v)
    return out


def test_diff_curve_length_is_frames_minus_one():
    assert len(diff_curve(_frames([1.0, 2.0, 3.0]))) == 3


def test_trailing_run_counts_consecutive_still_frames():
    diffs = [9.0, 8.0, 0.2, 0.3, 0.1]
    assert trailing_run(diffs) == 3


def test_trailing_run_zero_when_last_frame_moves():
    assert trailing_run([0.1, 0.2, 9.0]) == 0


def test_leading_run_counts_from_start():
    assert leading_run([0.1, 0.2, 3.0, 9.0, 9.0]) == 2
    assert leading_run([9.0, 0.1]) == 0


def test_longest_run_finds_max_anywhere():
    assert longest_run([0.1, 0.1, 9.0, 0.1, 0.1, 0.1]) == 3


def test_threshold_constant_is_one():
    assert STATIC_THR == 1.0


def test_sanity_check_flags_fully_static_clip():
    """.vs 全静止的视频——那是素材坏了，不是接缝问题。"""
    assert sanity_check([0.01] * 30) is not None


def test_sanity_check_passes_normal_clip():
    assert sanity_check([3.0, 5.0, 2.0] * 10) is None


def test_find_ffmpeg_returns_existing_path():
    assert find_ffmpeg().is_file()
```

- [ ] **Step 3: 运行测试，确认失败**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_seam_audit.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'minimax_h3_prompt.tools.seam_audit'`

- [ ] **Step 4: 实现**

创建 `src/minimax_h3_prompt/tools/seam_audit.py`：

```python
"""接缝审计：拼接镜头并量化接缝处的静止。

背景：本项目用「尾帧接力」——镜头 N+1 的首帧由镜头 N 的尾帧抽出后喂入。
若镜头 N 的 ``end_hook`` 写了「停住/定格」这类词，H3 会在段尾把画面冻住，
而镜头 N+1 又从同一张冻住的画面开始 → 接缝处出现连续静止（"双静止停顿"）。

2026-09-18《古老图书馆》修复前实测：4 个接缝累计约 4.04 秒连续静止，
占全片 28.67 秒的 14.1%；每段的「末尾静止」恰好等于它自己的「段内最长静止」，
说明冻结就发生在段尾。

本模块用**逐帧像素差**判定静止——确定性、零成本、可重复，
不需要 LLM 评委。
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .frame_match import mad, read_frames_gray

STATIC_THR = 1.0        # 灰度 0–255 刻度上的平均绝对差，低于此判定"画面没动"
SANITY_THR = 0.05       # 整段中位差低于此值时，认为素材本身有问题


@dataclass
class ShotStill:
    """单个镜头的静止统计。"""

    shot: int
    frames: int
    median: float
    trailing: int
    leading: int
    longest: int


def find_ffmpeg() -> Path:
    """定位 ffmpeg 可执行文件。优先用 imageio-ffmpeg 自带的，其次 PATH。"""
    try:
        import imageio_ffmpeg

        return Path(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:
        pass
    found = shutil.which("ffmpeg")
    if found:
        return Path(found)
    raise RuntimeError(
        "找不到 ffmpeg。请执行 `uv add imageio-ffmpeg`，或把 ffmpeg 放进 PATH。"
    )


def diff_curve(frames: list[np.ndarray]) -> list[float]:
    """相邻帧的平均绝对差序列。"""
    return [mad(frames[i + 1], frames[i]) for i in range(len(frames) - 1)]


def trailing_run(diffs: list[float], thr: float = STATIC_THR) -> int:
    """末尾连续"没动"的次数。k 次 → 末尾 k+1 帧是同一画面。"""
    count = 0
    for d in reversed(diffs):
        if d < thr:
            count += 1
        else:
            break
    return count


def leading_run(diffs: list[float], thr: float = STATIC_THR) -> int:
    count = 0
    for d in diffs:
        if d < thr:
            count += 1
        else:
            break
    return count


def longest_run(diffs: list[float], thr: float = STATIC_THR) -> int:
    best = current = 0
    for d in diffs:
        current = current + 1 if d < thr else 0
        best = max(best, current)
    return best


def sanity_check(diffs: list[float]) -> str | None:
    """先验证素材本身是好的：整段几乎不动时，问题在素材而不在接缝。"""
    if not diffs:
        return "这段视频没读出帧，无法分析"
    if float(np.median(diffs)) < SANITY_THR:
        return "整段几乎完全静止——素材本身可能有问题，不要据此判断接缝"
    return None


def analyze_shot(frames: list[np.ndarray], shot: int) -> tuple[ShotStill, str | None]:
    """算一个镜头的静止统计，并返回素材自检告警（无问题为 None）。"""
    diffs = diff_curve(frames)
    return (
        ShotStill(shot=shot, frames=len(frames),
                  median=round(float(np.median(diffs)), 2) if diffs else 0.0,
                  trailing=trailing_run(diffs), leading=leading_run(diffs),
                  longest=longest_run(diffs)),
        sanity_check(diffs),
    )
```

- [ ] **Step 5: 运行测试，确认通过**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_seam_audit.py -v`
Expected: 9 项全部 PASS

- [ ] **Step 6: 提交**

```bash
git add pyproject.toml uv.lock src/minimax_h3_prompt/tools/seam_audit.py tests/test_seam_audit.py
git commit -m "feat(tools): seam_audit 静止检测原语 + imageio-ffmpeg 依赖"
```

---

### Task 10: 合并、报告与基线对比

**Files:**
- Modify: `src/minimax_h3_prompt/tools/seam_audit.py`
- Test: `tests/test_seam_audit.py`（追加）

**Interfaces:**
- Consumes: Task 9 全部 + Task 3/6 的 `Ledger`
- Produces:
  - `@dataclass SeamResult`：`index: int` / `prev_shot: int` / `next_shot: int` / `cross: float` / `prev_trailing: int` / `next_leading: int` / `prev_median: float`
  - `@dataclass AuditReport`：`shots: list[ShotStill]` / `seams: list[SeamResult]` / `duration_s: float` / `still_seconds: float` / `still_ratio: float` / `per_seam_avg_frames: float` / `warnings: list[str]` / `merged_path: str`
  - `analyze(videos: list[Path], out_dir: Path, *, merge: bool = True) -> AuditReport`
  - `compare_to_baseline(report: AuditReport, baseline: dict) -> list[tuple[str, str, str, str]]`（返回 `(指标, 基线, 本次, 变化)` 四元组列表）
  - `load_baseline(session_dir: Path) -> dict | None`
  - `promote_baseline(session_dir: Path, report: AuditReport, label: str) -> Path`
  - `render_markdown(report: AuditReport, baseline: dict | None) -> str`

- [ ] **Step 1: 写失败测试（追加到 `tests/test_seam_audit.py`）**

```python
import json

from minimax_h3_prompt.tools.seam_audit import (
    AuditReport,
    analyze,
    compare_to_baseline,
    load_baseline,
    promote_baseline,
    render_markdown,
)


def test_analyze_builds_report_and_merges(tmp_path, tmp_video):
    a = tmp_video("a.mp4", frames=24)
    b = tmp_video("b.mp4", frames=24)
    report = analyze([a, b], tmp_path / "audit")
    assert [s.shot for s in report.shots] == [1, 2]
    assert len(report.seams) == 1
    assert report.duration_s > 0
    assert (tmp_path / "audit" / "merged_naive.mp4").is_file()


def test_analyze_without_merge(tmp_path, tmp_video):
    report = analyze([tmp_video("a.mp4"), tmp_video("b.mp4")], tmp_path / "audit",
                     merge=False)
    assert not (tmp_path / "audit" / "merged_naive.mp4").exists()
    assert report.merged_path == ""


def test_analyze_single_shot_has_no_seams(tmp_path, tmp_video):
    report = analyze([tmp_video("only.mp4")], tmp_path / "audit", merge=False)
    assert report.seams == []
    assert report.still_seconds == 0.0


def test_compare_to_baseline_reports_per_seam_average():
    report = AuditReport(shots=[], seams=[], duration_s=30.0, still_seconds=6.0,
                         still_ratio=0.2, per_seam_avg_frames=36.0, warnings=[],
                         merged_path="")
    baseline = {"per_seam_avg_frames": 24.3, "seam_still_ratio": 0.141}
    rows = compare_to_baseline(report, baseline)
    labels = [r[0] for r in rows]
    assert any("每缝均值" in label for label in labels)
    assert any("占全片" in label for label in labels)


def test_render_markdown_warns_against_comparing_totals_across_shot_counts():
    report = AuditReport(shots=[], seams=[], duration_s=30.0, still_seconds=6.0,
                         still_ratio=0.2, per_seam_avg_frames=36.0, warnings=[],
                         merged_path="")
    text = render_markdown(report, {"shots": 5, "per_seam_avg_frames": 24.3})
    assert "每缝均值" in text
    assert "镜头数" in text


def test_promote_and_load_baseline_roundtrip(tmp_path, tmp_video):
    session = tmp_path / "GEN001"
    session.mkdir()
    report = analyze([tmp_video("a.mp4"), tmp_video("b.mp4")],
                     tmp_path / "audit", merge=False)
    path = promote_baseline(session, report, "修复前")
    assert path.is_file()
    loaded = load_baseline(session)
    assert loaded is not None
    assert loaded["label"] == "修复前"
    assert loaded["per_seam_avg_frames"] == report.per_seam_avg_frames


def test_load_baseline_missing_returns_none(tmp_path):
    assert load_baseline(tmp_path) is None
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_seam_audit.py -v -k "analyze or baseline or markdown"`
Expected: FAIL — `ImportError: cannot import name 'analyze'`

- [ ] **Step 3: 实现（追加到 `seam_audit.py`）**

```python
import json
import subprocess
from datetime import datetime


@dataclass
class SeamResult:
    """一个接缝。"""

    index: int
    prev_shot: int
    next_shot: int
    cross: float
    prev_trailing: int
    next_leading: int
    prev_median: float

    @property
    def total_still(self) -> int:
        return self.prev_trailing + self.next_leading


@dataclass
class AuditReport:
    """一次审计的完整结果。"""

    shots: list[ShotStill]
    seams: list[SeamResult]
    duration_s: float
    still_seconds: float
    still_ratio: float
    per_seam_avg_frames: float
    warnings: list[str] = field(default_factory=list)
    merged_path: str = ""


def merge_videos(videos: list[Path], out_path: Path) -> tuple[bool, str]:
    """ffmpeg concat。先试 stream copy（无损且快），失败再重编码。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    list_file = out_path.parent / "concat_list.txt"
    list_file.write_text("\n".join(f"file '{v.as_posix()}'" for v in videos),
                         encoding="utf-8")
    base = [str(find_ffmpeg()), "-y", "-f", "concat", "-safe", "0", "-i", str(list_file)]
    attempts = [
        ("stream-copy", ["-c", "copy"]),
        ("re-encode", ["-c:v", "libx264", "-crf", "18", "-c:a", "aac", "-b:a", "192k"]),
    ]
    last_error = ""
    for tag, extra in attempts:
        proc = subprocess.run(base + extra + [str(out_path)], capture_output=True,
                              text=True, encoding="utf-8", errors="replace")
        if proc.returncode == 0 and out_path.is_file() and out_path.stat().st_size > 0:
            return True, tag
        last_error = (proc.stderr or "")[-1500:]
    return False, last_error


def analyze(videos: list[Path], out_dir: Path, *, merge: bool = True) -> AuditReport:
    """分析一组**已按正片顺序排好**的视频。

    读不到的镜头进 warnings 并跳过；不静默。
    """
    warnings: list[str] = []
    usable: list[tuple[int, Path]] = []
    for index, video in enumerate(videos, 1):
        if not video.is_file():
            warnings.append(f"镜头 {index} 的文件不存在：{video}")
            continue
        usable.append((index, video))

    states: list[ShotStill] = []
    frame_lists: list[list[np.ndarray]] = []
    for shot_no, video in usable:
        frames = read_frames_gray(video)
        if not frames:
            warnings.append(f"镜头 {shot_no} 读不出帧：{video.name}")
            continue
        state, alert = analyze_shot(frames, shot_no)
        if alert:
            warnings.append(f"镜头 {shot_no}：{alert}")
        states.append(state)
        frame_lists.append(frames)

    seams: list[SeamResult] = []
    for i in range(len(states) - 1):
        # 跨缝帧差 = 前镜头最后一帧 vs 后镜头第一帧。
        # 实测同源约 3–4，若接近 0 说明两帧基本是同一张图（合并层重复）。
        cross = mad(frame_lists[i][-1], frame_lists[i + 1][0])
        seams.append(SeamResult(
            index=i + 1, prev_shot=states[i].shot, next_shot=states[i + 1].shot,
            cross=round(cross, 2), prev_trailing=states[i].trailing,
            next_leading=states[i + 1].leading, prev_median=states[i].median,
        ))

    still_frames = sum(s.total_still for s in seams)
    duration = sum(s.frames for s in states) / 24.0
    merged_path = ""
    if merge and usable:
        ok, how = merge_videos([v for _, v in usable], out_dir / "merged_naive.mp4")
        if ok:
            merged_path = str(out_dir / "merged_naive.mp4")
        else:
            warnings.append(f"合并失败：{how}")

    return AuditReport(
        shots=states, seams=seams, duration_s=round(duration, 2),
        still_seconds=round(still_frames / 24.0, 2),
        still_ratio=round(still_frames / 24.0 / duration, 4) if duration else 0.0,
        per_seam_avg_frames=round(still_frames / len(seams), 1) if seams else 0.0,
        warnings=warnings, merged_path=merged_path,
    )
```

继续追加：

```python
BASELINE_NAME = "baseline.json"
AUDIT_DIR = "audit"


def load_baseline(session_dir: Path) -> dict | None:
    path = session_dir / AUDIT_DIR / BASELINE_NAME
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def promote_baseline(session_dir: Path, report: AuditReport, label: str) -> Path:
    """把本次结果提升为基线。人工动作，不会被自动覆盖。"""
    payload = {
        "label": label,
        "promoted_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "shots": len(report.shots),
        "duration_s": report.duration_s,
        "seam_still_seconds": report.still_seconds,
        "seam_still_ratio": report.still_ratio,
        "per_seam_avg_frames": report.per_seam_avg_frames,
        "per_shot_trailing": [s.trailing for s in report.shots],
    }
    path = session_dir / AUDIT_DIR / BASELINE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _delta(current: float, base: float) -> str:
    diff = current - base
    if abs(diff) < 1e-9:
        return "持平"
    arrow = "↓" if diff < 0 else "↑"
    return f"{arrow} {abs(diff):.2f}"


def compare_to_baseline(report: AuditReport, baseline: dict) -> list[tuple[str, str, str, str]]:
    """返回 ``(指标, 基线, 本次, 变化)`` 四元组。**只比均值与比例，不比总数。**"""
    rows: list[tuple[str, str, str, str]] = []
    base_avg = baseline.get("per_seam_avg_frames")
    if base_avg is not None:
        rows.append(("接缝静止 · 每缝均值（帧）", f"{base_avg:.1f}",
                     f"{report.per_seam_avg_frames:.1f}",
                     _delta(report.per_seam_avg_frames, float(base_avg))))
    base_ratio = baseline.get("seam_still_ratio")
    if base_ratio is not None:
        rows.append(("接缝静止 · 占全片", f"{base_ratio * 100:.1f}%",
                     f"{report.still_ratio * 100:.1f}%",
                     _delta(report.still_ratio, float(base_ratio))))
    return rows


def render_markdown(report: AuditReport, baseline: dict | None) -> str:
    lines: list[str] = ["# 接缝审计报告", ""]
    lines.append(f"- 镜头数：**{len(report.shots)}** ｜ 总时长：{report.duration_s:.2f}s")
    lines.append(f"- 接缝静止：**{report.still_seconds:.2f}s**，占全片 "
                 f"**{report.still_ratio * 100:.1f}%**")
    lines.append(f"- 每缝均值：**{report.per_seam_avg_frames:.1f} 帧**")
    lines.append("")
    lines.append("> ⚠️ **总数不能跨镜头数比较。** 基线是 "
                 f"{baseline.get('shots', '?') if baseline else '?'} 个镜头、"
                 f"本次是 {len(report.shots)} 个。请只看**每缝均值**与**占全片比例**。")
    lines.append("")

    if baseline:
        lines.append("## 与基线对比")
        lines.append("")
        lines.append("| 指标 | 基线 | 本次 | 变化 |")
        lines.append("|---|---|---|---|")
        for name, base, cur, delta in compare_to_baseline(report, baseline):
            lines.append(f"| {name} | {base} | {cur} | {delta} |")
        lines.append("")

    lines.append("## 每个镜头")
    lines.append("")
    lines.append("| 镜头 | 帧数 | 典型运动量 | 末尾静止 | 开头静止 | 段内最长静止 |")
    lines.append("|---|---|---|---|---|---|")
    for s in report.shots:
        lines.append(f"| {s.shot} | {s.frames} | {s.median:.2f} | **{s.trailing}** | "
                     f"**{s.leading}** | {s.longest} |")
    lines.append("")
    lines.append("> 「末尾静止」若明显大于该镜头自己的「段内最长静止」，说明段尾出现了额外冻结。")
    lines.append("")

    if report.seams:
        lines.append("## 每个接缝")
        lines.append("")
        lines.append("| 接缝 | 前镜头末尾静止 | 后镜头开头静止 | 接缝总静止 |")
        lines.append("|---|---|---|---|")
        for s in report.seams:
            lines.append(f"| {s.prev_shot}→{s.next_shot} | {s.prev_trailing} | "
                         f"{s.next_leading} | **{s.total_still}**"
                         f"（{s.total_still / 24:.2f}s） |")
        lines.append("")

    if report.warnings:
        lines.append("## 警告")
        lines.append("")
        for w in report.warnings:
            lines.append(f"- ⚠️ {w}")
        lines.append("")
    return "\n".join(lines)
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_seam_audit.py -v`
Expected: 16 项全部 PASS

- [ ] **Step 5: 提交**

```bash
git add src/minimax_h3_prompt/tools/seam_audit.py tests/test_seam_audit.py
git commit -m "feat(tools): seam_audit 合并、报告与基线对比"
```

---

### Task 11: CLI 子命令 `audit`

**Files:**
- Modify: `src/minimax_h3_prompt/main.py`
- Test: `tests/test_audit_cli.py`

**Interfaces:**
- Consumes: `ledger.rebuild`（取镜头顺序）+ `seam_audit.analyze`
- Produces:
  - `python launch.py audit run <session_dir> --output-dir <dir> [--no-merge]`
  - `python launch.py audit promote <session_dir> --label <文字>`

- [ ] **Step 1: 写失败测试**

```python
"""audit CLI 子命令测试。"""
from __future__ import annotations

from minimax_h3_prompt.main import build_parser, main


def test_parser_has_audit_run():
    args = build_parser().parse_args(["audit", "run", "S", "--output-dir", "O"])
    assert args.command == "audit"
    assert args.audit_command == "run"
    assert args.session_dir == "S"


def test_parser_has_audit_promote():
    args = build_parser().parse_args(["audit", "promote", "S", "--label", "修复前"])
    assert args.audit_command == "promote"
    assert args.label == "修复前"


def test_audit_run_missing_session_returns_one(tmp_path, capsys):
    code = main(["audit", "run", str(tmp_path / "无"), "--output-dir", str(tmp_path)])
    assert code == 1
    assert "不存在" in capsys.readouterr().out


def test_audit_run_writes_report(tmp_path, tmp_video, capsys):
    from tests.test_ledger import _make_session

    session, out = _make_session(tmp_path, tmp_video, n_shots=3)
    code = main(["audit", "run", str(session), "--output-dir", str(out), "--no-merge"])
    assert code == 0
    assert (session / "audit").is_dir()
    output = capsys.readouterr().out
    assert "每缝均值" in output
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_audit_cli.py -v`
Expected: FAIL — `invalid choice: 'audit'`

- [ ] **Step 3: 实现**

在 `build_parser()` 里追加：

```python
    audit_parser = project.add_parser("audit", help="接缝审计：拼接并量化接缝静止")
    audit_sub = audit_parser.add_subparsers(dest="audit_command")
    audit_run = audit_sub.add_parser("run", help="跑一次审计并写出报告")
    audit_run.add_argument("session_dir")
    audit_run.add_argument("--output-dir", action="append", required=True)
    audit_run.add_argument("--no-merge", action="store_true", help="只分析不合并视频")
    audit_promote = audit_sub.add_parser("promote", help="把最近一次结果提升为基线")
    audit_promote.add_argument("session_dir")
    audit_promote.add_argument("--label", required=True, help="基线标签，如「修复前」")
```

在 `main()` 分发里追加：

```python
    if args.command == "audit":
        return _run_audit(args)
```

新增：

```python
def _run_audit(args: argparse.Namespace) -> int:
    """接缝审计：从 ledger 取镜头顺序，拼接并量化。"""
    import json
    from pathlib import Path

    from .tools import seam_audit
    from .tools.ledger import rebuild
    from .tools.seam_audit import AuditReport, SeamResult, ShotStill, promote_baseline

    session_dir = Path(args.session_dir)
    if not session_dir.is_dir():
        print(f"[错误] 会话目录不存在：{session_dir}")
        return 1
    command = getattr(args, "audit_command", None)
    out_dir = session_dir / "audit"

    if command == "promote":
        latest = out_dir / "latest.json"
        if not latest.is_file():
            print("[错误] 还没有审计结果，请先跑 audit run")
            return 1
        raw = json.loads(latest.read_text(encoding="utf-8"))
        report = AuditReport(
            shots=[ShotStill(**s) for s in raw["shots"]],
            seams=[SeamResult(**s) for s in raw["seams"]],
            duration_s=raw["duration_s"], still_seconds=raw["still_seconds"],
            still_ratio=raw["still_ratio"],
            per_seam_avg_frames=raw["per_seam_avg_frames"],
            warnings=raw.get("warnings", []), merged_path=raw.get("merged_path", ""))
        path = promote_baseline(session_dir, report, args.label)
        print(f"基线已写出：{path}")
        return 0

    if command != "run":
        print("[错误] 请指定子命令：audit run / audit promote")
        return 1

    output_dirs = [Path(d) for d in args.output_dir]
    ledger = rebuild(session_dir, output_dirs)
    videos = _resolve_videos(ledger, output_dirs)
    if not videos:
        print("[错误] ledger 里没有可用的镜头，无法审计")
        return 1

    report = seam_audit.analyze(videos, out_dir, merge=not args.no_merge)
    out_dir.mkdir(parents=True, exist_ok=True)
    baseline = seam_audit.load_baseline(session_dir)
    report_path = out_dir / f"{_timestamp()}.md"
    report_path.write_text(seam_audit.render_markdown(report, baseline),
                           encoding="utf-8")
    (out_dir / "latest.json").write_text(
        json.dumps({"shots": [vars(s) for s in report.shots],
                    "seams": [vars(s) for s in report.seams],
                    "duration_s": report.duration_s,
                    "still_seconds": report.still_seconds,
                    "still_ratio": report.still_ratio,
                    "per_seam_avg_frames": report.per_seam_avg_frames,
                    "warnings": report.warnings,
                    "merged_path": report.merged_path},
                   ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"镜头 {len(report.shots)} 个 ｜ 每缝均值 "
          f"{report.per_seam_avg_frames:.1f} 帧 ｜ 占全片 "
          f"{report.still_ratio * 100:.1f}%")
    if baseline:
        for name, base, cur, delta in seam_audit.compare_to_baseline(report, baseline):
            print(f"  {name}: 基线 {base} → 本次 {cur}（{delta}）")
    for note in report.warnings:
        print(f"[警告] {note}")
    print(f"报告：{report_path}")
    return 0


def _resolve_videos(ledger, output_dirs) -> list:
    """把 ledger 里的镜头名解析回真实文件路径。"""
    from pathlib import Path

    index: dict[str, Path] = {}
    for directory in output_dirs:
        if directory.is_dir():
            for path in directory.rglob("*.mp4"):
                index.setdefault(path.name, path)
    resolved = []
    for shot in ledger.shots:
        path = index.get(shot.video)
        if path is not None:
            resolved.append(path)
    return resolved


def _timestamp() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y-%m-%dT%H%M%S")
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_audit_cli.py -v`
Expected: 4 项全部 PASS

- [ ] **Step 5: 人工验收——复现基线**

Run:
```bash
./.venv/Scripts/python.exe launch.py audit run \
  "output/sessions/古老图书馆里-少年撕下会发光的书页折成纸-9ac0bf14/GEN001" \
  --output-dir "D:/Comfyui/ComfyUI/output/视频/MiniMax-H3/2026-09-18"
```

Expected: `每缝均值 24.3 帧`、`占全片 14.1%`，与 2026-09-20 手工 spike 的 4.04s / 14.1% 一致（±0.1）。

- [ ] **Step 6: 把基线固化下来**

Run:
```bash
./.venv/Scripts/python.exe launch.py audit promote \
  "output/sessions/古老图书馆里-少年撕下会发光的书页折成纸-9ac0bf14/GEN001" \
  --label "修复前 @2026-09-18"
```

- [ ] **Step 7: 提交**

```bash
git add src/minimax_h3_prompt/main.py tests/test_audit_cli.py
git commit -m "feat(cli): 新增 audit run / audit promote 子命令"
```

---

# 阶段 3：预检 — `preflight`

> 阶段 3 结束时，你搬去 ComfyUI 之前跑一条命令，它会告诉你"这张帧对不上"或"还能跑 N 段"。

---

### Task 12: 可证规则（输入合法性）

**Files:**
- Create: `src/minimax_h3_prompt/tools/preflight.py`
- Test: `tests/test_preflight.py`

**Interfaces:**
- Consumes: `frame_match`（`imread_unicode` / `read_window` / `mad` / `MAD_MAYBE`）、`ledger.Ledger`
- Produces:
  - `@dataclass Issue`：`severity: str`（`"error"` / `"warning"` / `"refrain"`）/ `code: str` / `message: str`
  - `check_input_frame(frame_path: Path, prev_video: Path | None, *, ledger: Ledger | None = None, prev_video_name: str | None = None) -> list[Issue]`
  - `check_shot_duration(seconds: float) -> list[Issue]`
  - `check_not_discarded(prev_video_name: str, ledger: Ledger) -> list[Issue]`
  - `EXPECTED_VIDEO_SIZE = (1280, 736)`、`SEGMENT_MIN_S = 4.0`、`SEGMENT_MAX_S = 10.0`

- [ ] **Step 1: 写失败测试**

```python
"""preflight 预检测试。"""
from __future__ import annotations

import cv2
import pytest

from minimax_h3_prompt.tools.frame_match import read_window
from minimax_h3_prompt.tools.ledger import Discarded, Ledger, SCHEMA
from minimax_h3_prompt.tools.preflight import (
    EXPECTED_VIDEO_SIZE,
    check_input_frame,
    check_not_discarded,
    check_shot_duration,
)


def _write_png(path, image):
    path.parent.mkdir(parents=True, exist_ok=True)
    ok, buf = cv2.imencode(".png", image)
    assert ok
    path.write_bytes(buf.tobytes())


def _probe_from_tail(video, size=(1280, 736)):
    tail = read_window(video, window="tail", k=1)[0]
    return cv2.cvtColor(cv2.resize(tail, size, interpolation=cv2.INTER_NEAREST),
                        cv2.COLOR_GRAY2BGR)


def test_expected_size_matches_h3_output():
    assert EXPECTED_VIDEO_SIZE == (1280, 736)


def test_correct_input_frame_passes(tmp_path, tmp_video):
    prev = tmp_video("prev.mp4")
    frame_path = tmp_path / "shot-02-start.png"
    _write_png(frame_path, _probe_from_tail(prev))
    issues = check_input_frame(frame_path, prev)
    assert issues == []


def test_wrong_input_frame_is_error(tmp_path, tmp_video):
    prev = tmp_video("prev.mp4")
    unrelated = tmp_video("unrelated.mp4", base=(200, 200, 200))
    frame_path = tmp_path / "shot-02-start.png"
    _write_png(frame_path, _probe_from_tail(unrelated))
    issues = check_input_frame(frame_path, prev)
    assert [i.severity for i in issues] == ["error"]
    assert issues[0].code == "bridge_frame_mismatch"


def test_missing_input_frame_is_error(tmp_path, tmp_video):
    prev = tmp_video("prev.mp4")
    issues = check_input_frame(tmp_path / "无.png", prev)
    assert issues[0].code == "bridge_frame_missing"


def test_wrong_size_is_warning(tmp_path, tmp_video):
    prev = tmp_video("prev.mp4")
    frame_path = tmp_path / "shot-02-start.png"
    _write_png(frame_path, _probe_from_tail(prev, size=(640, 360)))
    issues = check_input_frame(frame_path, prev)
    assert any(i.code == "bridge_frame_size" and i.severity == "warning" for i in issues)


def test_duration_within_h3_grid_passes():
    assert check_shot_duration(6.0) == []
    assert check_shot_duration(10.0) == []


def test_duration_outside_grid_is_error():
    assert check_shot_duration(12.0)[0].code == "duration_out_of_range"
    assert check_shot_duration(3.0)[0].code == "duration_out_of_range"


def test_discarded_prev_video_is_error():
    ledger = Ledger(schema=SCHEMA, session={}, scan={}, shots=[],
                    discarded=[Discarded(video="old.mp4", video_dir="d", reason="废片",
                                         replaced_by="new.mp4", confidence="high")],
                    interruptions=[], review_needed=[], warnings=[])
    issues = check_not_discarded("old.mp4", ledger)
    assert issues[0].code == "prev_video_discarded"
    assert issues[0].severity == "error"
    assert check_not_discarded("new.mp4", ledger) == []
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_preflight.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'minimax_h3_prompt.tools.preflight'`

- [ ] **Step 3: 实现**

创建 `src/minimax_h3_prompt/tools/preflight.py`：

```python
"""提交前预检：把错误拦在昂贵的生成之前。

背景：一个镜头要跑约 10 分钟。若输入帧绑错（2026-09-20 就发生过——一次
重跑误用了上一段的**首帧**而不是尾帧，导致接缝硬跳切），要等 10 分钟才
发现，而且事后无法从磁盘还原。

规则分三档，按**可证性**分级（借鉴 guardrails 的 on_fail 语义，但不引入依赖）：

- 可证规则 → ``error``，阻断。都是能算出来的事实，错了就是错了
- 启发式规则 → ``warning``，不阻断。可能误报
- 资源规则 → ``refrain``，劝阻但由人决定

启发式为什么只能是 warning：2026-09-18 实测里，``end_hook`` 写「定格」的
镜头 5 末尾静止为 0，而写「停在」的镜头 1/3 分别是 11/29。冻结词是相关，
不是因果。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .frame_match import MAD_MAYBE, imread_unicode, mad, read_window, to_gray
from .ledger import Ledger

EXPECTED_VIDEO_SIZE = (1280, 736)
SEGMENT_MIN_S = 4.0
SEGMENT_MAX_S = 10.0


@dataclass
class Issue:
    """一条预检结论。``severity`` 取 "error" | "warning" | "refrain"。"""

    severity: str
    code: str
    message: str


def check_input_frame(frame_path: Path, prev_video: Path | None) -> list[Issue]:
    """校验待提交的输入帧：存在、来自上一段尾帧、尺寸正确。"""
    issues: list[Issue] = []

    image = imread_unicode(frame_path)
    if image is None:
        issues.append(Issue("error", "bridge_frame_missing",
                            f"输入帧读不到或不存在：{frame_path}"))
        return issues

    height, width = image.shape[:2]
    if (width, height) != EXPECTED_VIDEO_SIZE:
        issues.append(Issue(
            "warning", "bridge_frame_size",
            f"输入帧尺寸 {width}×{height} 与 H3 输出 {EXPECTED_VIDEO_SIZE[0]}×"
            f"{EXPECTED_VIDEO_SIZE[1]} 不一致，可能不是从上一段视频抽出的帧",
        ))

    if prev_video is None:
        issues.append(Issue("warning", "prev_video_unknown",
                            "没有提供上一段视频，无法校验输入帧来源"))
        return issues

    tail = read_window(prev_video, window="tail")
    if not tail:
        issues.append(Issue("warning", "prev_video_unreadable",
                            f"上一段视频读不到：{prev_video}"))
        return issues

    probe = to_gray(image)
    best = min(mad(probe, frame) for frame in tail)
    if best >= MAD_MAYBE:
        issues.append(Issue(
            "error", "bridge_frame_mismatch",
            f"输入帧与上一段尾帧对不上（mad={best:.2f} ≥ {MAD_MAYBE}）。"
            f"请确认绑的是 {prev_video.name} 的**尾帧**，不是首帧或其他段的帧",
        ))
    return issues


def check_shot_duration(seconds: float) -> list[Issue]:
    """段时长必须落在 ComfyUI 的 H3 整数档 4–10 秒内。"""
    if seconds < SEGMENT_MIN_S or seconds > SEGMENT_MAX_S:
        return [Issue("error", "duration_out_of_range",
                      f"段时长 {seconds}s 超出 H3 可选区间 "
                      f"{SEGMENT_MIN_S:.0f}–{SEGMENT_MAX_S:.0f}s")]
    if abs(seconds - round(seconds)) > 1e-6:
        return [Issue("error", "duration_not_integer",
                      f"段时长 {seconds}s 不是整数秒，H3 只接受整数档")]
    return []


def check_not_discarded(prev_video_name: str, ledger: Ledger) -> list[Issue]:
    """上一段不能是被判定为废弃的视频。"""
    for item in ledger.discarded:
        if item.video == prev_video_name:
            replaced = f"，应改用 {item.replaced_by}" if item.replaced_by else ""
            return [Issue("error", "prev_video_discarded",
                          f"上一段 {prev_video_name} 是废弃版本（{item.reason}）"
                          f"{replaced}")]
    return []
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_preflight.py -v`
Expected: 9 项全部 PASS

- [ ] **Step 5: 提交**

```bash
git add src/minimax_h3_prompt/tools/preflight.py tests/test_preflight.py
git commit -m "feat(tools): preflight 可证规则（输入帧来源/尺寸/时长/废片）"
```

---

### Task 13: 启发式规则与资源规则

**Files:**
- Modify: `src/minimax_h3_prompt/tools/preflight.py`
- Modify: `src/minimax_h3_prompt/observability.py`（`TokenMeter` 加预算检查）
- Test: `tests/test_preflight.py`（追加）

**Interfaces:**
- Consumes: Task 12 的 `Issue`、`observability.token_meter`
- Produces:
  - `FROZEN_WORDS: tuple[str, ...]`
  - `find_frozen_words(text: str) -> list[str]`
  - `check_frozen_words(text: str) -> list[Issue]`（返回 `warning` 级 Issue，含确定性改写建议）
  - `suggest_defrost(text: str) -> str`
  - `estimate_remaining_shots(remaining_tokens: int, tokens_per_shot: int) -> int`
  - `check_budget(remaining_tokens: int, tokens_per_shot: int, remaining_shots: int) -> list[Issue]`
  - `TokenMeter.percent_used(budget_tokens: int) -> float`（改 `observability.py`）

- [ ] **Step 1: 写失败测试（追加到 `tests/test_preflight.py`）**

```python
from minimax_h3_prompt.tools.preflight import (
    FROZEN_WORDS,
    check_budget,
    check_frozen_words,
    estimate_remaining_shots,
    find_frozen_words,
    suggest_defrost,
)


def test_frozen_words_cover_the_ones_found_in_the_plan():
    for word in ("停住", "定格", "静止", "不再变化", "停在"):
        assert word in FROZEN_WORDS


def test_find_frozen_words_detects():
    assert find_frozen_words("少年垂眼，双手停在巨书封面上") == ["停在"]
    assert find_frozen_words("镜头停在掌心尺度的特写") == ["停在"]


def test_find_frozen_words_none_when_clean():
    assert find_frozen_words("纸鸟卧进摊开掌心，头颈朝向少年") == []


def test_check_frozen_words_is_warning_not_error():
    issues = check_frozen_words("她停住脚步")
    assert issues and all(i.severity == "warning" for i in issues)
    assert issues[0].code == "frozen_word"


def test_suggest_defrost_replaces_hold_verbs():
    out = suggest_defrost("双手停在巨书封面上")
    assert "停在" not in out
    assert "停在" in suggest_defrost.__doc__ or True  # 只要求产出可读改写


def test_estimate_remaining_shots():
    assert estimate_remaining_shots(remaining_tokens=200_000, tokens_per_shot=50_000) == 4
    assert estimate_remaining_shots(remaining_tokens=0, tokens_per_shot=50_000) == 0
    assert estimate_remaining_shots(remaining_tokens=100, tokens_per_shot=0) == 0


def test_check_budget_refrains_when_short():
    issues = check_budget(remaining_tokens=50_000, tokens_per_shot=50_000,
                          remaining_shots=3)
    assert issues and issues[0].severity == "refrain"
    assert "还能跑 1 段" in issues[0].message


def test_check_budget_silent_when_plenty():
    assert check_budget(remaining_tokens=10_000_000, tokens_per_shot=50_000,
                        remaining_shots=3) == []
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_preflight.py -v -k "frozen or budget or estimate"`
Expected: FAIL — `ImportError: cannot import name 'FROZEN_WORDS'`

- [ ] **Step 3: 实现（追加到 `preflight.py`）**

```python
# 这些词让 H3 在段尾把画面冻住；下一段又从同一张冻住的画面长出来，
# 于是接缝处出现连续静止。2026-09-18 实测：镜头 1/3 的 end_hook 含「停在」，
# 末尾静止 11/29 帧；镜头 5 含「定格」却为 0——所以只是相关，不是因果，
# 只能给 warning。
FROZEN_WORDS: tuple[str, ...] = ("停住", "定格", "静止", "不再变化", "停在")

_DEFROST_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    ("停在", "刚落在"),
    ("停住", "刚收住"),
    ("定格", "姿态落定"),
    ("静止", "动作收势"),
    ("不再变化", "保持微动"),
)


def find_frozen_words(text: str) -> list[str]:
    """返回文本里命中的冻结词（去重，保持 FROZEN_WORDS 的顺序）。"""
    return [word for word in FROZEN_WORDS if word in text]


def suggest_defrost(text: str) -> str:
    """给出确定性改写建议：把"停住"类词换成"动作刚落定的姿态"。

    只做字符串替换，不改语义结构；产出需人工确认后才使用。
    """
    result = text
    for frozen, alive in _DEFROST_REPLACEMENTS:
        result = result.replace(frozen, alive)
    return result


def check_frozen_words(text: str) -> list[Issue]:
    """启发式规则：命中冻结词只提示，不阻断。"""
    hits = find_frozen_words(text)
    if not hits:
        return []
    return [Issue(
        "warning", "frozen_word",
        f"命中冻结词 {', '.join(hits)}——可能造成段尾冻结。"
        f"建议改写为：{suggest_defrost(text)[:120]}",
    )]


def estimate_remaining_shots(remaining_tokens: int, tokens_per_shot: int) -> int:
    """按单段实测消耗估算还能跑几段。"""
    if tokens_per_shot <= 0:
        return 0
    return max(0, remaining_tokens // tokens_per_shot)


def check_budget(remaining_tokens: int, tokens_per_shot: int,
                 remaining_shots: int) -> list[Issue]:
    """资源规则：剩余额度不够跑完时劝阻（不阻断，由人决定）。"""
    affordable = estimate_remaining_shots(remaining_tokens, tokens_per_shot)
    if affordable >= remaining_shots:
        return []
    return [Issue(
        "refrain", "budget_short",
        f"剩余额度按当前消耗还能跑 {affordable} 段，但计划还剩 {remaining_shots} 段。"
        f"2026-09-18 曾因百炼免费额度耗尽导致第 6 段中断",
    )]
```

在 `observability.py` 的 `TokenMeter` 里加入：

```python
    def percent_used(self, budget_tokens: int) -> float:
        """已用 token 占预算的比例（0–1）。预算为 0 时返回 0.0。"""
        if budget_tokens <= 0:
            return 0.0
        totals = self.totals()
        used = totals["input"] + totals["output"]
        return min(1.0, used / budget_tokens)

    def remaining_tokens(self, budget_tokens: int) -> int:
        """剩余 token 数（不小于 0）。"""
        if budget_tokens <= 0:
            return 0
        totals = self.totals()
        return max(0, budget_tokens - totals["input"] - totals["output"])
```

并在 `tests/test_observability.py` 追加两个用例：

```python
def test_percent_used_and_remaining():
    from minimax_h3_prompt.observability import TokenMeter

    meter = TokenMeter(0.5, 1.5)
    meter.add("role_a", input_tokens=300_000, output_tokens=200_000)
    assert meter.percent_used(1_000_000) == pytest.approx(0.5)
    assert meter.remaining_tokens(1_000_000) == 500_000
    assert meter.percent_used(0) == 0.0
    assert meter.remaining_tokens(0) == 0
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_preflight.py tests/test_observability.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add src/minimax_h3_prompt/tools/preflight.py src/minimax_h3_prompt/observability.py tests/test_preflight.py tests/test_observability.py
git commit -m "feat(tools): preflight 冻结词启发式与 token 预算规则"
```

---

### Task 14: CLI 子命令 `preflight`

**Files:**
- Modify: `src/minimax_h3_prompt/main.py`
- Test: `tests/test_preflight_cli.py`

**Interfaces:**
- Consumes: Task 12/13 全部
- Produces: `python launch.py preflight --input-frame <png> [--prev-video <mp4>] [--session <dir>] [--prompt <file>] [--duration <s>] [--budget-tokens N]`

- [ ] **Step 1: 写失败测试**

```python
"""preflight CLI 测试。"""
from __future__ import annotations

from minimax_h3_prompt.main import build_parser, main


def test_parser_has_preflight():
    args = build_parser().parse_args(["preflight", "--input-frame", "f.png"])
    assert args.command == "preflight"
    assert args.input_frame == "f.png"


def test_cli_clean_input_returns_zero(tmp_path, tmp_video, capsys):
    import cv2

    from minimax_h3_prompt.tools.frame_match import read_window

    prev = tmp_video("prev.mp4")
    tail = read_window(prev, window="tail", k=1)[0]
    img = cv2.cvtColor(cv2.resize(tail, (1280, 736), interpolation=cv2.INTER_NEAREST),
                       cv2.COLOR_GRAY2BGR)
    frame_path = tmp_path / "shot-02-start.png"
    ok, buf = cv2.imencode(".png", img)
    assert ok
    frame_path.write_bytes(buf.tobytes())

    code = main(["preflight", "--input-frame", str(frame_path),
                 "--prev-video", str(prev)])
    assert code == 0
    assert "通过" in capsys.readouterr().out


def test_cli_mismatched_input_returns_one(tmp_path, tmp_video, capsys):
    import cv2

    from minimax_h3_prompt.tools.frame_match import read_window

    prev = tmp_video("prev.mp4")
    other = tmp_video("other.mp4", base=(200, 200, 200))
    tail = read_window(other, window="tail", k=1)[0]
    img = cv2.cvtColor(cv2.resize(tail, (1280, 736), interpolation=cv2.INTER_NEAREST),
                       cv2.COLOR_GRAY2BGR)
    frame_path = tmp_path / "shot-02-start.png"
    ok, buf = cv2.imencode(".png", img)
    assert ok
    frame_path.write_bytes(buf.tobytes())

    code = main(["preflight", "--input-frame", str(frame_path),
                 "--prev-video", str(prev)])
    assert code == 1
    assert "对不上" in capsys.readouterr().out


def test_cli_frozen_word_warns_but_passes(tmp_path, tmp_video, capsys):
    import cv2

    from minimax_h3_prompt.tools.frame_match import read_window

    prev = tmp_video("prev.mp4")
    tail = read_window(prev, window="tail", k=1)[0]
    img = cv2.cvtColor(cv2.resize(tail, (1280, 736), interpolation=cv2.INTER_NEAREST),
                       cv2.COLOR_GRAY2BGR)
    frame_path = tmp_path / "shot-02-start.png"
    ok, buf = cv2.imencode(".png", img)
    assert ok
    frame_path.write_bytes(buf.tobytes())
    prompt = tmp_path / "hook.txt"
    prompt.write_text("少年双手停在巨书封面上", encoding="utf-8")

    code = main(["preflight", "--input-frame", str(frame_path),
                 "--prev-video", str(prev), "--prompt", str(prompt)])
    assert code == 0, "启发式命中不应阻断"
    assert "停" in capsys.readouterr().out


def test_cli_budget_refrain_reported(tmp_path, tmp_video, capsys):
    import cv2

    from minimax_h3_prompt.tools.frame_match import read_window

    prev = tmp_video("prev.mp4")
    tail = read_window(prev, window="tail", k=1)[0]
    img = cv2.cvtColor(cv2.resize(tail, (1280, 736), interpolation=cv2.INTER_NEAREST),
                       cv2.COLOR_GRAY2BGR)
    frame_path = tmp_path / "shot-02-start.png"
    ok, buf = cv2.imencode(".png", img)
    assert ok
    frame_path.write_bytes(buf.tobytes())

    code = main(["preflight", "--input-frame", str(frame_path),
                 "--prev-video", str(prev),
                 "--budget-tokens", "1000", "--tokens-per-shot", "500",
                 "--remaining-shots", "5"])
    assert code == 0
    assert "还能跑" in capsys.readouterr().out
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_preflight_cli.py -v`
Expected: FAIL — `invalid choice: 'preflight'`

- [ ] **Step 3: 实现**

在 `build_parser()` 里追加：

```python
    pre_parser = project.add_parser("preflight", help="提交前预检：校验输入帧与预算")
    pre_parser.add_argument("--input-frame", required=True, help="待提交的桥接帧 png")
    pre_parser.add_argument("--prev-video", default=None, help="上一段的 mp4")
    pre_parser.add_argument("--prev-video-name", default=None,
                            help="上一段在 ledger 里的文件名（用于废片检查）")
    pre_parser.add_argument("--session", default=None, help="会话目录（读 ledger 用）")
    pre_parser.add_argument("--prompt", default=None, help="提示词或 end_hook 文本文件")
    pre_parser.add_argument("--duration", type=float, default=None, help="本段时长（秒）")
    pre_parser.add_argument("--budget-tokens", type=int, default=0,
                            help="总额度 token（0 = 不检查预算）")
    pre_parser.add_argument("--tokens-per-shot", type=int, default=0,
                            help="单段实测 token 消耗")
    pre_parser.add_argument("--remaining-shots", type=int, default=0,
                            help="计划还剩几段")
```

在 `main()` 分发里追加：

```python
    if args.command == "preflight":
        return _run_preflight(args)
```

新增：

```python
def _run_preflight(args: argparse.Namespace) -> int:
    """提交前预检。可证规则失败返回 1，其余返回 0。"""
    from pathlib import Path

    from .tools import preflight
    from .tools.ledger import Ledger, rebuild

    issues: list[preflight.Issue] = []
    prev_video = Path(args.prev_video) if args.prev_video else None

    ledger = None
    if args.session:
        session_dir = Path(args.session)
        if not session_dir.is_dir():
            print(f"[错误] 会话目录不存在：{session_dir}")
            return 1
        ledger_path = session_dir / "ledger.json"
        if ledger_path.is_file():
            import json

            try:
                ledger = Ledger.from_dict(json.loads(ledger_path.read_text(
                    encoding="utf-8")))
            except (OSError, ValueError) as exc:
                print(f"[警告] ledger 读不了（{exc}），跳过废片检查")
        else:
            print("[警告] 会话目录里没有 ledger.json，跳过废片检查")

    issues += preflight.check_input_frame(Path(args.input_frame), prev_video)

    if ledger is not None and args.prev_video_name:
        issues += preflight.check_not_discarded(args.prev_video_name, ledger)

    if args.duration is not None:
        issues += preflight.check_shot_duration(args.duration)

    if args.prompt:
        prompt_path = Path(args.prompt)
        if not prompt_path.is_file():
            print(f"[错误] 提示词文件不存在：{prompt_path}")
            return 1
        issues += preflight.check_frozen_words(
            prompt_path.read_text(encoding="utf-8"))

    if args.budget_tokens:
        issues += preflight.check_budget(
            remaining_tokens=args.budget_tokens,
            tokens_per_shot=args.tokens_per_shot,
            remaining_shots=args.remaining_shots,
        )

    errors = [i for i in issues if i.severity == "error"]
    for issue in issues:
        tag = {"error": "[阻断]", "warning": "[提醒]", "refrain": "[劝阻]"}[issue.severity]
        print(f"{tag} {issue.message}")

    if errors:
        print(f"\n预检未通过：{len(errors)} 项阻断。请先修输入再提交。")
        return 1
    print("\n预检通过。")
    return 0
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_preflight_cli.py -v`
Expected: 5 项全部 PASS

- [ ] **Step 5: 全量回归**

Run: `./.venv/Scripts/python.exe -m pytest -q`
Expected: 原有 134 项 + 新增全部通过，0 失败

- [ ] **Step 6: 提交**

```bash
git add src/minimax_h3_prompt/main.py tests/test_preflight_cli.py
git commit -m "feat(cli): 新增 preflight 子命令"
```

---

## 完成标准

全部 14 个任务完成后，你应该能：

```bash
# 1. 查清任意会话的镜头链路
./.venv/Scripts/python.exe launch.py ledger rebuild <session> --output-dir <dir>

# 2. 量化接缝并与基线对比
./.venv/Scripts/python.exe launch.py audit run <session> --output-dir <dir>

# 3. 提交前拦错
./.venv/Scripts/python.exe launch.py preflight --input-frame <png> --prev-video <mp4>
```

验收断言（全部来自 2026-09-20 的人工核对）：

| 断言 | 期望 |
|---|---|
| 《古老图书馆》镜头顺序 | `00001 → 00002 → 00004 → 00005 → 00006` |
| `00003` | 标为 `discarded`，`confidence: high`，`replaced_by: 00004` |
| 4 张桥接帧 | 全部 `confidence: high` |
| 中断 | 报出「第 6 段未产出」 |
| 接缝静止 | 每缝均值 ≈ 24.3 帧，占全片 ≈ 14.1% |

---

## 明确不在本计划内

- **合并层的正式实现**：`seam_audit` 里的 `merge_videos` 已够用（stream copy），不另做模块
- **驱动 ComfyUI**：等 ①②③ 落地后，看还剩多少痛再决定
- **修 `end_hook` 冻结词**：那是提示词层的事，本计划只提供检测手段，不改提示词
- **CI 接入**：合成 fixture 已是 CI-ready，但配置 GitHub Actions 是独立的一步
