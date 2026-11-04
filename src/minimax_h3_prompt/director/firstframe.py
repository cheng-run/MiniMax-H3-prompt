"""`camera_cut` 段的段首帧生图提示词（P2，2026-11-02 验收汇总）。

为什么需要这个生产者：L5 规格只给**全片** first/last 出词（`graph/nodes.py` 的
``fl2va_frame_prompts``），切镜段的起点不在上一段终点，它自己的首帧既没有出词口、
也不在导演包里；#137 实跑时用户被要求「首帧自备」，终端连词都不给。

这里复用 `frame_prompt_engineer` 角色（八步骨架、生图纪律与词表都在它的系统提示词里），
只问**一帧**：本段起点那一格。产出落 ``GENxxx/director/firstframe-NN.md``。

与 #140／#114 的口径一致：这一份只描述**本段起点画面**，不碰尾帧（尾帧仍走 L4 编辑链）。
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path

from ..image_config import load_frame_image_model
from ..text_patterns import AT_TIMESTAMP as _SHOT_TIME
from .frame_meta import (
    frame_meta_path,
    read_frame_meta,
    sha256_text,
    write_frame_meta,
)
from .frames import DirectorSegment
from .planning import DIRECTOR_DIRNAME

FIRSTFRAME_FILENAME = "firstframe-{position:02d}.md"

# 用户挂过的段首帧图路径（#158 贴回画幅核对按它算期望值）。
# 为什么不另建一份文件：sidecar 的契约就是「与正文同生共死」，路径属于这份词的产物，
# 跟模型档、构图字段放一起，续跑读同一个口；换一份清单就多一处会与正文脱节的口径。
FIRST_FRAME_IMAGE_FIELD = "first_frame_image"

_SHOT_HEAD = re.compile(r"\*\*\[Shot\s+(\d+)\]\*\*")
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def firstframe_path(session_dir: str | Path, position: int) -> Path:
    """段首帧词的落点：``GENxxx/director/firstframe-NN.md``。"""
    return Path(session_dir) / DIRECTOR_DIRNAME / FIRSTFRAME_FILENAME.format(
        position=position)


def firstframe_meta_path(session_dir: str | Path, position: int) -> Path:
    """段首帧 sidecar 的落点（与正文同名，见 ``frame_meta``）。"""
    return frame_meta_path(firstframe_path(session_dir, position))


def record_first_frame_image(session_dir: str | Path, position: int,
                             image_path: str | Path) -> None:
    """把用户挂过的段首帧图路径写进段首帧 sidecar（#158）。

    贴回成片时项目要知道这一段该是什么画幅（票面：有锚图用锚图，没有才回落关键帧
    画布）。切镜段的首帧图是用户在陪跑里临时给的，盘上没有别的登记处——不记下来，
    续跑那一轮就只剩「元信息缺失」，核对等于没做。
    """
    path = firstframe_path(session_dir, position)
    meta = read_frame_meta(path) or {}
    meta.setdefault("kind", "firstframe")
    meta.setdefault("segment", position)
    meta[FIRST_FRAME_IMAGE_FIELD] = str(image_path)
    write_frame_meta(path, meta)


def _shot_blocks(shot_table: str) -> list[tuple[int, float, str]]:
    """``[(镜头号, 起点秒, 原文块)]``；第一镜没有时间戳，起点记 0。"""
    matches = list(_SHOT_HEAD.finditer(shot_table))
    blocks: list[tuple[int, float, str]] = []
    for position, match in enumerate(matches):
        end = matches[position + 1].start() if position + 1 < len(matches) else len(shot_table)
        text = shot_table[match.start():end]
        stamp = _SHOT_TIME.search(text)
        start = 0.0
        if stamp:
            minute, second, millis = (int(part) for part in stamp.groups())
            start = minute * 60 + second + millis / 1000.0
        blocks.append((int(match.group(1)), start, text.strip()))
    return blocks


def shot_excerpt_for_window(shot_table: str, start_s: float, end_s: float) -> str:
    """本段窗口覆盖到的镜头原文（含起点所在那一镜）。

    段边界按 17k+5 网格落位，经常**切在某个镜头中间**——起点所在那一镜必须收进来，
    否则首帧词会按下一镜写，画面直接从段中开始。
    """
    blocks = _shot_blocks(shot_table)
    if not blocks:
        return shot_table.strip()
    selected: list[str] = []
    for position, (_, shot_start, text) in enumerate(blocks):
        next_start = (blocks[position + 1][1]
                      if position + 1 < len(blocks) else float("inf"))
        if next_start <= start_s:
            continue
        if shot_start >= end_s:
            break
        selected.append(text)
    return "\n\n".join(selected) if selected else shot_table.strip()


def build_firstframe_request(segment: DirectorSegment,
                             shot_table: str, brief, model: str,
                             *, state: Mapping[str, object] | None = None) -> str:
    """只问**一帧**的请求：本段起点那一格，不要 last、不要中间节点。

    ``state`` 是阶段 1 的会话状态：分场剧本、人物/道具/背景设计、美术统筹、镜头
    评审锁定、画面细化、身份一致性锁定都在那里。切镜段是新场景，缺了这些设计事实，
    模型只能按剧情摘要自己想一个人——那正是 #137 实跑里「新场景换脸换装」的来源
    （#152）。
    """
    # 画布／出图模型／场景词三块与关键帧节点同一份真源（节点侧是私有函数，但两边
    # 描述的是同一件事；另写一份就会各漂）。
    from ..graph.nodes import (  # noqa: PLC0415 - 延迟导入，避免入口期拉起图栈
        _ctx, _frame_canvas_line, _frame_discipline_block, _frame_model_line,
        _scene_terms_block,
    )

    state = state or {}
    start_s = segment.start_frame / 24.0
    end_s = segment.end_frame / 24.0
    excerpt = shot_excerpt_for_window(shot_table, start_s, end_s)
    # 八项设计上下文与全片关键帧节点**同名同源**（`state` 键），不另立一套名字：
    # 另立一套就等于同一份设计有两个读法，改一处漏一处时首帧词会按旧设计写。
    design_context = _ctx(
        语言=str(brief.language or ""),
        分场剧本=str(state.get("script") or ""),
        人物设计=str(state.get("character_design") or ""),
        道具设计=str(state.get("prop_design") or ""),
        背景设计=str(state.get("background_design") or ""),
        美术统筹=str(state.get("art_design") or ""),
        镜头评审锁定=str(state.get("shot_review_lock") or ""),
        画面细化=str(state.get("visual_design") or ""),
        身份一致性锁定=str(state.get("identity_lock") or ""),
    )
    return (
        "只生成**一张**静态生图提示词：下面这一段的**段首帧**（本段第一格画面）。\n"
        "本段是切镜段：起点不在上一段终点（换了空间或时间），所以这一帧必须按本段的\n"
        "镜头描述独立构图，不要延续上一段的画面状态。\n"
        "只输出 `first` 一帧，不要 `last`，不要中间节点。\n\n"
        "【本段】"
        f"第 {segment.index} 段（{segment.type}），"
        f"{start_s:.3f}s–{end_s:.3f}s\n"
        f"【本段剧情】{segment.summary}\n"
        "【本段终点（不要画成首帧）】"
        f"{segment.end_hook}\n"
        "（这是本段**结束时**才达到的落定画面，用来说明运动终点；段首帧要画的是"
        "这一段的**起点**，不要把完成态画进开场。）\n\n"
        f"{design_context}\n\n"
        f"【本段镜头（分镜表节选）】\n{excerpt}\n\n"
        "【整片分镜表（对照用）】\n"
        f"{shot_table}\n\n"
        "【画布】\n"
        f"{_frame_canvas_line(model, brief.canvases)}\n"
        f"【出图模型】\n{_frame_model_line(model)}\n"
        f"【生图纪律】\n{_frame_discipline_block(model)}\n"
        "【场景词】\n"
        f"{_scene_terms_block(brief)}\n"
        "【原始主题】\n"
        f"{brief.plot}\n"
        "【起步澄清】\n"
        f"{brief.clarifications or '（无）'}\n"
        f"【时长】{brief.duration:g}s\n"
        f"【视觉风格】{brief.style}\n\n"
        "只输出一个 JSON 对象（不要 markdown 围栏）：\n"
        '{"scene_anchor": "…", "first": {"<模型子键>": '
        '{"positive_prompt": "…", "negative_prompt": "", '
        '"composition": "…", "lighting": "…"}}, "continuity_constraints": ["…"]}\n'
    )


def extract_first_frame(raw: str, model: str) -> dict | None:
    """从角色回复里取出 ``first`` 那一格（含 composition/lighting/negative）。

    返回的是**模型给出的 holder 字典**，不是拼好的正文：正文进可复制文件，其余字段
    进 sidecar（#152）。取不到返回 ``None``。
    """
    text = _FENCE.sub("", raw.strip()).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    first = data.get("first")
    if isinstance(first, list):
        first = first[0] if first else None
    if not isinstance(first, dict):
        return None
    # 模型档子键优先；其次任何带 positive_prompt 的 dict（角色举例子键与配置不符时）。
    holders = [first.get(model), first]
    holders.extend(value for value in first.values() if isinstance(value, dict))
    for holder in holders:
        if isinstance(holder, dict):
            prompt = holder.get("positive_prompt")
            if isinstance(prompt, str) and prompt.strip():
                return holder
    return None


def extract_first_prompt(raw: str, model: str) -> str | None:
    """从角色回复里取出 ``first`` 的正向提示词；取不到返回 ``None``。"""
    frame = extract_first_frame(raw, model)
    if frame is None:
        return None
    return str(frame["positive_prompt"]).strip()


def _segment_prompt_text(session_dir: str | Path,
                         segment: DirectorSegment) -> str | None:
    """本段视频词原文（provenance 的 ``segment_prompt_sha256`` 用它）；没有就 ``None``。

    读盘口径走 ``prompts.segment_path`` 一处，不在这里再拼一次文件名。
    """
    from .prompts import segment_path  # noqa: PLC0415 - 避免入口期拉起写段依赖

    try:
        return segment_path(session_dir, segment.index).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def write_firstframe_prompt(segment: DirectorSegment, *, llm,
                            shot_table: str, brief, session_dir: str | Path,
                            model: str | None = None,
                            state: Mapping[str, object] | None = None) -> str | None:
    """为切镜段产出段首帧词并落盘；调用失败返回 ``None``（不写半份）。

    正文只落正向提示词（用户说「复制给生图模型」的就是这一段）；composition /
    lighting / negative_prompt 与模型 provenance 落同名 sidecar，见 ``frame_meta``。
    """
    from ..agents import build_role_agents, run_agent  # noqa: PLC0415
    from ..tools.image_prompt_discipline import (  # noqa: PLC0415 - 与写词路径同层
        check_image_prompt, check_self_contained,
    )

    model = model or load_frame_image_model()
    request = build_firstframe_request(
        segment, shot_table=shot_table, brief=brief, model=model, state=state)
    agents = build_role_agents(
        llm, brief.refs, brief.mode, brief.duration, brief.variant)
    raw = run_agent(agents["frame_prompt_engineer"], request)
    frame = extract_first_frame(raw, model)
    if frame is None:
        print("[警告] 段首帧词没有解析出 first 帧的正向提示词，"
              "原文不落盘（半份词比没有更糟）。")
        return None
    prompt = str(frame["positive_prompt"]).strip()
    # 警告级、不拦产出（ADR 0018 同口径）：生图纪律按档判，自包含对所有档成立。
    # 打在产出那一刻，与尾帧词同一位置——等到出站再报，用户已经拿它去生图了。
    for warning in (*check_image_prompt(prompt, model=model),
                    *check_self_contained(prompt)):
        print(f"[警告] 第 {segment.index} 段段首帧词 "
              f"{warning.code}: {warning.message}", flush=True)
    path = firstframe_path(session_dir, segment.index)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(prompt + "\n", encoding="utf-8")
    write_frame_meta(path, {
        "kind": "firstframe",
        "segment": segment.index,
        "model": model,
        "composition": str(frame.get("composition") or ""),
        "lighting": str(frame.get("lighting") or ""),
        "negative_prompt": str(frame.get("negative_prompt") or ""),
        "segment_prompt_sha256": sha256_text(
            _segment_prompt_text(session_dir, segment)),
        # 段首帧是文生图，没有起点图；保留字段让首帧/尾帧 sidecar 同形（#153）。
        "start_image_sha256": None,
    })
    return prompt
