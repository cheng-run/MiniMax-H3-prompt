# 分段锁定区裁剪（Segment Lock Scoping）实施计划

> ⚠️ **未实施 + 部分已废弃（2026-09-22 标注）——不要照本计划施工。**
> 1. 本计划从未落成代码（全仓无 `segment_lock` / `segment_scope` / `extract_segment_shots`），且正文只写到 Task 2。
> 2. 其核心结构 `GLOBAL_LOCK:` 与 `EDGE_STABILITY_SENTENCE`（防波纹咒语）已被 **2026-09-22 官方格式迁移明令删除**
>    （`tools/h3_validator` 对其报 error，见 `CONTEXT.md` 末节「提示词格式纪律」）；照做会把违规结构重新引入。
> 3. 仍然成立的部分：根因 4「`shot_text_{n}` 键根本不存在 → 回退整张分镜表」，已由 2026-09-22 的首帧锚定修复落地
>    （`segment_prompts._segment_shot_texts`，缺陷编号 H4）。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让每个执行段的提示词只携带本段窗口内的人设/场景/道具信息，并把官方对齐指令、单镜头编号、桥接原文做确定性注入，消除"锁定区塞满全片剧情"导致的越窗演出与跨段漂移。

**Architecture:** 新增 `segment_lock` 模块：一次 LLM 调用把全片设计文档压缩成结构化 `SegmentLock`（含带时间窗的道具阶段与剧情节拍表），每段按 `[start_s, end_s)` 渲染子集并逐字复用；新增 `tools/segment_scope.py` 做越窗/预算检查；`segment_prompts.py` 增加 `extract_segment_shots`（修 `shot_text_N` 缺失 bug）与 `normalize_segment_prompt`（确定性规范化）；`wizard._run_segmented_flow` 负责接线。

**Tech Stack:** Python 3.14、pytest、LangChain（既有 `llm.invoke`）、无新增第三方依赖。

**Spec:** `docs/superpowers/specs/2026-09-17-segment-lock-scoping-design.md`

## Global Constraints

- Python `>=3.14`（`pyproject.toml` 已锁定），不引入任何新依赖。
- 官方提示词规范以 `C:\Users\XOS\.codex\skills\h3-prompt-writing\references\base-en.txt` 为唯一依据；三个核心段落顺序固定为 `integrated_multimodal_description` → `overall_soundscape` → `non_diegetic_music`。
- I2VA 首行必须逐字符等于 `For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.`（取自 `h3_validator.ALIGN_TEMPLATES`，禁止另抄一份字面量）。
- 每个执行段都是独立单镜头视频，镜头编号一律 `[Shot 1]`。
- 段时长必须为 4–10 秒整数（`MIN_SEGMENT_SECONDS` / `MAX_SEGMENT_SECONDS`）。
- 每个 `[Shot N]` 块结尾必须原样追加 `segment_prompts.EDGE_STABILITY_SENTENCE`。
- `SegmentPlan` 的 `start_s` / `end_s` / `duration_s` / `shots_in_segment` / `end_hook` 字段名不得改动（`tests/test_segment_planner.py` 依赖）。
- 所有面向用户的打印信息用简体中文；提示词正文用英文。
- 测试命令统一为 `python -m pytest tests -q`；在受控沙箱内 TEMP 被限制时使用 `$env:TEMP="$PWD\output\_pytest_tmp"; $env:TMP=$env:TEMP` 前缀（该目录已存在）。
- 提交信息用 `codex/` 分支，提交信息前缀 `fix:` 或 `feat:`。

## 文件结构

| 文件 | 职责 |
|---|---|
| `src/minimax_h3_prompt/segment_lock.py`（新建） | `SegmentLock` / `PropPhase` / `Beat` 数据结构、JSON 往返、按窗口渲染、LLM 构建 |
| `src/minimax_h3_prompt/prompts/segment_lock.md`（新建） | 生成 `SegmentLock` 的 LLM 指令 |
| `src/minimax_h3_prompt/tools/segment_scope.py`（新建） | 越窗节拍检查 + 锁定区预算检查 |
| `src/minimax_h3_prompt/segment_prompts.py`（改） | `extract_segment_shots`、`build_segment_v2_request`、`write_segment_v2`、`normalize_segment_prompt` |
| `src/minimax_h3_prompt/ui/wizard.py`（改） | 构建 lock 一次、逐段传入、展示前跑检查 |
| `src/minimax_h3_prompt/session_store.py`（改） | `_STATE_KEYS` 增加 `segment_lock` |
| `tests/test_segment_lock.py`（新建） | Task 1–2 |
| `tests/test_segment_prompts.py`（改） | Task 3–5 |
| `tests/test_segment_scope.py`（新建） | Task 6 |
| `tests/test_segmented_flow_scope.py`（新建） | Task 7 |

---

### Task 1: `SegmentLock` 数据结构与按窗口渲染

**Files:**
- Create: `src/minimax_h3_prompt/segment_lock.py`
- Test: `tests/test_segment_lock.py`

**Interfaces:**
- Consumes: 无（首个任务）。
- Produces:
  - `PropPhase(prop_id: str, start_s: int, end_s: int, text: str)`
  - `Beat(name: str, start_s: int, end_s: int, keywords: tuple[str, ...])`
  - `SegmentLock(identity, wardrobe, scene, style, forbidden, props, beats) -> SegmentLock`，前五项为 `tuple[str, ...]`，`props: tuple[PropPhase, ...]`，`beats: tuple[Beat, ...]`
  - `SegmentLock.to_dict() -> dict` / `SegmentLock.from_dict(raw: dict) -> SegmentLock`
  - `render_lock_for_segment(lock: SegmentLock, start_s: int, end_s: int) -> str`

- [ ] **Step 1: 写失败测试**

```python
"""SegmentLock 数据结构、JSON 往返与按窗口渲染测试。"""
from minimax_h3_prompt.segment_lock import (
    Beat,
    PropPhase,
    SegmentLock,
    render_lock_for_segment,
)


def _sample_lock() -> SegmentLock:
    return SegmentLock(
        identity=(
            "the boy is 12 to 14 years old, slim, narrow-shouldered, fragile.",
            "the boy has a faint diagonal old scar on his right palm.",
        ),
        wardrobe=("deep ink-blue old-style long coat over an old off-white shirt.",),
        scene=("an ancient library with a forbidden-book long table and a high stained-glass window.",),
        style=("dark fairy-tale fantasy, restrained old-gold and ink-blue palette.",),
        forbidden=("no modern clothing, no neon, no mechanical structures.",),
        props=(
            PropPhase("glowing_page", 0, 11, "an old page still bound in the book; fibers glowing, not yet folded."),
            PropPhase("paper_bird", 11, 16, "the folded origami paper bird with a thin weak light trail."),
            PropPhase("stained_glass_window", 16, 30, "the broken stained-glass window opening onto night."),
            PropPhase("dragon_bone_star_sea", 21, 30, "the dragon-bone star sea beyond the window."),
        ),
        beats=(
            Beat("touching", 0, 6, ("touch", "fingertip")),
            Beat("tearing", 6, 11, ("tear", "torn")),
            Beat("folding", 11, 16, ("fold", "origami", "paper bird")),
            Beat("window_break", 16, 21, ("stained-glass", "crack", "shatter")),
            Beat("leaping", 21, 26, ("leap", "jump")),
            Beat("star_sea", 26, 30, ("dragon-bone", "star sea")),
        ),
    )


def test_render_lock_is_byte_identical_for_shared_entries():
    lock = _sample_lock()
    first = render_lock_for_segment(lock, 0, 6)
    second = render_lock_for_segment(lock, 6, 11)
    shared = "- the boy is 12 to 14 years old, slim, narrow-shouldered, fragile."
    assert shared in first
    assert shared in second


def test_render_lock_excludes_future_prop_phases():
    lock = _sample_lock()
    text = render_lock_for_segment(lock, 0, 6).lower()
    assert "paper bird" not in text
    assert "origami" not in text
    assert "star sea" not in text
    assert "stained-glass window opening onto night" not in text


def test_render_lock_includes_only_intersecting_prop_phase():
    lock = _sample_lock()
    text = render_lock_for_segment(lock, 0, 6)
    assert "still bound in the book" in text
    assert "broken stained-glass window" not in text


def test_prop_phase_window_is_half_open():
    lock = SegmentLock(
        identity=("a boy.",), wardrobe=(), scene=(), style=(), forbidden=(),
        props=(PropPhase("glowing_page", 0, 6, "still bound in the book; not torn."),),
        beats=(),
    )
    assert "not torn" in render_lock_for_segment(lock, 0, 6)
    assert "not torn" not in render_lock_for_segment(lock, 6, 11)


def test_render_lock_uses_official_field_names():
    text = render_lock_for_segment(_sample_lock(), 0, 6)
    for header in ("character:", "background:", "props:"):
        assert header in text
    assert text.startswith("GLOBAL_LOCK:")


def test_segment_lock_json_roundtrip():
    lock = _sample_lock()
    assert SegmentLock.from_dict(lock.to_dict()) == lock

- [ ] **Step 2: 运行测试确认失败**

Run: `$env:TEMP="$PWD\output\_pytest_tmp"; $env:TMP=$env:TEMP; python -m pytest tests/test_segment_lock.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'minimax_h3_prompt.segment_lock'`

- [ ] **Step 3: 写最小实现**

```python
"""分段锁定区（SegmentLock）：把全片设计文档压缩成可按时窗裁剪的结构化锁定条目。

设计动机见 docs/superpowers/specs/2026-09-17-segment-lock-scoping-design.md：
旧实现把整份全片设计文档逐段塞进 GLOBAL_LOCK，导致后段剧情提前进入本段提示词。
本模块一次生成、逐段按 [start_s, end_s) 渲染子集，保证跨段共享条目逐字一致。
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
LOCK_FIELDS = ("identity", "wardrobe", "scene", "style", "forbidden")


@dataclass(frozen=True)
class PropPhase:
    """一个道具在某个时间窗内的状态描述（半开区间 [start_s, end_s)）。"""

    prop_id: str
    start_s: int
    end_s: int
    text: str


@dataclass(frozen=True)
class Beat:
    """全片剧情节拍及其时间窗，供越窗检查使用。"""

    name: str
    start_s: int
    end_s: int
    keywords: tuple[str, ...]


@dataclass(frozen=True)
class SegmentLock:
    identity: tuple[str, ...]
    wardrobe: tuple[str, ...]
    scene: tuple[str, ...]
    style: tuple[str, ...]
    forbidden: tuple[str, ...]
    props: tuple[PropPhase, ...] = field(default_factory=tuple)
    beats: tuple[Beat, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return {
            "identity": list(self.identity),
            "wardrobe": list(self.wardrobe),
            "scene": list(self.scene),
            "style": list(self.style),
            "forbidden": list(self.forbidden),
            "props": [asdict(p) for p in self.props],
            "beats": [
                {**asdict(b), "keywords": list(b.keywords)}
                for b in self.beats
            ],
        }

    @classmethod
    def from_dict(cls, raw: dict) -> SegmentLock:
        def strings(key: str) -> tuple[str, ...]:
            value = raw.get(key, [])
            if isinstance(value, str):
                value = [value]
            return tuple(str(v).strip() for v in value if str(v).strip())

        return cls(
            identity=strings("identity"),
            wardrobe=strings("wardrobe"),
            scene=strings("scene"),
            style=strings("style"),
            forbidden=strings("forbidden"),
            props=tuple(
                PropPhase(
                    prop_id=str(p.get("prop_id", "")),
                    start_s=int(p.get("start_s", 0)),
                    end_s=int(p.get("end_s", 0)),
                    text=str(p.get("text", "")).strip(),
                )
                for p in raw.get("props", [])
                if str(p.get("text", "")).strip()
            ),
            beats=tuple(
                Beat(
                    name=str(b.get("name", "")),
                    start_s=int(b.get("start_s", 0)),
                    end_s=int(b.get("end_s", 0)),
                    keywords=tuple(str(k).strip() for k in b.get("keywords", []) if str(k).strip()),
                )
                for b in raw.get("beats", [])
            ),
        )


def _overlaps(start_s: int, end_s: int, phase: PropPhase) -> bool:
    """半开区间相交：本段 [start_s, end_s) 与道具阶段 [phase.start_s, phase.end_s)。"""
    return phase.start_s < end_s and start_s < phase.end_s


def render_lock_for_segment(lock: SegmentLock, start_s: int, end_s: int) -> str:
    """把锁定区渲染为本段窗口内的英文条目；跨段共享条目逐字节一致。"""
    lines = ["GLOBAL_LOCK:"]
    sections = (
        ("character:", lock.identity + lock.wardrobe),
        ("background:", lock.scene),
        ("props:", tuple(p.text for p in lock.props if _overlaps(start_s, end_s, p))),
        ("visual_style:", lock.style + lock.forbidden),
    )
    for header, entries in sections:
        lines.append(header)
        lines.extend(f"- {entry}" for entry in entries)
    return "\n".join(lines)

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_segment_lock.py -q`
Expected: PASS — 6 passed

- [ ] **Step 5: 提交**

```bash
git add src/minimax_h3_prompt/segment_lock.py tests/test_segment_lock.py
git commit -m "feat: 新增 SegmentLock 结构化锁定区与按窗口渲染"
```

---

### Task 2: 用 LLM 一次性构建 `SegmentLock`

**Files:**
- Modify: `src/minimax_h3_prompt/segment_lock.py`（追加）
- Create: `src/minimax_h3_prompt/prompts/segment_lock.md`
- Test: `tests/test_segment_lock.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 `SegmentLock` / `PropPhase` / `Beat` / `PROMPTS_DIR`。
- Produces:
  - `build_segment_lock_request(state: dict, brief) -> str`
  - `parse_segment_lock(text: str) -> SegmentLock | None`
  - `build_segment_lock(state: dict, brief, llm) -> SegmentLock | None`
  - `lock_summary_zh(lock: SegmentLock) -> str`

- [ ] **Step 1: 写失败测试**

```python
def test_parse_segment_lock_from_fenced_json():
    from minimax_h3_prompt.segment_lock import parse_segment_lock

    raw = """```json
    {"identity": ["one boy, 12 to 14, slim."], "wardrobe": ["ink-blue long coat."],
     "scene": ["ancient library."], "style": ["dark fairy-tale."],
     "forbidden": ["no modern clothing."],
     "props": [{"prop_id": "glowing_page", "start_s": 0, "end_s": 11, "text": "still bound."}],
     "beats": [{"name": "touching", "start_s": 0, "end_s": 6, "keywords": ["touch"]}]}
    ```"""
    lock = parse_segment_lock(raw)
    assert lock is not None
    assert lock.identity == ("one boy, 12 to 14, slim.",)
    assert lock.props[0].prop_id == "glowing_page"
    assert lock.beats[0].keywords == ("touch",)


def test_parse_segment_lock_rejects_garbage():
    from minimax_h3_prompt.segment_lock import parse_segment_lock

    assert parse_segment_lock("not json at all") is None


def test_parse_segment_lock_tolerates_extra_prose():
    from minimax_h3_prompt.segment_lock import parse_segment_lock

    raw = '好的，这是结果：{"identity": ["a boy."], "props": [], "beats": []} 以上。'
    lock = parse_segment_lock(raw)
    assert lock is not None
    assert lock.identity == ("a boy.",)
    assert lock.scene == ()


def test_build_segment_lock_returns_none_on_llm_error():
    from minimax_h3_prompt.segment_lock import build_segment_lock

    class BrokenLLM:
        def invoke(self, request):
            raise RuntimeError("quota exhausted")

    assert build_segment_lock({"character_design": "x"}, object(), BrokenLLM()) is None


def test_build_segment_lock_request_includes_full_plan():
    from minimax_h3_prompt.segment_lock import build_segment_lock_request

    state = {
        "character_design": "少年设定",
        "background_design": "图书馆",
        "prop_design": "发光书页",
        "art_design": "暗黑童话",
        "creative_lock": "I2VA 30秒",
        "shot_table": "[Shot 1] 静默召唤",
    }
    request = build_segment_lock_request(state, object())
    for token in ("少年设定", "图书馆", "发光书页", "暗黑童话", "[Shot 1] 静默召唤"):
        assert token in request
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_segment_lock.py -q`
Expected: FAIL — `ImportError: cannot import name 'parse_segment_lock'`

- [ ] **Step 3: 写最小实现**

在 `segment_lock.py` 追加（沿用 `segment_planner._extract_json` 的容错策略，但自行实现以避免跨模块耦合）：

```python
def _extract_json(text: str) -> dict | None:
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = match.group(1) if match else text
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        start, end = candidate.find("{"), candidate.rfind("}")
        if 0 <= start < end:
            try:
                return json.loads(candidate[start:end + 1])
            except json.JSONDecodeError:
                return None
    return None


def parse_segment_lock(text: str) -> SegmentLock | None:
    data = _extract_json(str(text))
    if not isinstance(data, dict):
        return None
    lock = SegmentLock.from_dict(data)
    if not lock.identity:
        return None
    return lock


def build_segment_lock_request(state: dict, brief) -> str:
    instruction = (PROMPTS_DIR / "segment_lock.md").read_text(encoding="utf-8")
    return (
        f"{instruction}\n\n---\n"
        f"视频总时长：{float(brief.duration):g}s\n"
        f"对白语言：{brief.language}\n"
        f"创作方向：{brief.plot}\n\n"
        f"角色设定：\n{state.get('character_design', '')}\n\n"
        f"场景设定：\n{state.get('background_design', '')}\n\n"
        f"道具设定：\n{state.get('prop_design', '')}\n\n"
        f"美术风格：\n{state.get('art_design', '')}\n\n"
        f"创作锁定：\n{state.get('creative_lock', '')}\n\n"
        f"分镜表：\n{state.get('shot_table', '')}\n"
    )


def build_segment_lock(state: dict, brief, llm) -> SegmentLock | None:
    """一次生成全片锁定区；失败返回 None（调用方回退旧路径并告警）。"""
    try:
        response = llm.invoke(build_segment_lock_request(state, brief))
    except Exception:  # noqa: BLE001 - 任何 LLM 异常都回退
        return None
    return parse_segment_lock(str(getattr(response, "content", response)))


def lock_summary_zh(lock: SegmentLock) -> str:
    """给用户看的锁定区摘要（中文字段名 + 条数），非提示词正文。"""
    return (
        f"人设 {len(lock.identity) + len(lock.wardrobe)} 条 · 场景 {len(lock.scene)} 条 · "
        f"风格 {len(lock.style) + len(lock.forbidden)} 条 · 道具阶段 {len(lock.props)} 个 · "
        f"剧情节拍 {len(lock.beats)} 个"
    )
```

`src/minimax_h3_prompt/prompts/segment_lock.md` 用中文写指令，要求严格输出 JSON：

```markdown
# Segment Lock（全片锁定区压缩）

你把一份"全片设计资料"压缩成**结构化锁定区**，供长视频的每个 4-10 秒执行段按时间窗取用。

## 为什么要压缩

每个执行段的提示词会带上你输出的锁定区。旧做法把整份设计文档原样塞进每段，
导致后段剧情（折纸、纸鸟、碎窗、跃出、星海）提前进入前段提示词，模型演错。
因此：**全片通用的部分必须极简，只在某个时间窗成立的部分必须放进 `props` 的阶段里。**

## 输出格式（严格只有 JSON，不要任何解释 / markdown 围栏）

```json
{
  "identity": ["one boy, 12 to 14, slim, narrow-shouldered, long thin fingers.", "a faint diagonal old scar on his right palm."],
  "wardrobe": ["deep ink-blue old-style long coat over an old off-white shirt, dark gray-black trousers, dark brown old leather boots."],
  "scene": ["an ancient library with a forbidden-book long table and a high stained-glass window."],
  "style": ["dark fairy-tale fantasy, old-gold and ink-blue palette, restrained particles."],
  "forbidden": ["no modern clothing, no neon, no mechanical structures."],
  "props": [
    {"prop_id": "glowing_page", "start_s": 0, "end_s": 11, "text": "an old glowing page still bound in the forbidden book; fibers glowing softly, not yet torn."}
  ],
  "beats": [
    {"name": "touching", "start_s": 0, "end_s": 6, "keywords": ["touch", "fingertip"]}
  ]
}
```

## 硬约束

- `identity`：≤ 6 条，每条 ≤ 25 词；只写**全片都不变**的身份特征（年龄段、体态、脸型、发型、标志性伤痕）。
- `wardrobe`：≤ 4 条，每条 ≤ 25 词；只写全片都穿的那套衣服（概括到足以辨认，不要逐件罗列细节）。
- `scene`：≤ 3 条，每条 ≤ 25 词。
- `style`：≤ 4 条，每条 ≤ 25 词。
- `forbidden`：≤ 4 条，每条 ≤ 20 词。
- **不要**在以上五类里写任何只属于某个时间段的剧情（撕页、折纸、纸鸟、碎窗、跃出、星海）。
- `props`：每个关键道具按**状态变化**拆成多个阶段；`start_s`/`end_s` 为整数秒、半开区间 `[start_s, end_s)`；
  同一道具的阶段必须首尾相接、覆盖它出现的全部时间。`text` ≤ 30 词，只描述该阶段的状态。
- `beats`：把全片剧情节拍逐个列出；`start_s`/`end_s` 为整数秒；
  `keywords` 给 2-5 个**英文小写关键词或短语**，用于事后扫描某段提示词是否越窗写了该节拍。
- 所有条目正文用英文；时间必须落在 `0` 到视频总时长之间。
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_segment_lock.py -q`
Expected: PASS — 11 passed

- [ ] **Step 5: 提交**

```bash
git add src/minimax_h3_prompt/segment_lock.py src/minimax_h3_prompt/prompts/segment_lock.md tests/test_segment_lock.py
git commit -m "feat: 一次性构建结构化 SegmentLock"
```
