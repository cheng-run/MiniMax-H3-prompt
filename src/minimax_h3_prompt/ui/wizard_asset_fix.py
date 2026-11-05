"""已裁决素材画面事实的读时告警与就地修复（#168／#174）。

为什么要有这个模块：``_asset_loop`` 的待办队列**只收未落账的资产**（``_pending``
把 ``status=done`` 且钉住当前正本的那些滤掉），所以「已裁决、跳过」在真实运行里
根本走不到 ``_resume_returned_asset`` 那一支——续接时它们是从队列里静默消失的。
可一旦这些记录里混着「并进英文正文的中文补漏」（#162 之前产生的形态），下游
``proofread_descriptions`` → ``ref_asset_registry`` → ``<Picture N>`` 与段词，一路都
污染，而盘上没有任何入口能改：同 sha 重新交回会跳过读图（``test_done_record_at_
same_sha_skips_reread`` 钉住的行为），唯一的修法是手改 ``session-state.json``。

所以本模块**在循环入口**做两件事（每次进素材循环对每张已裁决资产各一次）：

1. **读时告警**：底本取当前生图提示词的同名条目，混排就点名资产与那段中文，
   并写一条交互日志告警（``language_mixed``，与 ``wizard_proofread`` 同族）；
2. **修复菜单**：``1=跳过（默认）`` / ``2=改素材画面事实``——把当前描述写进会话
   目录下的临时文本文件，用户改完回车，**同 sha 写回**（不换图、不花视觉调用），
   追加一条人工改写（``{at, before, after}``），删临时文件。

``wizard.py`` 有行数闸（``tests/test_wizard_size.py``），所以编排住这里；
``wizard`` 侧只留一次调用。问答由调用方注入（``ask``/``confirm``），本模块不
import ``wizard``——与 ``wizard_proofread`` 同一套防成环口径。
"""
from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .wizard_proofread import settled_mixed_language_warning

# 临时文本文件名：落在**会话目录**下（用户在资源管理器里够得到），一次只编辑一张，
# 用完即删。崩溃留下的那一份下次会被覆盖——它是中间物，不是产物。
EDIT_FILENAME = "proofread-edit.txt"


def review_settled(
    state: dict,
    brief: Any,
    generation_dir: str | Path,
    queue: Sequence[Any],
    abandoned: set[str],
    is_settled: Callable[[str], bool],
    ask: Callable[[str], str],
    confirm: Callable[..., bool],
) -> int:
    """遇到已裁决记录：先告警、再给修复菜单。返回这次改写了几张。

    只审**本次循环队列里**已裁决落定的资产（不在队列里的旧记录不碰——它们不属于
    这一轮，拿它们改出来的描述也没人消费）。``is_settled`` 是 ``_asset_loop`` 里
    那个判定（记录 status=done **且钉住当前正本**），传进来而不是在这里重写一份：
    同一语义只许一处口径。默认回车＝跳过，所以纯英文的正常会话只是多一行菜单、
    多一次回车。
    """
    from ..asset_ledger import load_asset_ledger
    from ..asset_proofread import STATUS_DONE, ProofreadRecord, proofread_rows
    from ..generation import image_prompt_variants
    from ..kinds import KIND_PICTURE

    names = [q.name for q in queue if q.name not in abandoned and is_settled(q.name)]
    if not names:
        return 0
    bases = {v.name: v.positive_prompt for v in image_prompt_variants(state, brief.canvases)}
    # 无生图提示词的登记项（brief.refs）也用同一份底本口径（同 _proofread_one_asset）
    for ref in getattr(brief, "refs", ()) or ():
        bases.setdefault(ref.name, getattr(ref, "description", ""))
    ledger = load_asset_ledger(generation_dir)
    records = proofread_rows(state)
    edited = 0
    for name in names:
        raw = records.get(name)
        if raw is None:
            continue
        record = ProofreadRecord.from_dict(name, raw)
        if record.status != STATUS_DONE:
            # 防御：调用方按 _settled 过滤过，走到这里说明两处口径漂了——
            # 宁可跳过也不要拿 pending_regen 的记录当已裁决去改。
            continue
        entry = ledger.get(name, KIND_PICTURE)
        tag = entry.tag if entry is not None else "无台账标签"
        print(f"\n[续跑·{name}] 这一版已裁决落定（{tag}），默认跳过。")
        base = str(bases.get(name, ""))
        if warn := settled_mixed_language_warning(name, record.description, base=base):
            print(warn)
        while True:
            choice = ask("  1=跳过（默认）  2=改素材画面事实（就地改写，不换图、不重读图）：").strip()
            if choice in ("", "1"):
                break
            if choice == "2":
                if _edit_description(
                        state, brief, generation_dir, name, record, base,
                        ask=ask, confirm=confirm):
                    edited += 1
                break
            print("无效输入，请选 1/2。")
    return edited


def _edit_description(
    state: dict,
    brief: Any,
    generation_dir: str | Path,
    name: str,
    record: Any,
    base: str,
    *,
    ask: Callable[[str], str],
    confirm: Callable[..., bool],
) -> bool:
    """把当前描述写到临时文件，等人改完再读回；返回是否落盘。

    不换图、不重读图，所以 ``sha256`` 原样保留——它钉的是**图**那一版，人工改的是
    **文**，两者不该互相冒充。空文本与未改动都**不落盘**（AC：改后为空或未改动 →
    不落盘并提示），留在提问里让人再改一次或 q 取消。
    """
    from ..asset_proofread import (
        PROOFREADS_KEY, ManualEdit, language_slip, proofread_rows,
    )
    from ..session_store import STATUS_AWAITING_FRAMES, save_session

    directory = Path(generation_dir)
    path = directory / EDIT_FILENAME
    before = record.description
    path.write_text(before, encoding="utf-8")
    print(f"[改素材画面事实·{name}] 当前描述已写入：{path}")
    print("  用任意文本编辑器改好、保存后回来回车；机器不重读图（sha 不变）。")
    try:
        while True:
            answer = ask("  改好后回车；输入 q 取消：").strip()
            if answer.lower() == "q":
                print("  已取消，描述保持原样。")
                return False
            try:
                after = path.read_text(encoding="utf-8").strip()
            except OSError as exc:
                print(f"  [提示] 读不到临时文件：{exc}；请重新编辑。")
                continue
            if not after:
                print("  [提示] 改后为空——未落盘；请再改一次，或输入 q 取消。")
                continue
            if after == before:
                print("  [提示] 内容没有变化——未落盘；请再改一次，或输入 q 取消。")
                continue
            break
        # 改完仍是混排：这次的目的正是治混排，所以默认**不保留**（回默认值就停住），
        # 但人可能有意留一个中文专名——给一次明确的保留出口，不硬拦。
        # 这里**不写交互日志**：告警账记的是「读到过混排」这一事实，人拒掉之后
        # 盘上并不存在那段混排文本，记一条就会变成假证据；读时那一处已经记过。
        slip = language_slip(after, body=base)
        if slip is not None:
            print(f"  [警告] 改后仍是中英混排：「{slip}」——它会原样进 <Picture N> 的描述与段词。")
            if not confirm("  改后仍中英混排：保留这次修改？", default=False):
                print("  未保留；描述保持原样。")
                return False
        updated = replace(record, description=after, manual_edits=(
            *record.manual_edits,
            ManualEdit(at=datetime.now(timezone.utc).isoformat(), before=before, after=after),
        ))
        records = proofread_rows(state)
        records[name] = updated.to_dict()
        state[PROOFREADS_KEY] = records
        save_session(directory, brief, state, status=STATUS_AWAITING_FRAMES)
        print(f"  ✓ 素材画面事实已更新（{name}，同 sha，未重读图）：{after}")
        print(f"    人工改写已留痕 {len(updated.manual_edits)} 条（改前/改后/时间都在记录里）。")
        return True
    finally:
        # 临时文件是这次编辑的中间物，不是产物：无论落盘还是取消都不留。
        # 删不掉不中断流程（改写可能已经落盘），但**不许静默**——留在盘上的是含用户
        # 原文的中间物，事后要能知道它属于哪一次编辑、为什么没清掉。
        try:
            path.unlink()
        except OSError as exc:
            print(f"  [提示] 临时文件清理失败，请手动删除：{path}（{exc}）",
                  file=sys.stderr)


__all__ = ["EDIT_FILENAME", "review_settled"]
