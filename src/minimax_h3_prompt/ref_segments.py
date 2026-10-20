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
落**段产出账**（一本账三类行，见 ``SEGMENT_EVENT_*``）。问答节奏住在 ``ui.wizard``，
本模块只放不碰终端的纯件（段展示块、语义核验块、改写一轮、段产出账读写）。
拼接仍归用户——成片拼接是 issue #57 的事。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .asset_ledger import sha256_file
from .segment_planner import SegmentPlan, pack_shots_into_segments, plan_segments
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
from .tools.h3_validator import split_ref_sections

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
    issues: list[list] = field(default_factory=list)
    segment_paths: list[Path] = field(default_factory=list)
    order_card_path: Path | None = None
    plan_path: Path | None = None
    used_fallback: bool = False


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


def _derive_plan(index: int, window: tuple[int, int], blocks: list[tuple[int, float, str]],
                 total_s: int) -> SegmentPlan:
    """回退路径的机械 plan：起止状态取自窗内首/末镜头块的首末句（英文，进提示词）。

    状态句剥掉一切 `At MM:SS.mmm`：状态声明是画面描述，句里带绝对片时会把
    「时间戳必须相对本段起点」的纪律从注记句破掉（对抗验证 B1 实证的旁路）。
    """
    picked = _window_blocks(blocks, window, total_s)
    start, end = window

    def _clean_block(text: str) -> str:
        clean = re.sub(r"^\[Shot\s+\d+\]\s*", "", text.strip())
        clean = re.sub(r"^At\s+\d{2}:\d{2}\.\d{3},?\s*", "", clean)
        return re.sub(r"At\s+\d{2}:\d{2}\.\d{3},?\s*", "", clean).strip()

    def _first_sentence(text: str) -> str:
        parts = re.split(r"(?<=[.!?])\s+", _clean_block(text))
        return parts[0].strip() if parts else _clean_block(text)

    def _last_sentence(text: str) -> str:
        parts = re.split(r"(?<=[.!?])\s+", _clean_block(text))
        return parts[-1].strip() if parts else _clean_block(text)

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
    for position, (plan, path) in enumerate(zip(result.plans, result.segment_paths), 1):
        lines.append(f"  第 {position} 段 {plan.start_s}s-{plan.end_s}s（{plan.duration_s}s）→ {path}")
    if result.order_card_path is not None:
        lines.append(
            f"  段清单与拼接顺序卡：{result.order_card_path}"
            "（拼接归用户，按卡片「先后」列 1→N 顺序拼接）")
    for position, issues in enumerate(result.issues, 1):
        for issue in issues:
            if getattr(issue, "severity", "") == "error":
                lines.append(f"  [提醒] 第 {position} 段校验未过 {issue.code}: {issue.message}")
    return "\n".join(lines)


def render_ref_order_card(plans: list[SegmentPlan], segment_paths: list[Path],
                          total_s: int) -> str:
    """段清单 + 拼接顺序卡（段号/起止秒/先后；issue #46 AC4）。拼接归用户。"""
    lines = [
        "# 段清单与拼接顺序卡（多参分段）",
        "",
        f"总时长 {total_s:g}s → {len(plans)} 个执行段（4–15s 整数档）。"
        "**拼接归用户**：请按「先后」列 1→{} 的顺序拼接各段输出视频；".format(len(plans))
        + "段间没有桥接帧，衔接靠全片共享同一批参考素材与每段起止状态声明对位。",
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
    账上还有 encounter/rewrite 两类行，见 ``SEGMENT_EVENT_*``）。拿它跳过已通过段的
    **续跑是 issue #58**，本票只写不读（除测试外没有生产路径读它），别按「已经能续跑」
    去理解这本账。

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


def read_segment_ledger(directory: str | Path, *, event: str | None = None) -> list[dict]:
    """读段产出账；文件不在＝空账，坏行**响**（ValueError）。

    坏行响不吞：追加账是「这段到底交回过什么」的凭据，静默跳过坏行等于进度缺页
    无人知（同 ``asset_loop.read_asset_rounds``）。

    ``event`` 过滤是**取数的正经口**（issue #56）：账上并存 output/encounter/rewrite
    三类行，要「哪几段过了」必须显式点名 ``event=output``；不给就返回全部行（看完整
    因果链时用）。
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
    if llm is not None and blocks:
        cuts = tuple(sorted({w[0] for w in windows} | {total_int}))
        try:
            plans = plan_segments(body, total_int, llm, mode="ref", shot_cuts=cuts)
        except Exception:  # noqa: BLE001 - 规划失败回退机械拆分
            plans = None
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

    plan_path = seg_dir / "plan.json"
    plan_path.write_text(
        "[" + ",\n ".join(_plan_json(p) for p in plans) + "]", encoding="utf-8")
    card_path = seg_dir / "order-card.md"
    card_path.write_text(render_ref_order_card(plans, segment_paths, total_int),
                         encoding="utf-8")
    return RefSegmentResult(
        plans=list(plans), texts=texts, issues=issues,
        segment_paths=segment_paths, order_card_path=card_path,
        plan_path=plan_path, used_fallback=used_fallback)


def _plan_json(plan: SegmentPlan) -> str:
    return json.dumps(plan.to_dict(), ensure_ascii=False)
