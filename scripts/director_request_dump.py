#!/usr/bin/env python
"""导演台写段请求离线导出（#306）：逐段导出请求全文与 sha256，只读。

用法（仓库根）：

    uv run python scripts/director_request_dump.py <GEN 目录> [--segment N] [--out DIR]

为什么要有这一条：spec #297 的四张票（#298／#299／#301／#302）各自写了一份一次性
脚本，都是「读 ``plan.json`` / ``shot-table.md`` / ``session-state.json``，再调
``build_segment_request``」。四份实现各自漂一点，「改前/改后」对照就不是同一把尺。
收口到这一条之后，验收与排查共用同一份组装，``--out`` 两次即可 ``diff -r`` 对照。

**同源落在三处**：请求由 ``director.prompts.build_segment_request`` 组装；画面决策
块由 ``director.prompts.visual_decision_source`` 按本段窗口镜头号渲染；两个上下文块
由 ``ui.wizard_director.write_context_blocks`` 取（与向导首写、改词同一个出口，也与
工作台那两处同源）。本脚本不另拼任何一段请求文本。

**只读**：不写会话目录——``--out`` 是唯一的写口，且拒绝落进会话目录。全片尾帧的
读图 sidecar 缺失时**报错而不现场读图**：读图是付费视觉调用、还会写会话目录，
离线导出复现不了那一次调用。会话状态文件（``session-state.json``）缺失时仍可导出
（票面最低要求是 ``plan.json`` ＋ 分镜表），但两个上下文块按空处理并**打警告**说破
它与真实写段不一定逐字节相同。

没有 ``--out`` 时把请求全文打到 stdout；给了 ``--out`` 时落
``segment-NN.request.txt``（UTF-8 原字节，sha256 即对这份文件算）与 ``index.json``，
屏幕只打摘要与 sha256。相对 ``--out`` 一律按**主工作树**解析——票面 AC3 要求物证
落在主树的 ``output/accept-<主题>/``（``output/`` 不入库，留在工作树里会随删树消失）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# 标准流类型在 mypy 眼里是 TextIO（没有 reconfigure）：用 getattr 探测，非 TTY 也能跑。
for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if _reconfigure is not None:
        _reconfigure(encoding="utf-8")

SESSION_STATE_FILENAME = "session-state.json"
SHOT_TABLE_FILENAME = "shot-table.md"
INDEX_FILENAME = "index.json"
REQUEST_NAME = "segment-{index:02d}.request.txt"


class DumpError(RuntimeError):
    """导不出来时的用户面错误：消息本身就是终端上要看到的那句话。"""


@dataclass(frozen=True)
class ShotTable:
    """分镜表原文与它的来源（取自会话状态还是盘上副本）＋ 两者的同源告警。"""

    text: str
    source: str
    sha256: str
    warning: str = ""


@dataclass(frozen=True)
class SegmentDump:
    """一段的导出结果。``request`` 是**将要发给模型的那份字符串**，不是重排版。"""

    index: int
    type: str
    start_frame: int
    end_frame: int
    start_s: float
    end_s: float
    export_frames: int
    window_shots: tuple[int, ...]
    revision_context: str
    visual_decision: str
    request: str

    @property
    def sha256(self) -> str:
        return _sha256(self.request)


@dataclass(frozen=True)
class SessionDump:
    session_dir: Path
    mode: str
    total_frames: int
    shot_table: ShotTable
    # 段表的总段数：``--segment`` 只筛导出项，标题里的「第几段/共几段」仍按整张表念。
    total_segments: int
    segments: tuple[SegmentDump, ...]
    # 没有会话状态时的降级说明（此时两个上下文块为空）；有状态时为空串。
    state_note: str = ""


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _shot_table(session_dir: Path, state: dict) -> ShotTable:
    """取分镜表原文并**点名它是从哪取的**：两个来源不一致时给一句警告。

    写段实际用的是会话状态里的 ``shot_table``（CLI 与工作台都走 ``stage_state``），
    所以它优先；没有状态文件的会话（见 ``dump_session`` 的降级分支）退回盘上的
    ``shot-table.md``——那是阶段 1 产物副本，与状态里的字符串同源。两者都在却不同
    源时必须说破：那意味着这一次导出与真实写段看到的不是同一张表，``diff`` 出来的
    差异不全是代码改动造成的。
    """
    state_table = str(state.get("shot_table") or "")
    file_path = session_dir / SHOT_TABLE_FILENAME
    file_table = (file_path.read_text(encoding="utf-8")
                  if file_path.is_file() else "")
    if state_table.strip():
        warning = ""
        if file_table.strip() and file_table != state_table:
            warning = (
                f"会话状态与 {SHOT_TABLE_FILENAME} 的分镜表不一致"
                f"（state={_sha256(state_table)[:12]}，"
                f"file={_sha256(file_table)[:12]}）：本次导出用会话状态那一份"
                "（写段实际用的就是它）")
        return ShotTable(
            text=state_table,
            source=f"{SESSION_STATE_FILENAME} 的 stage_state.shot_table",
            sha256=_sha256(state_table), warning=warning)
    if file_table.strip():
        return ShotTable(text=file_table, source=SHOT_TABLE_FILENAME,
                         sha256=_sha256(file_table))
    raise DumpError(
        f"找不到分镜表：{SESSION_STATE_FILENAME} 里没有 stage_state.shot_table，"
        f"盘上也没有 {SHOT_TABLE_FILENAME}。写段请求没有分镜表就只剩剧情概述，"
        "导出的不是当时那一份请求")


def _film_end_description(image: Path) -> str:
    """只读全片尾帧的读图 sidecar；缓存缺失时报错，绝不现场读图。

    ``get_film_end_description`` 在缓存失效时会调视觉模型并**写会话目录**——那是
    导出脚本不该做的事（付费、改现场）。判据复用 ``still_description.read_cached_description``
    这一处（同一条缓存命中契约，与真实写段同一个口），不在这里再写一遍 kind/指纹比较。
    """
    from minimax_h3_prompt.director.film_end import FILM_END_KIND
    from minimax_h3_prompt.director.still_description import read_cached_description

    try:
        cached = read_cached_description(image, kind=FILM_END_KIND)
    except OSError as exc:
        raise DumpError(f"全片尾帧读图 sidecar 读不出（图：{image}）：{exc}") from exc
    if cached is None:
        raise DumpError(
            f"全片尾帧图在盘上（{image}），但读图描述 sidecar 缺失或已过期："
            "真实写段会当场调视觉模型重读（付费、且会写会话目录），离线导出复现不了"
            "那一次调用。先在能联网的环境里走一次收集/写段把它落盘")
    return cached


def _sections_and_budget(session_dir: Path, mode: str) -> tuple[dict[str, str] | None, Any | None]:
    """r2v 的英文定义块与段内素材预算；其它模式都没有参考素材。"""
    if mode != "r2v":
        return None, None
    from minimax_h3_prompt.director import refdefs
    from minimax_h3_prompt.director.planning import load_asset_budget

    try:
        cached = refdefs.load_ref_sections(session_dir)
    except (OSError, ValueError) as exc:
        raise DumpError(f"r2v 全片英文定义块读不出：{exc}") from exc
    if cached is None or not refdefs.has_sections(cached):
        raise DumpError(
            "r2v 的全片英文定义块（director/ref-sections.json）缺失或不完整："
            "真实写段会先调模型重生成（付费、且会写会话目录），离线导出复现不了。"
            "先在能联网的环境里跑一次规划/写段把它落盘")
    try:
        budget = load_asset_budget(session_dir)
    except ValueError as exc:
        raise DumpError(f"段内素材预算读不出：{exc}") from exc
    return cached.as_mapping(), budget


def dump_session(session_dir: str | Path, *, segment: int | None = None) -> SessionDump:
    """按真实写段的同一份入参组装逐段请求；任何读不回来的产物都升成 ``DumpError``。"""
    session_dir = Path(session_dir)
    if not session_dir.is_dir():
        raise DumpError(f"会话目录不存在：{session_dir}")

    from minimax_h3_prompt.director.firstframe import window_shot_numbers
    from minimax_h3_prompt.director.pack import film_end_still
    from minimax_h3_prompt.director.planning import plan_path, read_plan_json
    # 手工按段解析画面决策工厂的那一行与 ``write_director_segments`` 是同一条分支：
    # 这里不做第二次判断，直接用它的私有小件（重写一遍「callable 就调用」迟早分叉）。
    from minimax_h3_prompt.director.prompts import (
        _resolve_visual_decision,
        build_segment_request,
    )
    from minimax_h3_prompt.session_store import load_session
    from minimax_h3_prompt.ui.wizard_director import write_context_blocks

    try:
        plan = read_plan_json(plan_path(session_dir))
    except (OSError, ValueError) as exc:
        raise DumpError(
            f"读不回导演台段表 {plan_path(session_dir)}：{exc}") from exc

    try:
        session = load_session(session_dir)
    except ValueError as exc:
        raise DumpError(f"会话文件读不出：{exc}") from exc
    state: dict[str, Any] = {}
    state_note = ""
    if session is None:
        # 票面 AC1 的最低要求是 plan.json ＋ 分镜表：没有会话状态也要能导出。
        # 这时两个上下文块没有来源，**显式说破**（并打警告），而不是假装它俩是空的
        # 就悄悄导出一份与任何一次真实写段都不同的请求。
        if (session_dir / SESSION_STATE_FILENAME).is_file():
            raise DumpError(
                f"{session_dir / SESSION_STATE_FILENAME} 存在但读不出"
                "（schema 不认识或内容损坏）：先修好会话文件，别拿一份假请求去对照")
        state_note = (
            f"没有 {SESSION_STATE_FILENAME}：修订块与画面决策块按空处理，"
            "本次导出与真实写段不一定逐字节相同")
    else:
        state = session.stage_state

    table = _shot_table(session_dir, state)
    sections, budget = _sections_and_budget(session_dir, plan.mode)
    # 与 ``write_director_segments`` 的调用方同一判据（向导 / 工作台都这么算）：
    # 只有 fl2v 且盘上真有那张图时，请求才带终点画面块。
    film_end_image = (film_end_still(session_dir)
                      if plan.mode == "fl2v" else None)
    film_end = film_end_image is not None
    film_end_description = (_film_end_description(film_end_image)
                            if film_end_image is not None else "")
    # 两个上下文块走与向导首写/改词同一个出口，不在这里另取一遍 state 键。
    blocks = write_context_blocks(state, table.text)

    if segment is not None:
        valid = [item.index for item in plan.segments]
        if segment not in valid:
            raise DumpError(
                f"--segment {segment} 不在段表里（本会话只有第 "
                f"{min(valid)}–{max(valid)} 段）")

    dumped: list[SegmentDump] = []
    for item in plan.segments:
        if segment is not None and item.index != segment:
            continue
        visual = _resolve_visual_decision(blocks["visual_decision"], item)
        try:
            request = build_segment_request(
                plan, item, shot_table=table.text, sections=sections,
                film_end=film_end, film_end_description=film_end_description,
                budget=budget.for_segment(item.index) if budget is not None else None,
                revision_context=blocks["revision_context"],
                visual_decision=visual)
        except ValueError as exc:
            # 定义块/预算这类「产物形状不对」由 ``build_segment_request`` 抛 ValueError：
            # 升成用户面错误，别让 traceback 冒充「缺产物」的说明（AC1）。
            raise DumpError(
                f"第 {item.index} 段请求组装失败：{exc}") from exc
        dumped.append(SegmentDump(
            index=item.index,
            type=item.type,
            start_frame=item.start_frame,
            end_frame=item.end_frame,
            start_s=item.start_s,
            end_s=item.end_s,
            export_frames=item.export_frames,
            window_shots=window_shot_numbers(
                table.text, item.start_s, item.end_s),
            revision_context=blocks["revision_context"],
            visual_decision=visual,
            request=request,
        ))
    return SessionDump(
        session_dir=session_dir,
        mode=plan.mode,
        total_frames=plan.total_frames,
        shot_table=table,
        total_segments=len(plan.segments),
        segments=tuple(dumped),
        state_note=state_note,
    )


def _indent(text: str, prefix: str = "    ") -> str:
    return "\n".join(prefix + line if line.strip() else line
                     for line in text.rstrip("\n").splitlines())


def _shots_label(shots: tuple[int, ...]) -> str:
    return "、".join(f"Shot {number}" for number in shots) if shots else "（无镜头块）"


def render_report(dump: SessionDump, *, echo_requests: bool) -> str:
    """人读摘要：每段的窗口镜头号、两个上下文块的来源值，以及请求的 sha256。"""
    lines = [
        f"会话：{dump.session_dir}",
        f"模式：{dump.mode}　总帧：{dump.total_frames}　段数：{dump.total_segments}",
        f"分镜表来源：{dump.shot_table.source}　sha256={dump.shot_table.sha256}",
    ]
    if dump.state_note:
        lines.append(f"[警告] {dump.state_note}")
    if dump.shot_table.warning:
        lines.append(f"[警告] {dump.shot_table.warning}")
    for item in dump.segments:
        lines += [
            "",
            f"[段 {item.index}/{dump.total_segments}] {item.type} · "
            f"帧 {item.start_frame}–{item.end_frame}"
            f"（{item.start_s:.3f}–{item.end_s:.3f}s，导出 {item.export_frames} 帧）"
            f" · 窗口镜头：{_shots_label(item.window_shots)}",
            "  修订上下文块（render_revision_context(state)）："
            + ("（空）" if not item.revision_context.strip()
               else f"sha256={_sha256(item.revision_context)}"),
            "  画面决策块（visual_decision_source(state, shot_table) 按本段窗口）："
            + ("（空）" if not item.visual_decision.strip()
               else f"sha256={_sha256(item.visual_decision)}"),
        ]
        if item.revision_context.strip():
            lines.append(_indent(item.revision_context))
        if item.visual_decision.strip():
            lines.append(_indent(item.visual_decision))
        lines.append(
            f"  请求：{len(item.request.encode('utf-8'))} 字节 · "
            f"sha256={item.sha256}")
        if echo_requests:
            lines += ["  ──────── 请求全文（真实写段发给模型的就是这份）────────",
                      item.request.rstrip("\n"),
                      "  ──────── 请求结束 ────────"]
    return "\n".join(lines)


def write_outputs(dump: SessionDump, out_dir: Path) -> list[Path]:
    """落请求全文与索引；请求文件按 UTF-8 原字节写，文件 sha256 就等于请求 sha256。"""
    session = dump.session_dir.resolve()
    target = out_dir.resolve()
    if target == session or session in target.parents:
        raise DumpError(
            f"--out 不能落进会话目录（{session}）：本脚本对会话只读，"
            "请用 output/accept-<主题>/ 这类仓内验收目录")
    target.mkdir(parents=True, exist_ok=True)
    # 旧产物不删（删除是票面没要求的破坏性动作）：只说破哪些请求文件不在本轮导出里，
    # 否则同目录上一次段数更多时，`diff -r` 会把「已删掉的段」读成「没变」。
    current = {REQUEST_NAME.format(index=item.index) for item in dump.segments}
    stale = sorted(path.name for path in target.glob("segment-*.request.txt")
                   if path.name not in current)
    if stale:
        print(f"[警告] 目录里还有上一次导出的请求文件（不删，请自行清理，"
              f"否则 diff 会误读）：{', '.join(stale)}")
    written: list[Path] = []
    rows = []
    for item in dump.segments:
        path = target / REQUEST_NAME.format(index=item.index)
        path.write_bytes(item.request.encode("utf-8"))
        written.append(path)
        rows.append({
            "index": item.index,
            "type": item.type,
            "start_frame": item.start_frame,
            "end_frame": item.end_frame,
            "export_frames": item.export_frames,
            "start_s": round(item.start_s, 3),
            "end_s": round(item.end_s, 3),
            "revision_context_chars": len(item.revision_context.strip()),
            "visual_decision_chars": len(item.visual_decision.strip()),
            "window_shots": list(item.window_shots),
            "revision_context_sha256": (_sha256(item.revision_context)
                                        if item.revision_context.strip() else ""),
            "visual_decision_sha256": (_sha256(item.visual_decision)
                                       if item.visual_decision.strip() else ""),
            "file": path.name,
            "bytes": len(item.request.encode("utf-8")),
            "sha256": item.sha256,
        })
    index = {
        "session_dir": str(dump.session_dir),
        "mode": dump.mode,
        "total_frames": dump.total_frames,
        "shot_table_source": dump.shot_table.source,
        "shot_table_sha256": dump.shot_table.sha256,
        "state_note": dump.state_note,
        "segments": rows,
    }
    index_path = target / INDEX_FILENAME
    index_path.write_bytes(
        json.dumps(index, ensure_ascii=False, indent=2).encode("utf-8"))
    written.append(index_path)
    return written


def _main_worktree_root() -> Path:
    """主工作树根：``--out`` 给相对路径时以它为准。

    票面 AC3 要求物证落在**主树**的 ``output/accept-<主题>/``。在工作树里跑时
    cwd 是工作树，相对路径会把物证留在工作树——而 ``output/`` 不入库，收工删树就
    没了。判据只看本脚本所在检出的 ``.git``：主树上是目录（根即主树），链接工作树
    里是指向 ``<主树>/.git/worktrees/<名>`` 的**文件**，往上三层就是主树根。

    不调 ``git worktree list``：它的路径输出是 UTF-8，子进程按控制台默认编码解码
    会得到乱码路径（2026-11-17 实测把物证建到了 ``D:\\椤圭洰`` 下）。
    """
    repo_root = Path(__file__).resolve().parents[1]
    marker = repo_root / ".git"
    if marker.is_dir():
        return repo_root
    if marker.is_file():
        try:
            content = marker.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            content = ""
        if content.startswith("gitdir:"):
            gitdir = Path(content[len("gitdir:"):].strip())
            if gitdir.parent.name == "worktrees":
                return gitdir.parents[2]
    return Path.cwd()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="导演台写段请求离线导出（只读；与真实写段同一份组装）")
    parser.add_argument("session_dir", help="GEN 目录（含 director/plan.json 与分镜表）")
    parser.add_argument("--segment", type=int,
                        help="只导出这一段（1 起）；不给则导出全部段")
    parser.add_argument("--out", type=Path,
                        help="把请求全文与 index.json 写到这个目录（相对路径按主工作树"
                             "解析，便于落到 output/accept-<主题>/；不得在会话目录里）")
    args = parser.parse_args(argv)
    out_dir = args.out
    if out_dir is not None and not out_dir.is_absolute():
        # 相对 --out 一律按主树解析：AC3 的物证必须进主树 output/，工作树里跑也一样。
        out_dir = _main_worktree_root() / out_dir

    try:
        dump = dump_session(args.session_dir, segment=args.segment)
        written = write_outputs(dump, out_dir) if out_dir else []
    except (DumpError, OSError) as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 1

    # 写了文件就不再把几十 KB 的请求全文打一遍：屏幕给摘要与路径，对照用 diff -r。
    print(render_report(dump, echo_requests=out_dir is None))
    if written:
        print(f"\n已落盘 {len(written)} 份：{out_dir}")
        for path in written:
            print(f"  {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
