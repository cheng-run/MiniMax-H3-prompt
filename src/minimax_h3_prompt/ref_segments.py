"""多参（ref2va）分段编排（issue #46）：>15 秒按镜头边界拆 4–15s 执行段，逐段出自包含六段式。

入口 ``run_ref_segmentation`` 是 >15s 多参产物的唯一出口：段提示词、plan.json、
段清单+拼接顺序卡都从这里落盘。三条写段路径与首尾帧链同构（分段写作层三处消费者
同改同测）：

- **规划式（v2）**：``plan_segments(mode="ref")`` 拿镜头切点边界与起止状态 →
  ``write_ref_segment_v2`` 逐段细写；
- **规划失败回退式**：``pack_shots_into_segments`` 机械打包（不均匀硬切、边界落
  切点）→ ``rewrite_ref_segment_prompt`` 逐段重写；
- **机械拼装**：LLM 不可用/调用失败时的诚实地板——定义性段落逐段重复 +
  ``rebase_ref_shot_blocks`` 切片重基，同样出自包含六段式。

多参**没有桥接帧**（锚定由全片共享参考素材承担），所以陪跑里没有剥帧/锚帧，
但**逐段陪跑与两道闸照走**（issue #55/#56）：一次只出当前一段 → 交回本段输出视频
→ **可证层**硬阻断（时长档/尺寸）→ **语义层**剥本段首/末帧读图、与 plan 声明的
起止状态并排交人判 → 未达到给五档出路（含「修改提示词」：只动画面正文再掷）→
落**段产出账**（一本账三类行，见 ``SEGMENT_EVENT_*``）。

全段通过后由 ``assemble_final_film`` **自动接成成片**（issue #57，反转 #39 的
「拼接归用户」）：段间硬切、用各段自身帧，缺一段就拒绝（缺段成片是伪产物）。≤15 秒
直出走同一套收尾——整片六段式即唯一执行段（``single_segment_result``），段数=1 时
拼接退化为拷贝改名。重跑某段后可单独跑一次手动拼接（CLI ``ref-concat``，与自动拼接
同一份编排）。

陪跑中断后由**续跑**接回（issue #58）：``load_ref_segments_for_resume`` 从
``segments/plan.json`` ＋ ``segment-NN.md`` 重建装载（零 LLM 调用），
``read_ref_handoff_progress`` 从段产出账取回已通过的段——**不新增第三本账**（阶段 2
断点防最贵一步重烧、续跑点防问答与计价产物丢失、段产出账的通过行就是逐段陪跑的进度，
三者各防各的）。问答节奏住在 ``ui.wizard``，本模块只放不碰终端的纯件（段展示块、
语义核验块、改写一轮、段产出账读写、续跑装载、成片拼接）。
"""
from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .asset_ledger import sha256_file
from .segment_planner import (
    MAX_SEG_S,
    SegmentPlan,
    pack_shots_into_segments,
    plan_segments,
)
from .segment_prompts import (
    _shot_blocks,
    assemble_ref_segment,
    assert_locked_annotations_intact,
    normalize_ref_segment_output,
    rebase_ref_shot_blocks,
    render_ref_segment_annotations,
    rewrite_ref_segment_prompt,
    split_ref_segment_detailed,
    split_task_type,
    validate_ref_segment,
    write_ref_segment_v2,
)
from .tools.h3_validator import split_ref_sections, validate_ref

def is_single_segment_route(total_s: float) -> bool:
    """本片是否走「整片＝唯一执行段」的直出路线：总时长不超过 H3 **单次生成上限**。

    上限就是规划器的执行段上限 ``MAX_SEG_S``（ref 模式 4–15s 档的顶）——「整片能不能
    一次喂给 H3」与「一段能有多长」是**同一条契约**，所以这里**从常量派生、不写字面 15**：
    重抄一份的话，改上限时这几处会静默漂（本仓「同一份契约不许两处口径」，2026-10-21
    复审实测：这个 15 在本仓已有 `MAX_SEG_S` / `MAX_SEGMENT_SECONDS` 两份真源）。
    """
    return float(total_s) <= MAX_SEG_S


# 机械 summary 固定句：拼装是降级路径，不编剧情摘要（不造窗外事件、不指代前文）
_MECH_SUMMARY_BODY = (
    "This segment plays the shots listed in detailed_description, "
    "using the shared reference assets defined in subject_definitions."
)

# 机械 plan 的退化状态：整片 detailed_description 解析不出任何镜头块时（畸形输入的
# 防御性边界）无从派生画面状态，用**不撒谎的空话**顶上——注记句不退化成「begins with .」，
# 该报的缺段/NO_SHOT 由校验器照报（产物照样交付、问题当面打，不静默）。
_FALLBACK_START_STATE = "the scene at the segment's start"
_FALLBACK_END_STATE = "the scene at the segment's end"


@dataclass
class RefSegmentResult:
    """一次多参分段的落盘结果。issues 逐段全量保留——校验结论绝不静默丢弃。"""

    plans: list[SegmentPlan]
    texts: list[str]
    # 交付时用的全片时长（＝ ``int(round(total_s))``）。**注记句与校验器同这个口径**，
    # 别拿 ``plans[-1].end_s`` 代替：规划器允许末段 end 略超 total（留余量，见
    # ``segment_planner.parse_segment_plan``），拿末段 end 当 total 会让「of the full
    # Ns video」这句对不上，同一个校验器两处接线给出两个结论（复审实测，issue #56）。
    # **不给默认值**：漏传就按 0 校验是静默错，宁可构造时就报缺参数。
    total_s: int
    issues: list[list] = field(default_factory=list)
    segment_paths: list[Path] = field(default_factory=list)
    order_card_path: Path | None = None
    plan_path: Path | None = None
    used_fallback: bool = False
    # 段文本是否带**分段注记**（时间轴注记＋起止状态声明）。>15s 拆段恒为 True；≤15s
    # 直出为 False——整片六段式就是它自己的唯一执行段，注记是给「段与全片对位」用的，
    # 段数=1 时无从对位（见 ``single_segment_result``）。**校验与改写据此选口径**：
    # 带注记走 ``validate_ref_segment``，不带注记走 ``validate_ref``——拿分段校验器去
    # 判直出文本，那两条注记判据是结构性必挂的假错。
    annotated: bool = True
    # 拆段**调用失败**的病因（含尝试次数；issue #67 / ADR 0019）。空串＝没有失败：
    # 纯机械路线（llm=None）与规划成功都是空串，两者不该被说成「失败了」。
    # 只有「回复不合格」也不进这里——那是内容病，调用本身是通的。
    planning_failure: str = ""


def _task_type_of(sections: dict[str, str]) -> str:
    task, _body = split_task_type(sections.get("summary", ""))
    return task or "reference generation"


def _window_blocks(blocks: list[tuple[int, float, str]], window: tuple[int, int],
                   total_s: int) -> list[tuple[int, float, str]]:
    """与时间窗 [start, end) **有重叠**的镜头块（块号, 块首秒, 原文）。

    按块首时间归属会**静默丢内容**（对抗验证 B1 实证）：跨窗长块（如 5–28s 的块）
    只进它开头那个窗，落在后一个窗里的下半段凭空消失。改为重叠归属后长块会重复
    进相邻窗——机械降级路径接受这份重复（空正文比重复更坏，真正按时间切片靠写段
    LLM），规划式路径按 shots_in_segment 精确切，不受此影响。
    """
    start, end = window
    picked: list[tuple[int, float, str]] = []
    for index, (number, at, text) in enumerate(blocks):
        block_end = blocks[index + 1][1] if index + 1 < len(blocks) else total_s
        if at < end and block_end > start:
            picked.append((number, at, text))
    return picked


def _clean_block(text: str) -> str:
    """镜头块原文 → 纯画面句：剥块号与一切 `At MM:SS.mmm`。

    状态声明是画面描述，句里带绝对片时会把「时间戳必须相对本段起点」的纪律从注记句
    破掉（对抗验证 B1 实证的旁路）。
    """
    clean = re.sub(r"^\[Shot\s+\d+\]\s*", "", text.strip())
    clean = re.sub(r"^At\s+\d{2}:\d{2}\.\d{3},?\s*", "", clean)
    return re.sub(r"At\s+\d{2}:\d{2}\.\d{3},?\s*", "", clean).strip()


def _first_sentence(text: str) -> str:
    parts = re.split(r"(?<=[.!?])\s+", _clean_block(text))
    return parts[0].strip() if parts else _clean_block(text)


def _last_sentence(text: str) -> str:
    parts = re.split(r"(?<=[.!?])\s+", _clean_block(text))
    return parts[-1].strip() if parts else _clean_block(text)


def _derive_plan(index: int, window: tuple[int, int], blocks: list[tuple[int, float, str]],
                 total_s: int) -> SegmentPlan:
    """回退路径的机械 plan：起止状态取自窗内首/末镜头块的首末句（英文，进提示词）。"""
    picked = _window_blocks(blocks, window, total_s)
    start, end = window

    start_state = _first_sentence(picked[0][2]) if picked else _FALLBACK_START_STATE
    end_state = _last_sentence(picked[-1][2]) if picked else _FALLBACK_END_STATE
    summary = start_state[:120] + ("…" if len(start_state) > 120 else "")
    return SegmentPlan(
        index=index, start_s=start, end_s=end,
        # 段号用块的真实镜头号（合成 1..n 会让 plan.json 的 shots_in_segment 撒谎）
        shots_in_segment=tuple(number for number, _at, _text in picked),
        summary=summary, end_hook=end_state, start_state=start_state,
    )


def _mechanical_segment(sections: dict[str, str], plan: SegmentPlan,
                        total_segments: int, total_s: int, shot_texts: list[str],
                        task_type: str) -> str:
    """机械拼装（LLM 不可用/失败的诚实地板）：同样出自包含六段式。"""
    return assemble_ref_segment(
        subject_definitions=sections.get("subject_definitions", ""),
        retention_analysis=sections.get("retention_analysis", ""),
        task_type=task_type,
        summary_body=_MECH_SUMMARY_BODY,
        detailed_body=rebase_ref_shot_blocks(shot_texts, plan.start_s),
        overall_soundscape=sections.get("overall_soundscape", "N/A") or "N/A",
        non_diegetic_music=sections.get("non_diegetic_music", "N/A") or "N/A",
        annotations=render_ref_segment_annotations(
            plan.index + 1, total_segments, plan.start_s, plan.end_s,
            int(round(total_s)), plan.start_state, plan.end_hook),
    )


def deliver_ref_segments(*, whole_prompt: str, total_s: float, out_dir: Path,
                         state: dict | None = None, brief=None) -> tuple[RefSegmentResult, str]:
    """多参分段交付（向导与 --brief 快路径共用这一份，issue #46）。

    建模 → 素材登记 → 拆段落盘 → 报告文案；两个 UI 入口只 print 报告，不再各拼
    一份同义流程（复制两份的交付函数迟早漂移）。模型构造失败降级机械拼装（不静默）；
    ``state`` 为空时登记从 ``brief.refs`` 派生（brief 文件流程的编号是用户声明的）。
    返回 ``(结果, 报告全文)``。
    """
    from .generation import ref_asset_registry
    from .model_factory import build_chat_model

    notes: list[str] = []
    try:
        llm = build_chat_model()
    except Exception as exc:  # noqa: BLE001 - 降级机械拆段，不静默失败
        llm = None
        notes.append(f"[提示] 模型构造失败（{exc}）：分段提示词改走机械拼装（定义逐段重复＋切片重基）。")
    registry = ref_asset_registry(state or {}, brief)
    result = run_ref_segmentation(
        whole_prompt=whole_prompt, total_s=total_s, out_dir=out_dir,
        llm=llm, ref_meta=[(r.picture, r.name, r.description) for r in registry])
    report = "\n".join([*notes, format_ref_segment_report(result)])
    return result, report


def format_ref_segment_report(result: RefSegmentResult) -> str:
    """交付报告（向导与 --brief 快路径共用同一份文案，不各拼各的）。

    error 级校验问题逐条当面打——段照样交付（重写预算已在写段层用尽），
    但结论绝不静默（warning 级不打：不阻断交付的提示刷屏只会淹没真问题）。
    """
    lines = [
        f"\n[多参分段] 总时长 >15 秒：已按镜头边界拆成 {len(result.plans)} 个执行段"
        f"（4–15s 整数档，不均匀硬切）："
        + ", ".join(f"{p.start_s}-{p.end_s}s" for p in result.plans),
    ]
    if result.planning_failure:
        # 回退机械拆分本身是对的，静默才是错的（issue #67 / ADR 0019）：把调用层给的
        # 病因原话（含尝试次数）打出来，让用户能分清是网络卡住还是内容不合格。
        lines.append(
            "[提示] 分段规划调用失败，已回退机械拆分（原因与尝试次数如下）：\n"
            f"{result.planning_failure}")
    for position, (plan, path) in enumerate(zip(result.plans, result.segment_paths), 1):
        lines.append(f"  第 {position} 段 {plan.start_s}s-{plan.end_s}s（{plan.duration_s}s）→ {path}")
    if result.order_card_path is not None:
        lines.append(
            f"  段清单与拼接顺序卡：{result.order_card_path}"
            "（拼接归系统：全段验收通过后自动接成成片）")
    for position, issues in enumerate(result.issues, 1):
        for issue in issues:
            if getattr(issue, "severity", "") == "error":
                lines.append(f"  [提醒] 第 {position} 段校验未过 {issue.code}: {issue.message}")
    return "\n".join(lines)


def render_ref_order_card(plans: list[SegmentPlan], segment_paths: list[Path],
                          total_s: int) -> str:
    """段清单 + 拼接顺序卡（段号/起止秒/先后；issue #46 AC4）。

    **拼接归系统**（issue #57 反转 #39 ID13 的「拼接归用户」）：全部段验收通过后自动按
    「先后」列接成成片；重跑某段后可单独跑一次手动拼接命令重出成片（不必从头走流程）。
    「先后」列仍是本卡的内容面——它是拼接顺序的唯一出站依据，自动与手动两条路都照它走。
    """
    lines = [
        "# 段清单与拼接顺序卡（多参分段）",
        "",
        f"总时长 {total_s:g}s → {len(plans)} 个执行段（4–15s 整数档）。"
        "**拼接归系统**：全部段验收通过后自动按「先后」列 1→{} 的顺序接成成片".format(len(plans))
        + "（段间硬切、用各段自身帧）；重跑某段后可单独触发一次手动拼接重出成片"
        "（`uv run launch.py ref-concat <生成目录>`）。"
        "段间没有桥接帧，衔接靠全片共享同一批参考素材与每段起止状态声明对位。",
        "",
        "| 先后 | 段号 | 起止秒 | 本段时长 | 提示词文件 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for position, (plan, path) in enumerate(zip(plans, segment_paths), 1):
        lines.append(
            f"| {position} | Segment {position} | {plan.start_s}s-{plan.end_s}s "
            f"| {plan.duration_s}s | {path.name} |")
    lines.append("")
    lines.append(f"分段规划明细见 plan.json（同目录）。登记工作流见注入卡。")
    return "\n".join(lines) + "\n"


# 段产出账文件名（issue #55 AC5）。住 segments/ 与 plan.json、order-card.md、
# gate-log.jsonl 同址——它是**逐段陪跑的进度**，与首尾帧链的闸门遭遇记录（gate-log）
# 分家：那本记桥接链的锚帧人裁，这本记多参段自己的交接物、遭遇与改写轮次
# （issue #56 AC8 明确「不写首尾帧链的闸门遭遇账」）。
SEGMENT_LEDGER_FILENAME = "segment-ledger.jsonl"

# 行类型判别字段（issue #56）。一本账装三类行，取数必须按 event 过滤：
# - ``output``   一段已交回、可证层放行、且人在语义层判为继续/接受（#55 起）
# - ``encounter``一次语义层遭遇：判定/出路/尝试次数/帧图（#56 AC8，每次遭遇都记）
# - ``rewrite``  「修改提示词」的一轮：意见＋改写前后全文（#56 AC6）
# 为什么不是三个文件：AC8/AC6 都点名「记入段产出账」，而「一段身上发生过什么」
# 只有按段号顺序读同一本才看得出因果链（改词→重掷→又没达到→显式接受）。
SEGMENT_EVENT_OUTPUT = "output"
SEGMENT_EVENT_ENCOUNTER = "encounter"
SEGMENT_EVENT_REWRITE = "rewrite"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def segment_ledger_path(directory: str | Path) -> Path:
    return Path(directory) / "segments" / SEGMENT_LEDGER_FILENAME


def log_segment_output(
    directory: str | Path,
    *,
    segment: int,
    start_s: int,
    end_s: int,
    video_path: str | Path,
) -> Path:
    """追加一行段产出账（``<generation_dir>/segments/segment-ledger.jsonl``）。

    一行＝一段**已交回并通过两道闸**的产出（可证层放行 ＋ 人在语义层判为继续/接受，
    见 issue #56）：被拦下的段、以及判为「重跑本段/修改提示词/停下」的那几轮都不落
    这一行——``event=output`` 的行在不在就是「这段过没过」的判据（**必须按 event 取数**，
    账上还有 encounter/rewrite 两类行，见 ``SEGMENT_EVENT_*``）。

    本行同时是**续跑的进度**（issue #58）：重跑向导时 ``read_ref_handoff_progress`` 按
    ``event=output`` 取回已通过的段，``ui.wizard._run_ref_segment_handoff`` 据此跳段
    ——已通过的段不重出提示词、不重跑、不重付。**不另开一本进度账**：这本账的行本来就
    恰好是「段号＋判定＋视频路径」，再记一份只会长出第二本会漂的真相。

    记 **SHA256** 是为了对抗**文件名可变引用**：只记文件名的话，段视频换版/改名后
    旧产物永久不可复测且无处报错（素材台账同款理由）。摘要**在写入时现算**，不另收
    参数——由调用方传 sha 就多一个「传错了也没人知道」的口子。

    ``verdict`` 也不收参数：它恒为 ``pass``，因为**行在＝两道闸都放行**。它留在行里是
    让账本自己说清这一列是什么，不是给调用方旋的旋钮；语义层那些「没达到」的判定
    走 ``log_segment_encounter``（那是另一类行）。
    """
    path = segment_ledger_path(directory)
    record = {
        "event": SEGMENT_EVENT_OUTPUT,
        "segment": int(segment),
        "start_s": int(start_s),
        "end_s": int(end_s),
        "video": str(Path(video_path).resolve()),
        "sha256": sha256_file(Path(video_path)),
        "verdict": "pass",
        "at": _now(),
    }
    _append_row(path, record)
    return path


def log_segment_encounter(
    directory: str | Path,
    *,
    segment: int,
    verdict: str,
    outcome: str,
    attempt: int,
    first_frame: str | Path | None = None,
    last_frame: str | Path | None = None,
) -> Path:
    """追加一行**语义层遭遇**（issue #56 AC8）：段号 / 判定 / 出路 / 尝试次数 / 帧图。

    「每次遭遇都记」而不是只记失败（同 gate-log 的理由）：将来若要给自动判据取阈值，
    要的是「首末帧读图 ＋ 起止状态声明 ＋ 人的判定」这套**分离边界**——只留失败样本
    会让边界偏斜。帧图路径是事后复跑那次判定的唯一入口，没有它攒下来的只是一堆
    无法重放的结论。

    ``verdict``/``outcome`` 是**人的**结论与选择，原样落盘、不做映射：出路集住在
    ``ui.wizard``（问答节奏归它），本函数不复制一份会漂的字面表。**不写 gate-log**：
    那是首尾帧链的锚帧人裁账，两本混在一起将来分不开（AC8 后半）。
    """
    path = segment_ledger_path(directory)
    record = {
        "event": SEGMENT_EVENT_ENCOUNTER,
        "segment": int(segment),
        "verdict": str(verdict),
        "outcome": str(outcome),
        "attempt": int(attempt),
        "first_frame": str(first_frame) if first_frame else None,
        "last_frame": str(last_frame) if last_frame else None,
        "at": _now(),
    }
    _append_row(path, record)
    return path


def log_segment_rewrite_round(
    directory: str | Path,
    *,
    segment: int,
    feedback: str,
    prompt_before: str,
    prompt_after: str,
    round_index: int | None = None,
) -> Path:
    """追加一行**改写轮次**（issue #56 AC6，形态照抄 ``asset-rounds.jsonl``）。

    一行＝一轮的完整前后对照，只追加不改写——「哪一轮改了什么、谁要求的」事后逐行
    可查。**不设硬上限**：一轮一轮地改是本票的正常节奏，卡上限只会逼人重跑分段规划
    （那是另一条出路，代价高得多）。``round_index`` 缺省按该段已有轮次数自动续号
    （跨调用不依赖内存计数，同 ``asset_loop.log_asset_round``）。
    """
    path = segment_ledger_path(directory)
    existing = [row for row in read_segment_ledger(directory, event=SEGMENT_EVENT_REWRITE)
                if int(row.get("segment", 0)) == int(segment)]
    record = {
        "event": SEGMENT_EVENT_REWRITE,
        "segment": int(segment),
        "round": int(round_index) if round_index is not None else len(existing) + 1,
        "feedback": str(feedback),
        "prompt_before": str(prompt_before),
        "prompt_after": str(prompt_after),
        "at": _now(),
    }
    _append_row(path, record)
    return path


def _append_row(path: Path, record: dict) -> None:
    """追加一行 JSON（父目录按需建；追加式，绝不改写既有行）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_segment_ledger(directory: str | Path, *, event: str | None) -> list[dict]:
    """读段产出账；文件不在＝空账，坏行**响**（ValueError）。

    坏行响不吞：追加账是「这段到底交回过什么」的凭据，静默跳过坏行等于进度缺页
    无人知（同 ``asset_loop.read_asset_rounds``）。

    ``event`` **必填、不给默认值**（复审采纳项）：账上并存 output/encounter/rewrite
    三类行，要「哪几段过了」必须显式点名 ``event=SEGMENT_EVENT_OUTPUT``；想一次看完整
    因果链时显式传 ``event=None``。危险的那条是默认值——留个「不给就返回全部」的默认，
    将来 ``#58`` 的续跑只要漏写一次就把「改了一轮词」读成「这段过了」。
    """
    path = segment_ledger_path(directory)
    if not path.is_file():
        return []
    out: list[dict] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"段产出账第 {lineno} 行不是合法 JSON：{path}") from exc
        if not isinstance(record, dict):
            raise ValueError(f"段产出账第 {lineno} 行不是对象：{path}")
        if event is None or record.get("event") == event:
            out.append(record)
    return out


# --- 成片拼接（issue #57）-----------------------------------------------------
# 成片落生成目录根部：`segments/` 里是逐段的中间物（段提示词、账、帧图），成片是**最终
# 产物**，与 video-prompt.md（整片存档）同级——它是用户在剪辑软件/播放器里要打开的那一个。
FINAL_FILM_FILENAME = "final.mp4"

# 成片短于起步总时长的告警容差：H3 产出**从不是整数秒**（4s 档恒 4.458s，见
# ``preflight.H3_TIER_FRAMES``），所以「等于总时长」的可证形态是**结构**的（各段计划
# 时长之和 ＝ 起步总时长），实测时长只做**只报短**的核对。容差按**一帧/段**给：档位
# 量化误差是帧级的（±2 帧 ≈ 0.08s），跨段累积不会超过这个量级；真短掉一档（如 10s 段
# 交回 7s 档）差的是好几秒，照样咬得住。
_FINAL_FILM_FRAME_S = 1.0 / 24.0


@dataclass
class FinalFilm:
    """一次成片拼接的结果。

    ``path`` 为 None ＝ **没产出成片**，``reason`` 说清卡在哪（缺段／账上视频对不上／
    拼接失败）——缺一段的成片时长对不上总时长，是伪产物，比没有更坏（spec #54）。
    """

    path: Path | None
    how: str            # "copy"（段数=1 退化为拷贝改名）/ "stream-copy" / "re-encode"
    total_s: int        # 起步输入的总时长（用户的目标成片时长）
    # 各段计划时长之和。**可以大于 total_s**：规划器允许末段 end 略超 total 留余量
    # （``segment_planner.parse_segment_plan``），所以成片比起步总时长长一点是设计使然，
    # 不是缺陷——判「段表对不对」要用覆盖式判据（``_coverage_problem``），不是相等。
    planned_s: int
    measured_s: float | None
    reason: str = ""
    warnings: list[str] = field(default_factory=list)


def validate_segment_text(text: str, *, annotated: bool, duration: float | None = None,
                          segment_number: int = 1, total_segments: int = 1,
                          start_s: float = 0.0, total_s: float | None = None,
                          ref_meta: list[tuple[int, str, str]] | None = None) -> list:
    """按段文本的**形态**选校验口径：带分段注记走 ``validate_ref_segment``，不带走 ``validate_ref``。

    口径跟着**文本的形态**走，不跟着「这是第几段」走（``RefSegmentResult.annotated``）：
    ≤15 秒直出的段是整片文本原样（不套注记），拿分段校验器去判它，那两条注记判据
    （``SEGMENT_TIMELINE_MISSING``/``SEGMENT_STATE_MISSING``）是结构性必挂的假错。

    映射**只留这一处**：交付时算一遍、改写后再算一遍，两处各写一遍这个映射，改一处漏
    一处就会让同一份文本「交付时过、改写后报」——issue #56 的假错正是同一种形态
    （同一个校验器两处接线给了两个口径）。
    """
    if annotated:
        return validate_ref_segment(
            text, duration=duration, segment_number=segment_number,
            total_segments=total_segments, start_s=start_s, total_s=total_s,
            ref_meta=ref_meta)
    return validate_ref(text, duration, ref_meta)


def _write_plan_file(seg_dir: Path, plans: list[SegmentPlan]) -> Path:
    """落 ``segments/plan.json``——**唯一**一处写它（两条产出路径同一形态）。

    同一份产物两处各拼一遍，形态就会漂（拆段那处按多段拼、直出那处只写单元素）。它同时
    是**手动拼接命令的段序真源**：形态漂了，将来按行读它的实现就只认得其中一种。
    """
    path = seg_dir / "plan.json"
    path.write_text("[" + ",\n ".join(_plan_json(plan) for plan in plans) + "]",
                    encoding="utf-8")
    return path


def read_plan_file(directory: str | Path) -> list[SegmentPlan]:
    """读 ``segments/plan.json``：段序与各段计划时长的**出站真源**。

    手动拼接命令没有内存里的 ``RefSegmentResult``，只能从盘上取「本片一共几段、顺序
    如何」——不读它就无从判断「某段没过」（AC3 要的正是这句）。
    """
    path = Path(directory) / "segments" / "plan.json"
    if not path.is_file():
        raise FileNotFoundError(f"读不到分段规划：{path}（本片还没拆段，或不是多参生成目录）")
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [SegmentPlan.from_dict(item) for item in raw]


def passed_segment_rows(directory: str | Path) -> dict[int, dict]:
    """段产出账里**已通过**的段 → ``{段号: 行}``（同一段多行取**最后**一行）。

    **必须按 ``event`` 取数**：一本账装 output/encounter/rewrite 三类行，不判别就会把
    「改了一轮词」读成「这段过了」（``read_segment_ledger`` 的 ``event`` 参数就是为此
    必填）。同段多行取最后一行＝最近一次通过的那份视频：重跑过的段，成片要用**新**的
    那一份，不是第一次通过时的那份。
    """
    rows: dict[int, dict] = {}
    for row in read_segment_ledger(directory, event=SEGMENT_EVENT_OUTPUT):
        rows[int(row.get("segment", 0))] = row
    return rows


# --- 逐段陪跑续跑（issue #58）--------------------------------------------------
def load_ref_segments_for_resume(directory: str | Path,
                                 *, total_s: float) -> RefSegmentResult:
    """按盘上的段表与段文件重建 ``RefSegmentResult``——续跑**零 LLM 调用**的装载口。

    续跑不能重跑阶段 2 再「重出」一遍段提示词：那既要重烧组装（最贵一步），又会得到
    **另一套**文本与**另一套**分段边界——而段产出账里的「第 N 段已通过」只对产出它的
    那一套有意义（同 base 路线 ``_cached_plans`` 的纪律）。所以段表取 ``segments/plan.json``
    （段序唯一出站真源），段文本取 ``segments/segment-NN.md``——后者也正是「修改提示词」
    改写后的落点，读回来就是**当前**那份词。

    **缺失/读不出就抛**：段表没了，``passed`` 里的段号指向哪几段就无从谈起；段文件没了，
    重出它要重付 LLM 且新文本会与已通过的那几段脱节。调用方据此响错停住（AC3 裁定：
    账文件不在＝零通过，但段表/段文件不在＝响错）。
    """
    directory = Path(directory)
    try:
        plans = read_plan_file(directory)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        # 「读不回段表」必须是调用方**捕得住**的一种错（按 AC3 响错停住），不是裸 traceback：
        # `read_plan_file` 只对字段走 `from_dict`，列表外层/条目类型错会抛
        # TypeError/AttributeError/KeyError（文件整个不在是 FileNotFoundError，照旧上抛）。
        raise ValueError(
            f"分段规划读不出来（{directory / 'segments' / 'plan.json'}）：{exc}") from exc
    if not plans:
        # 交付恒会写出 ≥1 段，空段表只可能是被改坏/清空过。不拦的话下面「一段都不存在」
        # 会让续跑当成「全部通过」，直接去拼接一条没有段的片子。
        raise ValueError(
            f"分段规划是空的（{directory / 'segments' / 'plan.json'}）："
            "本片一段都没有，无从续跑")
    paths: list[Path] = []
    texts: list[str] = []
    for position in range(1, len(plans) + 1):
        path = directory / "segments" / f"segment-{position:02d}.md"
        if not path.is_file():
            raise FileNotFoundError(
                f"第 {position} 段的提示词文件不见了：{path}"
                "（续跑不重出已交付的段——重出要重付 LLM，且新词会与已通过的那几段脱节）")
        texts.append(path.read_text(encoding="utf-8"))
        paths.append(path)
    # 注记的有无跟**走哪条交付路径**，不跟「段数 > 1」（issue #57 AC5 的直出路线是
    # 1 段且无注记；>15s 也可能只剩 1 段但带注记）。判据与分流那一处**同一条**：
    # `is_single_segment_route`——阈值住在那里、且从常量派生，这里不再写第二遍。
    return RefSegmentResult(
        plans=list(plans), texts=texts, total_s=int(round(total_s)),
        segment_paths=paths, plan_path=directory / "segments" / "plan.json",
        annotated=not is_single_segment_route(total_s))


def rotate_segment_ledger(directory: str | Path) -> Path | None:
    """把上一次陪跑的段产出账挪成 ``.bak``，给**新一次交付**腾出干净的进度记录。

    为什么需要它：``ui.wizard._session_dir`` 只要会话不是 completed 就复用同一个 GEN，
    而用户可以**拒绝续接**、重走一遍流程。那时 ``plan.json``/``segment-NN.md``/
    ``video-prompt.md`` 都会被新一轮交付整份覆盖，唯独这本账是追加式的、从不清理——
    不挪开的话，新一次的陪跑会读到**上一次**的通过行，把「第 1 段已通过」当成这一轮的
    进度照跳（2026-10-21 复审实测：拒绝续接 + 重输同题 → 落回同一 GEN，直接打印
    「第 1/2 段已通过验收，跳过」，而那段视频用户这一轮根本没生成过）。

    挪而不删：旧账是那一次交付的凭据，留着可查；返回新路径供调用方当面说破。
    """
    path = segment_ledger_path(directory)
    if not path.is_file():
        return None
    target = path.with_name(path.name + ".bak")
    index = 1
    while target.exists():
        index += 1
        target = path.with_name(f"{path.name}.bak{index}")
    path.rename(target)
    return target


def read_ref_handoff_progress(directory: str | Path, *,
                              total_segments: int) -> dict[int, dict]:
    """续跑要的「已通过段」→ ``{段号: 行}``；账与段表**对不上就抛**（绝不静默从零开始）。

    ``total_segments`` 是段表的段数（调用方手上就有 ``len(result.plans)``）——只收这个数
    而不是整张段表：本函数**只用它的长度**，收一张表会让读者以为还要拿段内容做点什么。

    进度就是段产出账的 ``event=output`` 行（一段一行、含段号/判定/视频路径/SHA256，
    issue #55）——本票**不新开一本进度账**（AC4：不新增含义重叠的账）。``event`` 必须
    显式点名：同一本账还装着 encounter/rewrite 两类行，不判别就会把「改了一轮词」读成
    「这段过了」（``read_segment_ledger`` 的 event 参数就是为此必填）。

    四条硬核对，任一不过就抛：
    - 账行错形/读不出（``read_segment_ledger`` 管 JSON/对象形态；**缺字段**由下面那条管）；
    - 通过行**缺 AC1 点名的字段**（段号/判定/视频路径）或 SHA256——「行在＝这段过了」这条
      判据建立在**行写全了**之上：一条只剩 ``{"event":"output"}`` 的行不是「这段过了」的
      证据，拿它跳段等于放行一段没人生成过的视频（2026-10-21 复审实测：剥掉 video 后
      续跑照跳、路径为空、还不响）；
    - 段号落在段表之外（账与段表不是同一次拆段的产物）；
    - 通过行**跳号**（陪跑是严格顺序的，中间缺段说明账被删过/改过——按它续跑会跳过
      从没做过的段，正是本票要消灭的形态）。

    账文件**不在**不算错：追加账在「一段都没通过」时本来就不存在（#55 的用例钉着
    这一点），那种情况返回空表、从第 1 段起——调用方会把「已通过 0 段」当面打出来。
    """
    try:
        passed = passed_segment_rows(directory)
    except (AttributeError, TypeError) as exc:
        # 段号字段错形（如 `"segment": null`）在 `int(...)` 处抛的是 TypeError，不是
        # ValueError——不转的话它就绕过调用方的 `except (OSError, ValueError)` 变成裸
        # traceback，而「账行错形要响错停住」是 CONTEXT.md「分段续跑」词条的承诺
        # （2026-10-21 复审实测：`{"event":"output","segment":null}` 复现得到）。
        raise ValueError(f"段产出账的行读不出段号（{segment_ledger_path(directory)}）：{exc}") from exc
    for number, row in sorted(passed.items()):
        missing = [key for key in ("video", "sha256", "verdict")
                   if not str(row.get(key) or "").strip()]
        if missing:
            raise ValueError(
                f"段产出账第 {number} 段的通过行缺字段 {missing}"
                f"（{segment_ledger_path(directory)}）：行在＝这段过了这条判据建立在行写全了"
                "之上，残缺的行不能拿来跳段")
        if str(row.get("verdict")) != "pass":
            raise ValueError(
                f"段产出账第 {number} 段的通过行 verdict 是 {row.get('verdict')!r}，不是 pass："
                "只有通过行才该出现在 event=output 里（判定见 log_segment_output 的注释）")
    total = int(total_segments)
    extra = sorted(number for number in passed if not 1 <= number <= total)
    if extra:
        raise ValueError(
            f"段产出账里有段表之外的段号 {extra}（段表只有 {total} 段）："
            "账与 segments/plan.json 不是同一次拆段的产物，不能拿它续跑")
    numbers = sorted(passed)
    if numbers != list(range(1, len(numbers) + 1)):
        raise ValueError(
            f"段产出账的通过行跳号了：{numbers}——陪跑是严格顺序的，中间缺段说明"
            "账被删过或改过；按它续跑会跳过从没做过的段")
    return passed


def _coverage_problem(plans: list[SegmentPlan], total_s: int) -> str:
    """段表必须无缝覆盖 ``[0, 起步总时长]``；不覆盖就返回一句「哪儿不对」（空串＝没问题）。

    **末段 end 允许略超 total**（规划器留余量：``segment_planner.parse_segment_plan``
    写着「最后一段 end 可以略超 total（留余量）但不许欠」），所以判据是「≥」不是「＝」。
    拿「各段计划时长之和 ＝ 起步总时长」当判据，末段一留余量就会把**全段通过**的片子
    判成「段表与本次生成不是一套」而拒绝出片——同一份契约两处口径，本仓已犯过（issue
    #56 的假 ``SEGMENT_TIMELINE_MISSING`` 就是这个形态；本票复审实测末段超 total 的
    规划产物可达）。
    """
    if not plans:
        return "段表是空的（segments/plan.json 里没有段）"
    if plans[0].start_s != 0:
        return f"段表不从 0 起（首段从 {plans[0].start_s}s 起）"
    for previous, following in zip(plans, plans[1:]):
        if previous.end_s != following.start_s:
            return (f"段表有缺口/重叠：{previous.start_s}-{previous.end_s}s 之后是 "
                    f"{following.start_s}-{following.end_s}s")
    if plans[-1].end_s < total_s:
        return f"段表只覆盖到 {plans[-1].end_s}s，欠起步总时长 {total_s}s"
    return ""


def assemble_final_film(*, directory: str | Path, plans: list[SegmentPlan],
                        total_s: int, rows: dict[int, dict]) -> FinalFilm:
    """把**已通过验收**的各段按段序接成成片（issue #57）。

    ``rows`` 是段产出账里 ``event=output`` 的行（``passed_segment_rows``）；**取数是调用
    方的事，拼不拼由这里说了算**——「未通过即不拼」这条规矩必须住在被拼接的那一处，
    否则它就是一个可以被绕过的调用方约定（本仓「规则在、接线不在」已犯七次）。

    段序取 ``plans``（不是 ``rows`` 的插入顺序、也不是交回顺序）：plan.json 是段序的
    唯一出站真源，自动与手动两条路都照它走。段数=1 时拼接退化为**拷贝改名**——≤15 秒
    直出走的就是这一档，不劳 ffmpeg 一趟（spec #54「段数=1，拼接退化为拷贝改名」）。

    四道可证核对都在这里做，任一不过就**不产出成片**并说清卡在哪：段表不覆盖整条片子
    （``_coverage_problem``）、账上记的视频读不到（改名/搬走）、SHA256 对不上（换版）、
    拼接本身失败。实测时长只做**只报短**的核对（H3 产出比标称长是设计使然，见
    ``_FINAL_FILM_FRAME_S``）。
    """
    from .tools.frame_match import video_meta
    from .tools.seam_audit import merge_videos

    directory = Path(directory)
    planned_s = sum(int(plan.duration_s) for plan in plans)

    def _refuse(reason: str) -> FinalFilm:
        return FinalFilm(path=None, how="", total_s=int(total_s), planned_s=planned_s,
                         measured_s=None, reason=reason)

    missing = [plan.index + 1 for plan in plans if (plan.index + 1) not in rows]
    if missing:
        return _refuse(
            "第 " + "、".join(str(n) for n in missing)
            + f" 段尚未通过验收：缺一段的成片时长对不上起步总时长 {int(total_s)}s，"
            "是伪产物——不产出成片。")
    problem = _coverage_problem(plans, int(total_s))
    if problem:
        return _refuse(f"{problem}：段表与本次生成不是一套，不产出成片。")

    sources: list[Path] = []
    for plan in plans:
        number = plan.index + 1
        row = rows[number]
        video = Path(str(row.get("video", "")))
        if not video.is_file():
            return _refuse(f"第 {number} 段账上记的视频读不到：{video}"
                           "（改名/搬走了？）——不产出成片。")
        recorded = str(row.get("sha256") or "")
        if recorded and sha256_file(video) != recorded:
            return _refuse(
                f"第 {number} 段的文件与验收时记下的 SHA256 不一致：{video}"
                "（换版/被覆盖？）——它已经不是通过验收的那一份，不产出成片。"
                "重跑该段后请用 `ref-concat <生成目录> --replace "
                f"{number} <新视频>` 显式指定。")
        sources.append(video)

    out = directory / FINAL_FILM_FILENAME
    if len(sources) == 1:
        # 段数=1：拼接退化为拷贝改名（≤15 秒直出走这一档）
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.resolve() != sources[0].resolve():
            shutil.copy2(sources[0], out)
        how = "copy"
    else:
        ok, how = merge_videos(sources, out)
        if not ok:
            return _refuse(f"拼接失败（先 stream copy、失败转重编码都没成）：{how}")

    measured = float(video_meta(out).get("duration_s") or 0.0) or None
    warnings: list[str] = []
    if measured is None:
        warnings.append(f"读不到成片的时长元数据：{out}（跳过时长核对）")
    elif measured + len(sources) * _FINAL_FILM_FRAME_S < planned_s:
        warnings.append(
            f"成片实测 {measured:.2f}s，短于各段计划时长之和 {planned_s}s——"
            "可能有段没接上，或某段交回的不是计划时长档的产出")
    return FinalFilm(path=out, how=how, total_s=int(total_s), planned_s=planned_s,
                     measured_s=measured, warnings=warnings)


def format_final_film(film: FinalFilm) -> str:
    """成片结果的当面文案（AC3/AC7）：落点、时长、拼接方式，以及卡在哪一段。

    起步总时长与各段计划时长之和**分别给**：末段留余量时两者不等（成片会比目标长一点），
    只报一个数会让人以为成片超了是缺陷——那是规划器的余量，不是错。
    """
    if film.path is None:
        return "\n[成片] 未产出成片：" + film.reason
    planned = (f"起步总时长 {film.total_s}s"
               if film.planned_s == film.total_s
               else f"起步总时长 {film.total_s}s（各段计划时长之和 {film.planned_s}s，"
                    "末段留余量）")
    lines = [
        f"\n[成片] 已接成成片：{film.path}",
        f"  段间硬切、用各段自身帧；{planned}，"
        + (f"实测 {film.measured_s:.2f}s" if film.measured_s else "实测时长读不出")
        + f"（拼接方式：{'拷贝改名' if film.how == 'copy' else film.how}）。",
    ]
    lines += [f"  [提醒] {note}" for note in film.warnings]
    return "\n".join(lines)


def render_ref_segment_briefing(plan: SegmentPlan, position: int, total: int,
                                path: Path) -> str:
    """当前待办那一段的展示块（issue #55 AC1）：段号 / 起止秒 / 本段时长 / 提示词文件。

    逐段陪跑一次只摆一段，这块是「用户此刻要复制进 ComfyUI 的那一份」的抬头；
    起止秒与本段时长是选时长档的依据（H3 只接受整数档），所以三样同排一行。
    整片六段式已降级为存档预览，不出现在这里（换交付主体）。
    """
    shots = "、".join(str(number) for number in plan.shots_in_segment) or "未标注"
    return "\n".join([
        f"第 {position}/{total} 段 · 起止 {plan.start_s}s-{plan.end_s}s"
        f"（本段时长 {plan.duration_s}s）· 覆盖 Shot {shots}",
        f"提示词文件：{path}",
    ])


def render_ref_segment_semantic_check(
    *,
    plan: SegmentPlan,
    position: int,
    total: int,
    first_description: str | None,
    last_description: str | None,
    first_frame: str | Path | None,
    last_frame: str | Path | None,
) -> str:
    """语义层核验块（issue #56 AC1）：本段起止状态声明与首/末帧实际画面**并排**。

    为什么必须并排、且打在人做决定的那一刻：``start_state``/``end_hook`` 只在展示
    提示词时出现过（比视频交回早十几分钟），剥帧当轮只看得见读图结果——人得在两个
    时刻各记一半、再凭记忆判（桥接链的锚帧语义核验当初就是为这个理由建的）。所以
    排版是判据的一部分：每端的「声明 → 实际画面 → 帧图」三行相邻，中间不插别的。

    读图失败/没取到帧**说破**（"读图失败"／"未取得"）而不是留白：缺一半的并排看起来
    像「没问题」，而事实是「量不出来」——本层由人下结论，人得知道眼前少的是什么。
    """
    def _row(value: str | None, missing: str) -> str:
        return f"    {value if value else missing}"

    return "\n".join([
        f"[本段语义核验] 第 {position}/{total} 段"
        f"（起止 {plan.start_s}s-{plan.end_s}s）",
        "  本段声明的开场状态（plan.start_state）：",
        _row(plan.start_state, "（本段没有留下开场状态声明）"),
        "  首帧实际画面（读图结果）：",
        _row(first_description, "（读图失败：没有可对照的画面描述）"),
        f"  首帧图：{first_frame if first_frame else '（未取得）'}",
        "  本段声明的结束状态（plan.end_hook）：",
        _row(plan.end_hook, "（本段没有留下结束状态声明）"),
        "  末帧实际画面（读图结果）：",
        _row(last_description, "（读图失败：没有可对照的画面描述）"),
        f"  末帧图：{last_frame if last_frame else '（未取得）'}",
    ])


# --- 「修改提示词」一轮（issue #56 AC4/AC5）---------------------------------
# 请求块标题**只有这一处定义**（请求文本、假模型与断言三处各写一遍就是又一个会各自
# 漂移的字面量，同 ``asset_loop.CURRENT_PROMPT_HEAD`` 的纪律）。
REF_SEGMENT_BODY_HEAD = "【本段画面正文】"
REF_SEGMENT_LOCKED_HEAD = "【锁死声明（逐字保留，且不许出现在你的输出里）】"
REF_SEGMENT_FEEDBACK_HEAD = "【改写意见】"


def build_ref_segment_revision_request(*, body: str, locked: str, feedback: str) -> str:
    """「修改提示词」的请求文本（纯函数，返回字符串即契约）。

    ``locked`` 从**段文件自己**取（``split_ref_segment_detailed``），不从 plan 重渲染：
    「只动画面正文」的字面含义就是**其余一字不动**，包括那两句声明——交付时校验器已经
    核过「声明与 plan 对得上」（``SEGMENT_TIMELINE_MISSING``/``SEGMENT_STATE_MISSING``），
    改写这一轮再去重渲染，就成了顺手替用户「修」文件里的别的东西。

    「只动正文、其余逐字保留」这条纪律是硬要求不是客套：提示词是逐轮小步改的，让模型
    顺手「优化」没提到的镜头，跨段一致性就换来整篇漂移（同素材改写与修订基线那句
    「未提到的部分原样保留」的用意）。
    """
    return (
        "下面是一条 MiniMax H3 多参（ref2va）执行段的**画面正文**与它的**锁死声明**。\n"
        "请按用户意见改写画面正文，仍然输出可直接喂给 H3 的正文片段（不要六段式各节标题、"
        "不要解释、不要前后缀）。\n\n"
        "纪律：\n"
        "1. **只改用户意见点名的问题**；没提到的镜头逐字保留——提示词是逐轮小步改的，"
        "全篇重写会把没提到的细节一并漂走。\n"
        "2. **锁死声明不许动，也不许出现在你的输出里**：时间轴注记与起止状态声明由组装器"
        "注入，它和相邻段的衔接咬死（相邻段的末态与下一段起始态是同一瞬间的同一画面），"
        "改了它就要级联改下一段。你只能让正文**兑现**这两句声明，不能改声明本身。\n"
        "3. 用户意见若与锁死声明冲突（例如要求改本段末态画面），照旧只改正文、声明原样"
        "不动——声明写错属于计划错，不在这里改。\n"
        "4. 正文时间基相对本段起点（00:00.000 ＝ 本段第一帧），严禁写进绝对片时；"
        "仍从风格开场句写到最后一个镜头。\n\n"
        f"{REF_SEGMENT_BODY_HEAD}\n{str(body).strip()}\n\n"
        f"{REF_SEGMENT_LOCKED_HEAD}\n{str(locked).strip()}\n\n"
        f"{REF_SEGMENT_FEEDBACK_HEAD}\n{str(feedback).strip()}"
    )


def revise_ref_segment_body(old_text: str, feedback: str, llm) -> str:
    """按用户意见改写一段多参执行段的**画面正文**，返回整段新文本（issue #56 AC4）。

    ``llm`` 由调用方注入（带 ``invoke(str)`` 接口，同 ``rewrite_asset_prompt`` 的口径），
    测试可传 stub。

    **只换正文**：定义块（subject_definitions/retention_analysis）、summary、两条音轨
    与锁死声明都从原文本原样取回，改完由组装器重新拼——「只动画面正文」因此是代码性质，
    不是服从性问题。

    两条抛错（调用方据此保持原词，不采用这一轮）：
    - 空产出：拼出来会是一段没有画面的提示词；
    - 模型改动了锁死声明（``assert_locked_annotations_intact``）——静默放行等于让用户
      拿着一份自相矛盾的提示词去生成，而语义层闸门正是拿那两句当对照物。
    """
    sections = split_ref_sections(old_text)
    locked, body = split_ref_segment_detailed(sections.get("detailed_description", ""))
    request = build_ref_segment_revision_request(body=body, locked=locked, feedback=feedback)
    response = llm.invoke(request)
    text = str(getattr(response, "content", response)).strip()
    if not text:
        raise ValueError("改写结果为空")
    assert_locked_annotations_intact(text, locked)
    # 模型照抄了一遍声明（无害，但同一句话出现两遍只会挤掉正文）：先剥首行注记，
    # 再丢掉与锁定声明逐字相同的那一行——注记以组装器注入的那一份为准。
    _duplicate, new_body = split_ref_segment_detailed(text)
    new_body = "\n".join(
        line for line in new_body.splitlines() if line.strip() != locked.strip()).strip()
    if not new_body:
        raise ValueError("改写结果为空（只剩锁死声明）")
    task_type, summary_body = split_task_type(sections.get("summary", ""))
    return assemble_ref_segment(
        subject_definitions=sections.get("subject_definitions", ""),
        retention_analysis=sections.get("retention_analysis", ""),
        task_type=task_type or _task_type_of(sections),
        summary_body=summary_body,
        detailed_body=new_body,
        overall_soundscape=sections.get("overall_soundscape", ""),
        non_diegetic_music=sections.get("non_diegetic_music", ""),
        annotations=locked,
    )


def run_ref_segmentation(*, whole_prompt: str, total_s: float, out_dir: Path,
                         llm=None,
                         ref_meta: list[tuple[int, str, str]] | None = None) -> RefSegmentResult:
    """把整片 ref 六段式拆成逐段自包含提示词并落盘（issue #46）。

    llm：带 ``invoke(str)`` 的模型；None → 全机械（不静默失败，产物照样完整）。
    ref_meta：素材登记（(picture, name, description)），供「定义逐段重复」校验。
    """
    sections = split_ref_sections(whole_prompt)
    task_type = _task_type_of(sections)
    body = sections.get("detailed_description", "")
    blocks = _shot_blocks(body, keep_cut_timestamp=True) if body.strip() else []
    total_int = int(round(total_s))

    if blocks:
        # 切点钳进 [0, total]：块时间超 total（调用方时长与整片不一致）时不得把窗外切点带进打包
        starts = sorted({int(at) for _n, at, _t in blocks if 0 <= int(at) < total_int})
        if not starts or starts[0] != 0:
            starts = [0, *starts]
        windows = [(starts[i], starts[i + 1]) for i in range(len(starts) - 1)]
        windows.append((starts[-1], total_int))
        windows = [(s, e) for s, e in windows if e > s] or [(0, total_int)]
    else:
        windows = [(0, total_int)]

    plans: list[SegmentPlan] | None = None
    used_fallback = False
    planning_failure = ""
    if llm is not None and blocks:
        cuts = tuple(sorted({w[0] for w in windows} | {total_int}))
        try:
            plans = plan_segments(body, total_int, llm, mode="ref", shot_cuts=cuts)
        except Exception as exc:  # noqa: BLE001 - 规划失败回退机械拆分
            # **不吞病因**（issue #67）：``plan_segments`` 只在调用失败时抛错（卡住到
            # 上限/中流断流/其他异常），文案自带尝试次数。回退本身是对的，静默才是错的
            # ——把原话带进结果，由交付报告当面打给用户。
            plans = None
            planning_failure = str(exc)
    if not plans:
        used_fallback = True
        packed = pack_shots_into_segments(windows, total_int)
        plans = [_derive_plan(index, window, blocks, total_int)
                 for index, window in enumerate(packed)]

    texts: list[str] = []
    for plan in plans:
        if not used_fallback:
            shot_texts = [text for number, _at, text in blocks
                          if number in plan.shots_in_segment] or [
                text for _n, _at, text in _window_blocks(
                    blocks, (plan.start_s, plan.end_s), total_int)]
            text = write_ref_segment_v2(
                plan, plans, whole_sections=sections, shot_texts=shot_texts,
                task_type=task_type, llm=llm, total_s=total_int, ref_meta=ref_meta)
        else:
            shot_texts = [text for _n, _at, text in _window_blocks(
                blocks, (plan.start_s, plan.end_s), total_int)]
            text = None
            if llm is not None:
                raw = rewrite_ref_segment_prompt(
                    whole_prompt=whole_prompt, plan=plan,
                    total_segments=len(plans), total_s=total_int,
                    shot_texts=shot_texts, llm=llm)
                if raw:
                    text = normalize_ref_segment_output(
                        raw, whole_sections=sections, plan=plan,
                        total_segments=len(plans), total_s=total_int, task_type=task_type)
        if text is None:
            text = _mechanical_segment(sections, plan, len(plans), total_int,
                                       shot_texts, task_type)
        texts.append(text)

    seg_dir = out_dir / "segments"
    seg_dir.mkdir(parents=True, exist_ok=True)
    segment_paths: list[Path] = []
    issues: list[list] = []
    for position, (plan, text) in enumerate(zip(plans, texts), 1):
        path = seg_dir / f"segment-{position:02d}.md"
        path.write_text(text, encoding="utf-8")
        segment_paths.append(path)
        issues.append(validate_ref_segment(
            text, duration=plan.duration_s, segment_number=position,
            total_segments=len(plans), start_s=plan.start_s,
            total_s=total_int, ref_meta=ref_meta))

    plan_path = _write_plan_file(seg_dir, plans)
    card_path = seg_dir / "order-card.md"
    card_path.write_text(render_ref_order_card(plans, segment_paths, total_int),
                         encoding="utf-8")
    return RefSegmentResult(
        plans=list(plans), texts=texts, total_s=total_int, issues=issues,
        segment_paths=segment_paths, order_card_path=card_path,
        plan_path=plan_path, used_fallback=used_fallback,
        planning_failure=planning_failure)


def single_segment_result(*, whole_prompt: str, total_s: float, out_dir: Path,
                          ref_meta: list[tuple[int, str, str]] | None = None,
                          ) -> RefSegmentResult:
    """≤15 秒：整片六段式就是本片**唯一一个执行段**（issue #57 AC5）。

    为什么直出路线也要造一个「段」：收尾（交回 → 可证层/语义层两道闸 → 段产出账 →
    成片）**不分叉**（spec #54「≤15 秒统一」）。触发口径仍按总时长分流——直出**不拆段**，
    所以这里的段文本是整片六段式**原样**（不重写、不套分段注记：注记是给「段与全片对位」
    用的，段数=1 时无从对位，见 ``RefSegmentResult.annotated``）。

    段文件/plan.json/顺序卡照落盘，与 >15s 同一套产物：手动拼接命令与续跑读的是它们，
    少一份就要给 ≤15s 单开一条读盘口径。

    起止状态**派生自整片正文自己的首/末镜句**（同机械拆段的 ``_derive_plan``）——语义层
    的对照物因此就是「这份提示词自己声明的开场与收尾」，不是另写一份会漂的声明。
    """
    sections = split_ref_sections(whole_prompt)
    total_int = int(round(total_s))
    body = sections.get("detailed_description", "")
    blocks = _shot_blocks(body, keep_cut_timestamp=True) if body.strip() else []
    _task_type, summary_body = split_task_type(sections.get("summary", ""))
    start_state = _first_sentence(blocks[0][2]) if blocks else _FALLBACK_START_STATE
    end_state = _last_sentence(blocks[-1][2]) if blocks else _FALLBACK_END_STATE
    plan = SegmentPlan(
        index=0, start_s=0, end_s=total_int,
        shots_in_segment=tuple(number for number, _at, _text in blocks),
        # summary 取整片自己的那句（有就原样用；没有才退回开场状态句，不编剧情）
        summary=(summary_body.strip() or start_state),
        end_hook=end_state, start_state=start_state)

    seg_dir = out_dir / "segments"
    seg_dir.mkdir(parents=True, exist_ok=True)
    path = seg_dir / "segment-01.md"
    path.write_text(whole_prompt, encoding="utf-8")
    plan_path = _write_plan_file(seg_dir, [plan])
    card_path = seg_dir / "order-card.md"
    card_path.write_text(render_ref_order_card([plan], [path], total_int),
                         encoding="utf-8")
    return RefSegmentResult(
        plans=[plan], texts=[whole_prompt], total_s=total_int,
        # 直出文本按**不带注记**的形态校验（口径映射只住在 ``validate_segment_text``）
        issues=[validate_segment_text(whole_prompt, annotated=False,
                                      duration=total_int, ref_meta=ref_meta)],
        segment_paths=[path], order_card_path=card_path, plan_path=plan_path,
        annotated=False)


def _plan_json(plan: SegmentPlan) -> str:
    return json.dumps(plan.to_dict(), ensure_ascii=False)
