"""按镜头拆分并按段重写 H3 视频提示词（长视频分段流水线的提示词侧）。

H3 最终提示词结构（官方 base-en.txt，2026-09-22 迁移）：帧变体对齐指令行 →
integrated_multimodal_description（含 [Shot N] 块，英文）→ overall_soundscape →
non_diegetic_music。soundscape/music 按段重写为英文摘要句（官方 §4.6/§4.7）。

本模块提供两级拆分：
- ``split_shots_from_prompt``：机械拆分，每段 = 前缀 + 单镜头块（时间戳归零）+ 全局声音后缀。
- ``rewrite_segment_prompt``：每段一次轻量 LLM 重写，soundscape/music 为本段重写
  英文摘要句，生成符合官方规范的独立单镜头提示词；失败自动回退机械拆分。

另外提供 ``shots_from_prompt`` 与 ``_shot_blocks``：向导路径下 ShotPlan 为空时，
从提示词的 [Shot N] 块与 At MM:SS.mmm 时间戳反推镜头表（时长 = 下一镜时间戳差值）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import film_language
from .tools.h3_validator import (
    ALIGN_TEMPLATES,
    EDGE_STABILITY_SENTENCE,
    ValidationIssue,
    has_align_instruction,
    only_errors,
    split_ref_sections,
    timestamps_seconds,
    validate_base,
    validate_ref,
)
from .user_revisions import pending_revisions, render_revision_block

# 镜头块标记：[Shot N]（允许中括号与数字间任意空白）
_SHOT_BLOCK = re.compile(r"\[Shot\s+(\d+)\]")
# 「快机位」措辞（issue #11：致闪因素之一）。覆盖实测物证形态
# 「a fast low tracking shot」「at fast speed」，兼顾 rapid/whip 等同义。
_FAST_CAMERA_RE = re.compile(
    r"\b(?:a\s+)?(?:fast|rapid|swift|whip)[\w-]*\s+"
    r"(?:low\s+|high\s+|wide\s+|tight\s+|close\s+|handheld\s+|tracking\s+|pan\s+"
    r"|dolly\s+|zoom\s+|push-in\s+|crane\s+|aerial\s+|overhead\s+)*"
    r"\w*(?:shot|tracking|pan|dolly|zoom|camera move|push-in|crane|handheld)\b"
    r"|at fast speed|rapid(?:ly)?\s+(?:tracking|pan|dolly|zoom|camera)|whip pan"
    r"|fast-paced camera",
    re.IGNORECASE,
)
# 镜头块首的绝对时间戳：[Shot 2] At 00:03.500, the camera ...
_TIMESTAMP_AFTER_TAG = re.compile(r"(\[Shot\s+\d+\])\s*At\s+(\d{2}):(\d{2})\.(\d{3}),?\s*", re.IGNORECASE)
# 镜头区之后属于全局声音/配乐段落的标记（用于界定镜头区结尾）
_SUFFIX_MARKERS = ("overall_soundscape:", "non_diegetic_music:")

# 写段直调的角色名（issue #70 / ADR 0019）：事件、成本表与进度面板都按它分账。
# 四条路径各占一个名字——合成一行的话，成本表上看不出钱花在「写段」还是「回退写段」，
# 而这两条路径的触发条件（规划失败）恰好是用户最需要区分的那种。
SEGMENT_WRITER_ROLE = "role_segment_writer"
SEGMENT_REWRITER_ROLE = "role_segment_rewriter"
REF_SEGMENT_WRITER_ROLE = "role_ref_segment_writer"
REF_SEGMENT_REWRITER_ROLE = "role_ref_segment_rewriter"


def _direct_write(llm, request: str, *, role: str, label: str) -> str | None:
    """写段类直调的唯一出口（issue #70 / ADR 0019）：接看门狗，失败把病因打给人看。

    **为什么在这里打印、而不是把病因往上传**：四个写段函数的返回契约都是
    ``str | None``（调用方据此回退机械拼装/保持原词），改返回值要把四个调用点的
    回退分支全部重写一遍；而票面要的正是「失败那一刻的一行字」——写段是从调用方
    看不到「调了几次、为什么失败」的地方。看门狗已把尝试次数与病因写进
    ``DirectCallResult.error``（卡住 / 中流断流 / 其他异常三种措辞），这里原样转述，
    不另造一套口径（本仓「同一契约不许两处口径」）。

    ``label`` 由调用方给（如「第 2 段回退写段」）：只有它知道段号与路径名，
    这里不猜。
    """
    # 延迟导入：agents 会拉起 langchain 角色装配栈，本模块也被 --brief 快路径直接
    # import（理由同 ``segment_planner.plan_segments``）。
    from .agents import run_direct_call  # noqa: PLC0415

    result = run_direct_call(llm, request, role=role)
    if not result.ok:
        print(f"[警告] {label}调用失败（已试 {result.attempts} 次）：{result.error}",
              flush=True)
        return None
    text = result.text.strip()
    if not text:
        # 空回复同样是失败：旧代码在这里静默返回 None，向导只打一句「跳过」。
        print(f"[警告] {label}返回空文本（已试 {result.attempts} 次）：按失败处理，"
              "不把空段当产物交出去。", flush=True)
        return None
    return text


# 行首裸时间戳（无 [Shot N] 标记的镜头边界），用于"模型漏标镜头号"的兼容降级
_BARE_TIMESTAMP_LINE = re.compile(r"^At\s+(\d{2}):(\d{2})\.(\d{3}),?\s*", re.IGNORECASE)


def _mark_bare_timestamp_shots(prompt: str) -> str:
    """兜底：LLM 漏标 [Shot N] 时，把行首 At MM:SS.mmm 行补上 [Shot N] 再拆分。

    触发条件：只有 1 个真实镜头块（_shot_starts 排除 `(from [Shot N])` 交叉引用），
    但行首存在 ≥1 个裸时间戳；每个裸时间戳行前补 `[Shot N]`，N 递增。
    """
    if len(_shot_starts(prompt)) != 1:
        return prompt  # 0 个（真单段）或多于 1 个（已标好）都不用兜底
    lines = prompt.splitlines(keepends=True)
    first_shot_seen = False
    shot_n = 1
    out: list[str] = []
    changed = False
    for line in lines:
        if _SHOT_BLOCK.search(line):
            first_shot_seen = True
        if (
            first_shot_seen
            and _BARE_TIMESTAMP_LINE.match(line)
            and not _SHOT_BLOCK.search(line)
        ):
            shot_n += 1
            line = f"[Shot {shot_n}] " + line  # 在原行首加镜头标记
            changed = True
        out.append(line)
    return "".join(out) if changed else prompt


@dataclass(frozen=True)
class ShotPrompt:
    """一段可单独执行的镜头提示词。

    - ``text``：提示词正文；
    - ``start_seconds`` / ``duration_seconds``：本段在整条视频里的时间窗，
      供信息卡展示与声音段按窗裁剪；未知为 None。
    """

    shot_number: int
    text: str
    start_seconds: float | None = None
    duration_seconds: float | None = None

    def to_dict(self) -> dict:
        return {
            "shot_number": self.shot_number,
            "text": self.text,
            "start_seconds": self.start_seconds,
            "duration_seconds": self.duration_seconds,
        }


def _shot_starts(prompt: str) -> list[re.Match]:
    """定位真正的镜头块起点，排除 `(from [Shot N])` 之类的交叉引用。

    引用特征：`[Shot N]` 紧跟 `)`（如 `(from [Shot 1]) is fully referenced.`）；
    真实镜头块开头是 `[Shot N] ` + 正文 / `At MM:SS.mmm`。
    """
    starts: list[re.Match] = []
    for match in _SHOT_BLOCK.finditer(prompt):
        rest = prompt[match.end():match.end() + 1]
        if rest == ")":
            continue  # 交叉引用，非镜头块
        starts.append(match)
    return starts


def _end_seconds_for(blocks: list[tuple[int, float, str]], index: int, total_duration: float | None) -> float | None:
    """第 index 段的结束时刻：下一段起点；末段用 total_duration 兜底。"""
    if index + 1 < len(blocks):
        return blocks[index + 1][1]
    return total_duration


def _to_segment_header(header: str) -> str:
    """整片头 → 分段头：把整片锚定行**原位换成**规范的单图开场锚。

    每段只喂一张图，FL2VA/L2VA 的整片锚定行不能留（理由见 ``SEGMENT_ANCHOR_VARIANT``）。
    反过来用「含 Picture 2 就剔」过滤会让整片 FL2VA/L2VA 的锚定行整行消失，段 ≥2
    变成无锚定行——交付闸门随即用 I2VA 措辞报红（issue #9 实测）。
    源提示词不含锚定行时（T2VA / ref 模式）不凭空注入。
    """
    lines = header.splitlines()
    if not any(has_align_instruction(line) for line in lines):
        return header
    replaced = [
        I2VA_ANCHOR_LINE if has_align_instruction(line) else line for line in lines
    ]
    return "\n".join(replaced).rstrip() + "\n\n"


def split_shots_from_prompt(prompt: str, total_duration: float | None = None) -> list[ShotPrompt]:
    """把整条 H3 提示词机械拆成逐镜头提示词。

    - 只有一个（或没有）镜头时原样返回单段（由调用方决定是否回退）。这条路径
      **不**过 ``_to_segment_header``：返回的是整片提示词、锚定行仍是整片形态；
      向导在 ``len(segments) < 2`` 时直接回退为展示整段、不当作执行段交付。
    - 每段 = 全局前缀 + 该镜头块（时间戳归零）+ 全局声音/配乐后缀；
      给出 total_duration 时同时填好每段的时间窗（start/duration）。
    """
    prompt = prompt.strip()
    if not prompt:
        return []
    prompt = _mark_bare_timestamp_shots(prompt)  # 兼容 LLM 漏标 [Shot N] 的裸时间戳镜头
    if total_duration is not None:
        # 总时长同样取整：ComfyUI H3 时长选项只有整数档
        total_duration = float(round(total_duration))
    matches = _shot_starts(prompt)
    if len(matches) < 2:
        return [ShotPrompt(1, prompt)]

    # 镜头区结尾：第一个 [Shot 之后出现的 soundscape/music 标记
    suffix_start = len(prompt)
    for marker in _SUFFIX_MARKERS:
        position = prompt.find(marker, matches[0].start())
        if position != -1:
            suffix_start = min(suffix_start, position)
    header = prompt[: matches[0].start()].rstrip() + "\n\n"
    suffix = prompt[suffix_start:].rstrip() + "\n" if suffix_start < len(prompt) else ""
    body = prompt[matches[0].start():suffix_start]

    blocks = _shot_blocks(prompt)
    inner = _shot_starts(body)
    segments: list[ShotPrompt] = []
    for index, match in enumerate(inner):
        end = inner[index + 1].start() if index + 1 < len(inner) else len(body)
        block = body[match.start():end].strip()
        block = _TIMESTAMP_AFTER_TAG.sub(r"\1 ", block)  # 单镜头视频从 0 秒开始
        segment_header = _to_segment_header(header)
        text = segment_header + block + "\n\n" + suffix

        start_s = blocks[index][1] if index < len(blocks) else None
        end_s = _end_seconds_for(blocks, index, total_duration) if blocks else None
        duration = round(end_s - start_s, 3) if (start_s is not None and end_s is not None) else None
        segments.append(ShotPrompt(
            shot_number=int(match.group(1)), text=text.strip() + "\n",
            start_seconds=start_s, duration_seconds=duration,
        ))
    warn_out_of_range_durations(
        [s.duration_seconds for s in segments if s.duration_seconds is not None]
    )
    return segments


def _shot_blocks(prompt: str, *, keep_cut_timestamp: bool = False) -> list[tuple[int, float, str]]:
    """返回 [(shot_number, start_seconds, block_text)]；[Shot 1] 起点为 0。

    时间戳取整：ComfyUI 的 H3 时长选项只有整数档，LLM 若生成小数
    时间戳（如 8.50s）会产生小数段；此处统一四舍五入到整数秒兜底。

    keep_cut_timestamp：多参机械切片（issue #46）要**保留**切点时间戳再整体重基
    ——合并镜段里的 [Shot 2] 缺切点时间会被校验器判 SHOT_NO_TIMESTAMP。默认
    False 保持首尾帧链的既有语义（块文本剥掉切点时间）。
    """
    matches = _shot_starts(prompt)
    if not matches:
        return []
    suffix_start = len(prompt)
    for marker in _SUFFIX_MARKERS:
        position = prompt.find(marker, matches[0].start())
        if position != -1:
            suffix_start = min(suffix_start, position)
    body = prompt[matches[0].start():suffix_start]
    inner = _shot_starts(body)
    blocks: list[tuple[int, float, str]] = []
    for index, match in enumerate(inner):
        end = inner[index + 1].start() if index + 1 < len(inner) else len(body)
        block = body[match.start():end].strip()
        stamp = _TIMESTAMP_AFTER_TAG.match(block)
        seconds = 0.0
        if stamp:
            minutes, secs, millis = int(stamp.group(2)), int(stamp.group(3)), int(stamp.group(4))
            seconds = float(round(minutes * 60 + secs + millis / 1000.0))
        text = block if keep_cut_timestamp else _TIMESTAMP_AFTER_TAG.sub(r"\1 ", block)
        blocks.append((int(match.group(1)), seconds, text.strip()))
    return blocks


# ComfyUI 的 H3 时长选项区间：段时长必须落在此区间（整数秒）。
# 4–15 档（issue #46 AC6）：59- 工作流时长节点实测 min=4/max=15，旧「只有 4-10」已过时。
MIN_SEGMENT_SECONDS = 4.0
MAX_SEGMENT_SECONDS = 15.0

def warn_out_of_range_durations(durations: list[float], *, stream_print=print) -> None:
    """对落在 4-15s 区间外的段长打印警告（不阻断流程，由人工调整总时长）。"""
    bad = [d for d in durations if not (MIN_SEGMENT_SECONDS <= d <= MAX_SEGMENT_SECONDS)]
    if bad:
        rendered = "、".join(f"{d:g}s" for d in bad)
        stream_print(
            f"[警告] 段时长 {rendered} 超出 ComfyUI H3 可选区间（4-15s 整数），"
            "请人工调整对应镜头时长或总时长"
        )


def shots_from_prompt(prompt: str, total_duration: float):
    """从提示词的 [Shot N] 块 + At MM:SS.mmm 时间戳反推 Shot 列表。

    时长规则：第 N 镜时长 = 第 N+1 镜起点 − 本镜起点；最后一镜 = total_duration − 起点。
    时间戳与总时长均按整数秒处理（ComfyUI H3 时长选项只有 4-10s 整数档）。
    链式字段（previous_shot_id / start_state_derived_from）按顺序接续，满足 ShotPlan.validate。
    """
    from .project_models import Shot  # 延迟导入，避免循环依赖

    total_duration = float(round(total_duration))
    blocks = _shot_blocks(prompt)
    if not blocks:
        return []
    shots: list[Shot] = []
    for index, (shot_number, start, block) in enumerate(blocks):
        if index + 1 < len(blocks):
            duration = blocks[index + 1][1] - start
        else:
            duration = total_duration - start
        if duration <= 0:
            raise ValueError(
                f"[Shot {shot_number}] 时间戳推断出非正时长（起点 {start:g}s，总长 {total_duration:g}s）；"
                "时间戳按整数秒处理，请检查各镜头时间戳是否严格递增且间隔 ≥1s、不超总时长"
            )
        previous_id = f"SH{shot_number - 1:03d}" if index else ""
        one_line = " ".join(block.split())
        summary = one_line[:160] + ("…" if len(one_line) > 160 else "")
        shots.append(Shot(
            shot_id=f"SH{shot_number:03d}",
            shot_number=shot_number,
            duration_seconds=round(duration, 3),
            start_state=summary,
            action=one_line,
            end_state=summary,
            previous_shot_id=previous_id,
            start_state_derived_from=previous_id,
        ))
    return shots


def is_degenerate_durations(durations: list[float]) -> bool:
    """≥3 个镜头且时长全等 → 判定为"无视情节节拍的均分输出"。"""
    return len(durations) >= 3 and len(set(durations)) == 1


# ---------------------------------------------------------------------------
# 按段重写：soundscape/music 按段重写为英文摘要句，生成合规的独立单镜头提示词
# ---------------------------------------------------------------------------

# 分段的锚定行恒为「单图开场锚」形态（即官方 I2VA 行，逐字符固定，base-en.txt 2.1）。
# 每段只喂一张图——段 1 是用户提交的那张，后续段是上一段剥出的桥接帧——且喂进本段
# first frame 槽（wizard._capture_bridge_frame）。brief.variant 描述的是**整片**
# （阶段 2 提示词 + 用户提交哪些帧），逐段执行时由桥接链取代，故分段不得按
# brief.variant 套 ALIGN_TEMPLATES：FL2VA 行会声明用户不喂的 Picture 2，L2VA 行会
# 把这张开场图声明成对齐 S.SS 的尾帧锚（issue #9 裁定 B，2026-09-23）。
# 取值直接取自 validator 的官方模板：模板侧与校验侧同一来源，结构上不可能各说各话。
SEGMENT_ANCHOR_VARIANT = "I2VA"
I2VA_ANCHOR_LINE = ALIGN_TEMPLATES[SEGMENT_ANCHOR_VARIANT]

# ---------------------------------------------------------------------------
# 用户累积修订进分段请求（issue #26）
# ---------------------------------------------------------------------------

# 「不是只落实最后一条」这句不再写进标题：``render_revision_block`` 的收尾句已经说了
# 同一件事，一个块里说两遍只是噪声。
REVISION_CONTEXT_HEADER = "【用户累积修订（长视频的**每一段**都要落实）】"

# 注入优先序：图 > 修订 > 分镜表。图最高沿用既有教条——图是用户提交的**产物**，
# 分镜表是**计划**，成片必须与图连续；修订压过分镜表，因为分镜表是模型按主题写的计划
# 文本，用户的明确要求就是对它的否决。三者不冲突时都要落实。
#
# 这条**判据字符串**是全仓唯一一份常量：测试按它认这条声明
# （`test_segment_prompts` / `test_segment_planner` / `test_wizard_segment_revisions`
# 各有一处断言），向导的写段前核对块把修订块整块照打（用户看到的与模型看到的是同一块）。
# 画面决策块（issue #34）也引它、不另写一句同义的话：同一裁定两套措辞，改一次要散改
# 两处，而本仓已经吃过「同一句原文多份硬拷贝」的亏（#30）。改措辞要 grep 它。
PRIORITY_TIERS = "首帧读图结果 > 用户累积修订 > 分镜表"
REVISION_PRIORITY_NOTE = (
    f"【注入优先序】三者冲突时一律按此裁定：{PRIORITY_TIERS}。"
    "图是用户提交的产物、分镜表只是计划，成片必须与图连续；修订是用户的明确要求，"
    "压过分镜表这类模型生成的计划文本。三者不冲突时都要落实。"
)


def render_revision_context(state: dict) -> str:
    """分段请求共用的「累积用户修订 ＋ 注入优先序」块；没有修订时返回空串。

    为什么做成**一个**函数而不是各拼各的：

    - 分段有两条产出路径（v2 规划式、规划失败回退式），2026-09-22 只改了主路径、回退
      路径的缺陷原样复发过一次（见 ``tests/test_wizard_bridge_anchor.py`` 文件头）；
    - 真源是 ``state["user_revisions"]``（阶段 1 人机循环写入、在持久化白名单里），
      从 state 读意味着**每一段**都拿得到，不必逐段传参。

    取**待落地**的那部分（``user_revisions.pending_revisions``）：已落地的设定级修订
    其要求已经写进设定产物，而设定产物本来就在分段请求里——再带一遍是拿同一条要求
    叠加第二次（issue #28 / T6）。

    分段规划**不依赖 state**（它的入参是显式的），所以那条路径由调用方把本函数的
    返回值经 ``revision_context`` 传进去。

    与 ``user_revisions.render_revision_block`` 的分工：那个只产出**清单正文**，给阶段 1
    的首帧节点用（那里没有「段」这回事）；本函数在它外面加了分段用的标题与优先序声明。
    """
    body = render_revision_block(pending_revisions(state))
    if not body:
        return ""
    return f"{REVISION_CONTEXT_HEADER}\n{body}\n{REVISION_PRIORITY_NOTE}"


def _revision_block_for_request(state: dict) -> str:
    """请求模板里用的修订块：没有修订时返回空串，块尾补一个换行。

    补换行这件事**只在这里做**：两条路径的模板都是逐行 f-string 拼接，分头写「有没有
    修订 + 要不要补换行」迟早漏一处——漏了就是把修订块和下一节黏在同一行，而请求文本
    没有校验器看得住这种错。
    """
    block = render_revision_context(state)
    return f"{block}\n" if block else ""


# ---------------------------------------------------------------------------
# 阶段 1 的画面决策进写段请求（issue #34）
# ---------------------------------------------------------------------------

VISUAL_DECISION_HEADER = "【阶段 1 已定的画面决策（本段落笔依据）】"


def _visual_decision_block_for_request(state: dict, shots: tuple[int, ...] = ()) -> str:
    """请求模板里用的画面决策块：无内容返回空串，有则**自带前后换行**。

    ``shot_review_lock``（分镜圆桌的可生成性结论）与 ``visual_design``（摄影指导的逐镜
    构图／机位运动／光线／景深）**本来就在 state 里、也在持久化白名单里**，此前只是没人
    递给写段——长视频每一段只看到分镜表原文，于是「没水平」在长视频上是结构性的（#30
    记录，``docs/research/2026-09-26-film-language-prompt-sources.md`` P4）。

    两份一起、且**整份**注入，理由：

    - 二者都是**全片**产物（视觉基调、全片硬约束、每个镜头的构图）。逐镜切片会把跨段
      一致性所依据的那半切掉——「同一只猫、同一套色温」正是靠这份全片声明立住的。
      故整份送入，靠块首那条「只取本段 Shot N」的纪律约束时间窗，与既有的
      「时间窗外剧情禁区」同形。分镜表之所以逐镜切（``_segment_shot_texts``），是因为
      它是**会被整段照抄的原文**，且那套切法只认 ``[Shot N]`` 标记；画面细化用 markdown
      标题分镜，不在那套切法的适用面内。
    - 缺任一份只注入另一份（圆桌与摄影指导谁先产出不保证）；两份都缺则整块不出现——
      **不注入空块、不报错**（设定级重跑与降级路径都可能出现空值）。

    前后换行在这里一并做完，不另拆一个「只出内容」的函数：本块只有这一个消费者
    （``render_revision_context`` 之所以分成两层，是因为向导的写段前核对块是第二个读者）。
    换行散到两个调用点去写才是真正的风险——两条写段路径各写一次「有没有块 + 补不补换行」，
    漏一处就是把块和下一节黏在同一行，而请求文本没有校验器看得住这种错。

    末尾那条层级声明引 ``PRIORITY_TIERS``（与修订块同一个常量），不是自己再写一句：
    画面细化由分镜表细化而来、同属「模型生成的计划文本」，故那条裁定对它的细化版同样
    成立。

    **分段规划（segment_planner）不接本块**：本票只管写段。规划师的段尾钩子是锚帧语义
    校验的可判定对照物，要不要给它画面决策得单独裁定——与「词表不进规划师」同一个理由
    （ADR 0007 的「不做」清单）。
    """
    lock = str(state.get("shot_review_lock", "") or "").strip()
    design = str(state.get("visual_design", "") or "").strip()
    if not lock and not design:
        return ""
    scope = "、".join(f"Shot {int(n)}" for n in shots) if shots else "本段时间窗内的镜头"
    lines = [
        VISUAL_DECISION_HEADER,
        f"下面两份是**全片**产物（含其他时间窗的镜头）：本段只取 {scope} 的内容，"
        "其他镜头的事件、构图与机位一个字都不能写进正文（与「时间窗外剧情禁区」同一条纪律）。",
    ]
    if lock:
        lines.append(f"镜头评审锁定（分镜圆桌的可生成性结论，含全片硬约束）：\n{lock}")
    if design:
        lines.append(f"画面细化（摄影指导：每镜的构图／机位运动／光线／景深）：\n{design}")
    lines.append(
        f"优先序照旧（{PRIORITY_TIERS}）：这两份与分镜表同属**计划侧**的细化，"
        "故与首帧图冲突一律以画面为准、与修订冲突一律以修订为准。"
    )
    block = "\n".join(lines)
    return f"\n{block}\n"


# 落笔侧的电影语言切片（issue #32）：整片提示词工程师与两条写段路径拿的是**同一份**
# （``film_language._WRITING_SIDE_SLICES``，"segment_writer" 与 "prompt_engineer" 两个键共用）。
# 这里只取一次、两个模板插同一个常量——两条写段路径各拼各的 f-string，
# 各调一次 ``video_slice`` 迟早漏一处，而漏掉的那条（回退路径）只在规划失败时才走，
# 缺陷要等到规划真的失败才现形（issue #26 的锚定与修订就这么掉过一次）。
# 取不到会**报错**（真源小节标题改了就是改了），不静默少送——本仓的「规则在、接线不在」
# 正是这类静默缺陷，不能靠人发现。
_WRITING_WORDLIST = film_language.video_slice("segment_writer")

_REWRITE_INSTRUCTION = f"""你是 H3 视频提示词工程师。把整条视频的提示词重写为**只覆盖指定时间窗的一段独立单镜头提示词**。

输入：完整提示词（多镜头）+ 本段时间窗（秒）。
输出格式（严格遵守官方 H3 base-en.txt 规范，只输出提示词本身，不要任何解释）：
- 第一行 I2VA 对齐指令（逐字符）：{I2VA_ANCHOR_LINE}
- 空一行；
- integrated_multimodal_description: 只含本段的一个 [Shot 1] 块，英文；正文以 `[Shot 1] ` 标记紧跟字段名开头
  （官方逐字符格式，如 `integrated_multimodal_description: [Shot 1] Live-action, cinematic, a wide shot frames ...`）
  （沿用原 [Shot N] 的画面/运镜/表演描述，**时间戳归零＝相对本段起点计时，绝不得出现绝对片时**，不得虚构原镜头外的内容）；
  实体外观只写在首次出场的 Shot；不写 GLOBAL_LOCK/BRIDGE_FROM/END_HOOK/时长句；
  **每个 Shot 块必须以官方 edge-stability 原句收尾**（逐字符照抄，不改写、不译文）：
  `{EDGE_STABILITY_SENTENCE}`
  官方要求它的理由就是本流程的桥接帧链——抽出的尾帧要保持轮廓锐利，留给下一段当首帧参考；
  **机位与节拍二选一**：快机位（fast/rapid tracking、whip pan、`at fast speed` 等）与 ≥3 个动作节拍
  （正文 ≥3 个 `At 00:XX.XXX`）不得同段共存。**依据只在 4 步采样档成立**——那档同 seed 单变量对照
  （同图、同节拍数，只差机位措辞）：慢机位 0.78 / 快机位+3 拍 4.865，阈值取在 4.1–4.9；
  8 步档下同一文本实测 0.869，但 **8 步的慢机位对照与独立载荷样本都还没有**，所以「8 步下这条处置
  是否还需要」**未验证**：纪律照旧执行，**不许自行放宽**，要放宽先补那两组样本。
  默认降机位为静态/慢速中景、节拍照写，剧情必须快镜时把本段节拍压到 2 个以内；
- overall_soundscape: **为本段重写**（不是从整条裁切，按段重写）：1-4 句 English 连续段落，
  描述本段时间窗内的环境声与动作声，无时间戳（官方 §4.6）；
- non_diegetic_music: **为本段重写**：1-3 句 English，或无声时只写 N/A，无时间戳（官方 §4.7）。

{_WRITING_WORDLIST}
"""


def rewrite_segment_prompt(
    segment: ShotPrompt,
    full_prompt: str,
    llm,
    *,
    state: dict | None = None,
    is_first: bool = False,
    is_last: bool = False,
    position_index: int | None = None,
) -> str | None:
    """用 LLM 把 segment 重写为本段时间窗内的独立提示词；失败返回 None（调用方回退机械拆分）。

    调用走直调看门狗（issue #70 / ADR 0019）：卡住会重试、失败那句会把病因与尝试
    次数打给用户；返回 None 只表示「本段没有可用的重写稿，请回退」，不再等于静默。

    llm 由调用方注入（测试可传 stub）；应为带 ``invoke(str, config=...)`` 接口的对象。

    position_index：本段 0-based 段号，用于从 ``bridge_frame_descriptions`` 按段号取
    桥接帧读图结果（回退路径与 v2 路径同一套锚定）；缺省 None 保持旧行为。

    state / is_first / is_last：首尾帧锚定。分段规划失败时流程会回退到本函数，
    这条路径同样要按「图片实际画面」锚定，否则规划一失败，缺陷就原样复发。
    """
    start = segment.start_seconds or 0.0
    duration = segment.duration_seconds
    # 写段时间基必须是本段 0 起：绝对片时只用于剧情定位，否则 LLM 会把节拍排到片段长度之外
    window = (
        f"0-{duration:g}s（本段时长 {duration:g}s；绝对片时 {start:g}s – {start + duration:g}s "
        "仅作剧情定位，严禁写进提示词的时间戳）"
        if duration is not None
        else f"本段 0 起（时长未知；绝对片时 {start:g}s 起，仅作剧情定位，严禁写进时间戳）"
    )
    anchor = _frame_anchor_note(is_first, is_last, state or {}, segment_index=position_index)
    request = (
        f"{_REWRITE_INSTRUCTION}\n\n本段时间窗：{window}\n"
        f"目标镜头：[Shot {segment.shot_number}]\n"
        f"{anchor}\n"
        f"{_revision_block_for_request(state or {})}"
        # 画面决策同样要进回退路径（issue #34）：这条路径只在规划失败时才走，漏了它
        # 「规划一失败就掉一个档次」——与 issue #26 的锚定/修订同一个坑。
        # 段号取本段的镜头号：回退路径的段是单镜头机械拆分段。
        f"{_visual_decision_block_for_request(state or {}, (segment.shot_number,))}"
        f"\n完整提示词：\n{full_prompt}"
    )
    return _direct_write(
        llm, request, role=SEGMENT_REWRITER_ROLE,
        label=(f"第 {position_index + 1} 段回退写段" if position_index is not None
               else "本段回退写段"))


# ---------------------------------------------------------------------------
# v2：规划式分段（planner 给整秒边界 + 剧情钩子；每段独立细写）
# ---------------------------------------------------------------------------

_SEGMENT_V2_INSTRUCTION = f"""你是 H3 视频提示词的"分段编剧"。为长视频的其中一个 4-10 秒执行段写**可以直接喂给 MiniMax H3 的完整英文提示词**（严格官方 base-en.txt 格式；对白、歌词、画面可见文字保留原文语言）。

H3 是执行型模型：你写什么它就做什么，含糊等于失控；把**不属于本段时间窗的事件**写进任何字段，H3 就会把它们提前演出来。要求：

## ⚠️ 时间窗纪律（最高优先级）
- 你是给「整片 N 秒中的第 [start-end] 秒」写提示词，但**产出的是这一段自己的独立提示词**
- **时间基（最高优先级）：正文里所有 `At 00:XX.XXX` 一律相对本段起点计时**（`00:00.000`＝本段第一帧＝Picture 1），
  取值必须落在 `[0, 本段时长)` 内。绝对片时严禁出现——若本段从整片第 8 秒开始，正文**不能**写 `At 00:08.200`，
  要写 `At 00:00.200`。喂给 H3 的是本段这一条片段，它的时钟从 0 开始；超出时长的节拍会被挤成一团或凭空乱动
- 你只负责写**本段时间窗发生的事**。分析表/人物设定里出现的、发生在其他时间窗的事件（比如整片第 N1 秒才会破碎的窗户、第 N2 秒的转折动作），**本段一个字都不能出现**
- 违反这条 = 本段作废

## 结构（严格官方 base-en.txt 格式，全部英文）
第一行 I2VA 对齐指令（逐字符）：
`{I2VA_ANCHOR_LINE}`
空一行后：
1. `integrated_multimodal_description:` 本段剧情，**默认只有 [Shot 1] 一个镜头块**（段内切镜是显式例外，仅景别跳变等确有必要时）。
   - 正文**以 `[Shot 1] ` 标记紧跟字段名开头**（官方逐字符格式，如 `integrated_multimodal_description: [Shot 1] Live-action, cinematic, a wide shot frames ...`）——
     首行 `(from [Shot 1])` 的引用靠这个标记成立，漏写即引用悬空；标记后先声明风格与初始构图，描述画面必须与 Picture 1（本段首帧图：段 1 是用户提交的首帧，后续段是桥接帧）一致——图片是唯一事实源，文字只做锚定，不要描述图片里不存在的状态
   - **实体外观只写在它首次出场的 Shot 内**，一次写全；未出场的实体不写外观。
     **只有该实体在本段首帧（Picture 1）里确实不存在时**，才可以用一句否定（如 `No cat is visible in the frame.`）；
     首帧图里已经有的实体**绝不能**否定——那等于让 H3 无视你喂进去的图，本段会当场跑偏
   - 时间戳 `At 00:XX.XXX` 写进句子内，**且相对本段起点**（本段第 2.5 秒 → `At 00:02.500, the glass door is slowly pushed open...`）
   - **最后一个时间戳距段尾必须 ≥1s**——关键出场节拍要留展开空间，不许压在段尾
     （此处的「段尾」＝**本段时长**，不是整片时长）
   - **每个 Shot 块收尾**：先让画面自然落定（不写"静止/定格"），然后以官方 edge-stability 原句收尾——
     它必须是**该块的最后一句**；**多镜段里每个 `[Shot N]` 块各加一次**，不是只在段尾加一次，
     逐字符照抄、不改写不译文（官方 base-en.txt 2.1 硬要求；理由见下）：
     `{EDGE_STABILITY_SENTENCE}`
   - **机位与节拍二选一（实测纪律）**：快机位（fast/rapid tracking、whip pan、`at fast speed` 等）
     与 **≥3 个动作节拍**（正文里 ≥3 个 `At 00:XX.XXX`）**不得同段共存**。**依据只在 4 步采样档
     成立**——那档同 seed 单变量对照（同图、同节拍数，只差机位措辞）：慢机位 0.78 / 快机位+3 拍
     4.865，阈值取在 4.1–4.9；8 步档下同一文本实测 0.869。但 **8 步的慢机位对照与独立载荷样本
     都还没有**，所以「8 步下这条处置是否还需要」**未验证**：纪律照旧执行，**不许自行放宽**，
     要放宽先补那两组样本。默认**降机位**：改静态/慢速中景
     （`a static medium shot with small amplitude at slow speed`），节拍照写；剧情确实必须快镜时，
     把本段节拍压缩到 2 个以内，或把后续节拍拆给相邻段
2. `overall_soundscape:` **为本段重写**英文摘要句（官方 §4.6）：1-4 句 English 连续段落，描述本段的环境声与动作声；**无时间戳**。
3. `non_diegetic_music:` **为本段重写**英文摘要句（官方 §4.7）：1-3 句 English，写观众能听到、角色听不到的配乐（乐器/速度/节奏）；无声时只写 `N/A`；**无时间戳**。

## 禁止（官方 base-en.txt 不存在的自创结构，出现即作废）
- 不写 `GLOBAL_LOCK:` 集中定义区（实体外观只进首次出场的 Shot）
- 不写 `BRIDGE_FROM:` 段首状态字段（段首由 Picture 1 锚定句表达）
- 不写 `END_HOOK:` 段尾状态字段（段尾自然收句）
- 不写 `This is a N-second continuous shot.` 时长句（时长由对齐指令承载）
- 不要漏掉块尾的 edge-stability 原句——它**不是**自创结构：官方 base-en.txt 明令要求
  每个镜头块以此句收尾，理由是抽出的尾帧要留给下一段当首帧参考（正是本流程的桥接帧链）
- 不要把未来段的动作提前写进本段
- 不要输出任何解释/markdown/前言；只输出提示词纯正文

{_WRITING_WORDLIST}
"""


def real_frame_descriptions(state: dict) -> dict[str, str]:
    """从 state 取真实帧图的读图结果 ``{role: description}``（无则空 dict）。

    role 为 ``first`` / ``last``；空白描述视为没有（回退计划锚定）。
    """
    out: dict[str, str] = {}
    for item in state.get("fl2va_frame_descriptions") or []:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role", "")).strip()
        description = str(item.get("description", "")).strip()
        if role and description:
            out[role] = description
    return out


def frame_anchor_context(state: dict) -> str:
    """渲染真实首/尾帧画面描述，供分段规划层（segment_planner）锚定；无读图结果返回 ""。"""
    descriptions = real_frame_descriptions(state)
    labels = {"first": "视频第一帧实际画面", "last": "视频最后一帧实际画面"}
    return "\n".join(
        f"{labels[role]}：{descriptions[role]}" for role in ("first", "last") if role in descriptions
    )


# 双向开场一致性纪律句（issue #12，所有段共用）。
# 正向（issue #6，2026-09-22）：图中已有的事物不得写成不存在/尚未出现；
# 反向（issue #12，GEN004 段 2 坏的正是这一半）：图中没有的事物不得写成已存在/
# 从外部进入——桥接帧是门完整关闭、没有猫，正文却写「门被推开、猫已在店内」。
_FRAME_CONSISTENCY_DISCIPLINE = (
    "本段开场状态必须与这张图逐项一致，双向对齐："
    "**图中已有的事物绝不可写成不存在、尚未出现或「等它登场」**"
    "（例如图中猫已站在店内，就不能写「空店」「无猫」「No cat is visible in the frame」"
    "或让它在本段才推门进来）；"
    "**图中没有的事物也绝不可写成已经存在或正在入场**"
    "（例如图中门是关着的、没有猫，就不能写「门被推开」「猫冲进店内」"
    "或「clerk swings into frame」这类与首帧矛盾的开场）。"
    "分镜表、上文「本段剧情概述」与这张图冲突时，一律以画面为准。"
)


def _bridge_frame_descriptions(state: dict) -> dict[int, str]:
    """从 state 取各段桥接帧的读图结果 ``{0-based 段号: 描述}``。

    元素形如 ``{"segment": <段号>, "description": ...}``，由向导在剥出桥接帧后
    立刻读图写入（``bridge_frame_descriptions``）；缺段/空白描述视为没有（诚实降级）。
    """
    out: dict[int, str] = {}
    for item in state.get("bridge_frame_descriptions") or []:
        if not isinstance(item, dict):
            continue
        try:
            segment = int(item.get("segment"))
        except (TypeError, ValueError):
            continue
        description = str(item.get("description", "")).strip()
        if description:
            out[segment] = description
    return out


def _frame_anchor_note(is_first: bool, is_last: bool, state: dict,
                       segment_index: int | None = None) -> str:
    """本段的首/尾帧锚定说明（两条分段路径共用）。

    第 1 段的首帧 = 用户提交的关键帧图 → 注入读图结果，并明令它压过分镜表。
    中间段的首帧 = 桥接帧（上一段剥出的尾帧）→ 向导剥帧后已读图并写入
    ``bridge_frame_descriptions`` 时注入该段的读图结果（issue #12：写段 LLM
    此前对「本段 0.00s 状态」的全部输入都是动作中段的分镜表/剧情概述，在信息上
    不可能写出与桥接帧一致的开场状态）；没有读图结果时只声明图片是唯一事实源。
    末段的尾帧 = 用户提交的尾帧图 → 正文结尾必须落到该状态。

    双向一致性纪律句（``_FRAME_CONSISTENCY_DISCIPLINE``）对**所有**段发出，
    不只第 1 段——GEN004 段 2 正是在中间段把「图中没有的」写成了「正在入场」。
    """
    descriptions = real_frame_descriptions(state)
    lines: list[str] = []
    # 段 1 的 Picture 1 = 用户提交的那张关键帧图：FL2VA/I2VA 是首帧，仅尾帧模式（L2VA）是尾帧
    anchor_role = "first" if "first" in descriptions else "last"
    bridge = _bridge_frame_descriptions(state)
    if segment_index is not None:
        # 段号是权威事实源：is_first 由它派生，杜绝调用方传出自相矛盾的组合
        is_first = segment_index == 0
    elif is_first:
        segment_index = 0
    if is_first and anchor_role in descriptions:
        slot = "首帧图" if anchor_role == "first" else "尾帧图（仅尾帧模式 L2VA）"
        lines.append(f"Picture 1 是用户提交的{slot}，它的实际画面（读图结果，**唯一事实源**）：\n{descriptions[anchor_role]}")
    elif not is_first and segment_index is not None and segment_index in bridge:
        lines.append(
            f"Picture 1 是上一段剥出的桥接帧（上一段末尾画面），它的实际画面（读图结果，**唯一事实源**）：\n{bridge[segment_index]}"
        )
    elif is_first:
        lines.append("Picture 1 是用户提交的首帧图（图片是唯一事实源）")
    else:
        lines.append("Picture 1 是上一段剥出的桥接帧（上一段末尾画面），图片是唯一事实源")
    lines.append(_FRAME_CONSISTENCY_DISCIPLINE)
    if is_last and "last" in descriptions:
        lines.append(
            f"本段是最后一段，视频结束时必须落到用户提交的尾帧实际画面（读图结果，唯一事实源）：\n{descriptions['last']}"
        )
    return "\n".join(lines)


def _shot_text_map(state: dict) -> dict[int, str]:
    """把 state 里的分镜表按 ``[Shot N]`` 切成 ``{shot 号: 该镜原文}``（无标记则空 dict）。"""
    table = str(state.get("shot_table", "") or "").strip()
    if not table:
        return {}
    return {number: text for number, _start, text in _shot_blocks(table)}


def _segment_shot_texts(plan, state: dict) -> tuple[list[str], bool]:
    """本段各 Shot 的原文。返回 ``(文本列表, 是否精确定位到本段镜头)``。

    从分镜表按 ``[Shot N]`` 抽取本段覆盖的镜头——整条分镜表灌进每一段会把其他时间窗的
    事件一起喂给 H3，破坏「时间窗纪律」。
    同一根因早前已被诊断过并命名为 ``extract_segment_shots``
    （`docs/design/2026-09-17-segment-lock-scoping-design.md` 根因 4：
    「``shot_text_{n}`` 键根本不存在 → 回退整张分镜表」），但那份计划从未实现，
    且其配套结构 GLOBAL_LOCK 已被 2026-09-22 官方格式迁移废弃（同批误删的 edge-stability
    句已于 issue #8 恢复——它是官方硬要求，见 ``EDGE_STABILITY_SENTENCE``）。
    """
    table_map = _shot_text_map(state)
    texts: list[str] = []
    exact = True
    for shot_n in plan.shots_in_segment:
        block = str(table_map.get(int(shot_n), "")).strip()
        if block:
            texts.append(block)
        else:
            exact = False
    return texts, (exact and bool(texts))


def build_segment_v2_request(
    plan,  # SegmentPlan
    all_plans: list,
    state: dict,
) -> str:
    """组装 v2 写段请求（官方英文格式模板）。

    plan：当前段的 SegmentPlan；all_plans：全部规划（用于未来段剧情红线与段落定位）。

    累积修订与帧锚定一样**从 state 读**：state 是本请求的既有入参，每一段都会走这条
    组装，于是修订对每一段都可见。这里原先还收一个 ``brief`` 参数——它从头到尾没有任何
    读者，而修订正是被指望经它传进来的（issue #26 的断链）。死参数已删，不是派新用途。
    """
    shots_text, shots_exact = _segment_shot_texts(plan, state)
    if shots_exact:
        shots_block = "本段 Shot 原始描述（只含本段覆盖的镜头，按时间顺序）：\n" + "\n\n".join(shots_text)
    else:
        shots_block = (
            "本段 Shot 原始描述（⚠️ 未能按 Shot 号定位本段镜头，以下是整条分镜表；"
            "**只准取本段时间窗内的内容**，其他镜头的事件一个字都不能出现）：\n"
            + str(state.get("shot_table", ""))
        )

    # 未来段剧情红线：把后续段的 summary 列出来，明令禁止提前出现
    future_events = [
        f"Segment {p.index + 1} ({p.start_s}-{p.end_s}s): {p.summary}"
        for p in all_plans if p.index > plan.index
    ]
    future_block = (
        "\n【时间窗外剧情禁区】以下事件属于其他视频段，本段绝对不能出现其中任何一个：\n"
        + "\n".join(f"- {s}" for s in future_events)
        if future_events else ""
    )

    return (
        f"{_SEGMENT_V2_INSTRUCTION}\n\n"
        f"--- \n"
        f"本段编号：Segment {plan.index + 1}/{len(all_plans)}\n"
        f"写段时间窗：0-{plan.duration_s}s（本段时长 {plan.duration_s}s；"
        f"绝对片时 {plan.start_s}-{plan.end_s}s 仅作剧情定位，严禁写进提示词的时间戳）\n"
        f"包含 Shot：{plan.shots_in_segment}\n"
        f"本段剧情概述（中文，仅供你理解剧情，不得写进提示词）：{plan.summary}\n"
        f"{_frame_anchor_note(plan.index == 0, plan.index == len(all_plans) - 1, state, segment_index=plan.index)}\n"
        f"{_revision_block_for_request(state)}"
        f"{future_block}\n"
        f"\n{shots_block}"
        # 画面决策排在分镜表原文**之后**（判断，不是实测）：GEN004 的真实 state 里两者
        # 互相打架——分镜表 Shot 2 写 `Tracking Shot…at fast speed`，而锁定与画面细化都
        # 明令「取消高速跟踪」——而交付产物照抄了分镜表那条（见 output/issue34-evidence）。
        # 原文在前、最终取舍在后，冲突时写段器最后读到的是**改过的那一版**。
        # 层级不靠位置承载：块内引 PRIORITY_TIERS 明说它低于画面与修订。
        f"{_visual_decision_block_for_request(state, plan.shots_in_segment)}"
    )


# 交付前校验：产出不合格时回插 issue 有界重写。2 = 共 2 次 LLM 调用（首次 + 1 次重写）
MAX_SEGMENT_ATTEMPTS = 2


def _check_fast_camera_multi_beat_coexist(text: str, stamps: list[float],
                                          issues: list[ValidationIssue]) -> None:
    """「快机位 + 多拍动作」同段共存 → error（issue #11，钉 seed 2×2 实测）。

    两因素单独出现都不致闪（R1：快机位 + 2 拍 = 0.173 绿；R2：慢机位 + 3 拍 = 0.78 可接受），
    共存才致闪（00016 0.78 → 00017 4.865，同 seed 只差机位措辞，6.2 倍）。
    故两个条件必须同时命中才报。多拍阈值 ≥3 个时间戳：快机位 + ≥3 拍的物证全部
    落在 4.1–4.9（红），快机位 + 2 拍为 0.173（绿）——阈值落在实测分离边界上。

    **上述阈值与「共存才致闪」的结论都是在 4 步采样下测得的（2026-09-23 复测修正）。**
    钉 seed 单变量对照显示**采样总步数才是主因**：同 seed 同文本同 LoRA，
    4 步 2.715 / 8 步 0.869（3.1 倍）；且本检查判 red 的 armA 文本在 8 步下得 0.869，
    与 4 步下的低运动对照 00016（0.78）相当——被判「必闪」的内容在 8 步下并不闪。
    另：闪动与 **LoRA 标称步数是否与采样步数对齐无关**（4step LoRA + 4 步 = 4.420，
    8step LoRA + 4 步 = 4.473，同 seed 同文本，4 步档五发全落在 2.7–5.7）。

    故本规则**判定逻辑保持不变**，但引用它时必须声明采样步数——4 步是其成立的前提。
    ADR 0003 记此裁定。

    无条件运行（不按 duration 门控）：判定只用绝对节拍数，漏传时长不该让检查静默失效
    ——「规则在、接线不在」的同型风险，不留给未来调用方。
    """
    if len(stamps) < 3:
        return
    if not _FAST_CAMERA_RE.search(text):
        return
    issues.append(ValidationIssue(
        "error", "FAST_CAMERA_MULTI_BEAT_COEXIST",
        f"本段同时存在快机位措辞（fast/rapid tracking 等）与 {len(stamps)} 个动作节拍"
        "——「快机位 + 多拍动作」同段共存实测致闪（4 步采样下 flicker_std 4.1–4.9；"
        "单因素均绿）。默认降机位（改静态/慢速中景）；剧情必须快镜时，把动作节拍"
        "压缩到 2 个以内或拆到相邻段。（该阈值量于 4 步采样：同一文本 8 步下实测 "
        "0.869，故本规则以 4 步为前提，改采样配置前先重测）"))


def validate_segment(text: str, duration: float | None = None,
                     start_s: float = 0.0) -> list[ValidationIssue]:
    """按本段时长校验一段分段提示词（返回 issue 列表，error 级需阻塞）。

    分段路径产出的是**本段自己的独立提示词**，它的 target video 就是这一条片段，
    所以时间戳必须落在 ``[0, duration)`` 内；正文缺 ``[Shot 1]`` 标记会让首行
    ``(from [Shot 1])`` 引用悬空（validator 报 NO_SHOT）。

    校验口径与写段模板同源：分段恒为**单图开场锚**，故 variant 恒取
    ``SEGMENT_ANCHOR_VARIANT``（理由见该常量）。跟着 ``brief.variant`` 走会让
    「整片锚定行漏进段」的产出被判合格，用户直接粘进 H3。

    ``start_s`` 补一条 error 级检查**抓不到**的启发式警告：写成绝对片时的正文，只要绝对
    秒没超过片段长度，就与合法写法**完全同形**（``At 00:04.000`` 究竟是「片段第 4 秒」
    还是「整片第 4 秒」，正则无法区分）。故：本段不从 0 起（``start_s > 0``）却**所有**
    时间戳都 ≥ ``start_s`` → 高度可疑，报 warning（不阻塞，避免误伤合法写法）。
    """
    issues = validate_base(text, duration=duration, variant=SEGMENT_ANCHOR_VARIANT)
    stamps = timestamps_seconds(text)
    _check_fast_camera_multi_beat_coexist(text, stamps, issues)
    _warn_if_timestamps_look_absolute(stamps, start_s, issues)
    return issues


def segment_errors(text: str, duration: float | None = None,
                   start_s: float = 0.0) -> list[ValidationIssue]:
    """只取 error 级 issue（warning 不阻塞交付），供重写循环用。"""
    return only_errors(validate_segment(text, duration, start_s))


def _warn_if_timestamps_look_absolute(stamps: list[float], start_s: float,
                                      issues: list[ValidationIssue]) -> None:
    """时间戳疑似绝对片时的启发式 warning（base 与 ref 分段校验共用这一份）。

    error 级检查抓不到这种：写成绝对片时的正文，只要绝对秒没超过片段长度，
    就与合法写法**完全同形**（`At 00:04.000` 究竟是「片段第 4 秒」还是「整片第
    4 秒」，正则无法区分）。故只对「本段不从 0 起却所有时间戳都 ≥ start_s」报警
    （不阻塞，避免误伤合法写法）。复制两份就是第二处判据——base/ref 一处漂移即分叉。
    """
    if start_s > 0 and stamps and min(stamps) >= start_s:
        issues.append(ValidationIssue(
            "warning", "TIMESTAMPS_LOOK_ABSOLUTE",
            f"本段从整片 {start_s:g}s 起，正文所有时间戳都 ≥ {start_s:g}s（最早 {min(stamps):.3f}s）"
            "——疑似写成绝对片时。正文时间戳必须相对本段起点（0.000＝本段第一帧）"))


def write_segment_v2(
    plan,
    all_plans: list,
    state: dict,
    llm,
) -> str | None:
    """用 LLM 为该段写细颗粒度完整提示词（官方英文格式）。LLM 调用失败返回 None。

    产出先过 ``segment_errors``：有 error 就把 issue 回插请求里有界重写（共
    ``MAX_SEGMENT_ATTEMPTS`` 次 LLM 调用 = 首次 + 1 次重写）。仍不合格时**交回原文**
    而不是 None——返回 None 会被向导当成「生成失败」跳过，用户看不到坏在哪；
    交回原文后由向导把 issue 明细当面打给用户（warning 级不参与重写）。

    调用本身走直调看门狗（issue #70）：卡住会先重试，用尽后打印病因与尝试次数再
    返回 None。``MAX_SEGMENT_ATTEMPTS`` 只管**内容**不合格的重写（issue 回插），
    与看门狗的存活重试是两道不同的预算，别把它们合并成一道。
    """
    duration = float(plan.duration_s)
    start_s = float(plan.start_s)
    base_request = build_segment_v2_request(plan, all_plans, state)
    request = base_request
    text: str | None = None
    for attempt in range(MAX_SEGMENT_ATTEMPTS):
        text = _direct_write(
            llm, request, role=SEGMENT_WRITER_ROLE,
            label=f"第 {plan.index + 1} 段写段")
        if text is None:
            # 调用失败（卡住到上限/中流断流/其他异常）：病因已由 ``_direct_write``
            # 当面打出，这里照老契约返回 None 让调用方回退，但不静默（issue #70）。
            return None
        errors = segment_errors(text, duration, start_s)
        if not errors:
            return text
        if attempt + 1 == MAX_SEGMENT_ATTEMPTS:
            break  # 最后一轮：重试预算已用尽，不再构造没人用的请求
        request = (
            f"{base_request}\n\n"
            "【上一次产出被校验器判为不合格，请按下列问题修正后重写】\n"
            + "\n".join(f"- {i.code}: {i.message}" for i in errors)
        )
    return text


def _ctx(**fields: str) -> str:
    """CONTEXT 行内嵌套：key: value，跳过空值。"""
    lines = [f"- {k}: {v}" for k, v in fields.items() if v]
    return "\n".join(lines) if lines else "（空）"


# ---------------------------------------------------------------------------
# 多参分段（issue #46）：自包含 ref 六段式——组装、时间轴注记、校验
#
# 与首尾帧链的分段是两套锚定语义：多参**没有桥接帧**，跨段连续性靠
# 「每段各自用到的参考素材（段内子集＋重编号）+ 每段起止状态声明 + 时间轴注记对位」；
# issue #9「分段恒单图开场锚」属首尾帧链，ref 段**不得**套 I2VA 锚定行。
# ---------------------------------------------------------------------------

# 时间轴注记首句（canonical，唯一可写来源）：段序 + 起止秒 + 全片时长。
_REF_TIMELINE_NOTE = (
    "Segment {segment_number} of {total_segments}, covering "
    "{start_s}s-{end_s}s of the full {total_s}s video."
)
_REF_START_STATE_PREFIX = "This segment begins with "
_REF_END_STATE_PREFIX = "This segment ends with "

# 指代前文的措辞：每段是独立生成、模型无跨段记忆，「如前所述」对它就是无效信息
# （issue #46）。列表只收**跨段指代**的确定形态；段内多镜的 "the previous shot"
# 是合法叙述，不在禁列。中文条目防写段器偶发混入中文指代（正文应全英文）。
_ANAPHORA_RE = re.compile(
    r"as mentioned (?:before|above|earlier|previously)"
    r"|as (?:described|noted|stated|established|introduced) (?:before|above|earlier|previously)"
    r"|(?:mentioned|described|noted|stated) (?:before|above|earlier|previously)"
    r"|(?:previous|preceding|earlier|following|next) segment"
    r"|earlier in the (?:video|film|story)"
    r"|as we (?:saw|established|learned)"
    r"|see above|from earlier|as before"
    r"|如前所述|如上所述|前面提到|前述|见上文",
    re.IGNORECASE,
)


def _strip_state_period(text: str) -> str:
    """状态句嵌模板前去尾句点：模板自带句点，不叠「. .」。"""
    return str(text).strip().rstrip(".。")


def render_ref_segment_timeline_note(segment_number: int, total_segments: int,
                                     start_s: int, end_s: int, total_s: int) -> str:
    """时间轴注记首句（段序+起止秒+全片时长）。校验器同取本函数——注记与判据不分头写。"""
    return _REF_TIMELINE_NOTE.format(
        segment_number=int(segment_number), total_segments=int(total_segments),
        start_s=int(start_s), end_s=int(end_s), total_s=int(total_s))


def render_ref_segment_annotations(segment_number: int, total_segments: int,
                                   start_s: int, end_s: int, total_s: int,
                                   start_state: str, end_state: str) -> str:
    """时间轴注记 ＋ 起止状态声明的 canonical 文本（issue #46 AC3）。

    多参段间叙事衔接的唯一显式依据就是这两样：注记对位（本段在全片的位置）、
    状态声明（本段首尾瞬间的画面）。三句固定形态，组装器注入、校验器按句检出。
    """
    return (
        f"{render_ref_segment_timeline_note(segment_number, total_segments, start_s, end_s, total_s)} "
        f"{_REF_START_STATE_PREFIX}{_strip_state_period(start_state)}. "
        f"{_REF_END_STATE_PREFIX}{_strip_state_period(end_state)}."
    )


def split_ref_segment_detailed(detailed: str) -> tuple[str, str]:
    """把 detailed_description 拆成 ``(锁死声明, 画面正文)``（issue #56）。

    注记由组装器置于 detailed_description 顶部（``assemble_ref_segment``），所以切点
    就是**首行**：首行是注记形态时归锁定部分，其余全归正文；没有注记行时锁定部分是
    空串、正文原样（不把正文头一行误当注记吃掉）。

    这是「改写只动画面正文」与「那两句被改动就响」两处判据的**共同切点**，而「注记长
    什么样」这件事只有 ``_timeline_note_shape`` 一处（从 canonical 模板派生）——形态
    重抄一份的代价不是重复，而是常量一改它就静默失效。
    """
    first, sep, rest = str(detailed or "").partition("\n")
    if sep and re.match(_timeline_note_shape(), first.strip()):
        return first.strip(), rest.strip()
    return "", str(detailed or "").strip()


def _timeline_note_shape() -> str:
    """``_REF_TIMELINE_NOTE`` → 形态正则（占位符一律换通配），由 canonical 模板**派生**。

    派生而不是字面重抄（ADR 0008 裁定二「只留一处可写来源，其余派生或被钉住」）：重抄
    的代价不是重复，而是**静默失效**——模板一改，那份拷贝就再也匹配不上，拿它做的守卫
    成了永远不响的守卫（「永不为红＝没有」）。骨架（"Segment … of …, covering … of the
    full … video."）保留，只有占位符处放宽。
    """
    return re.sub(r"\\\{[^}]*\\\}", lambda _match: r"[^.\n]*", re.escape(_REF_TIMELINE_NOTE))


def _annotation_like_shape() -> str:
    """注记类句子的识别式：时间轴注记首句 ＋ 起止状态那两句（三条形态全部派生自常量）。

    **每次调用现拼**（不在 import 时编译）：「识别式跟着常量走」因此是**当场可验**的
    ——改掉前缀常量，守卫必须跟着改（有测试钉着，见 ``test_ref_segment_revision.py``）。
    """
    return "|".join([
        re.escape(_REF_START_STATE_PREFIX) + r"[^.\n]*",
        re.escape(_REF_END_STATE_PREFIX) + r"[^.\n]*",
        _timeline_note_shape(),
    ])


def assert_locked_annotations_intact(text: str, locked: str) -> None:
    """改写结果里若出现注记类句子，必须与锁定文本逐字一致，否则**响**（issue #56 AC5）。

    为什么锁死：相邻段的末态与下一段起始态是**同一瞬间的同一画面**，改它就要级联改
    下一段的 start_state 并回写 plan.json；不改而放过，语义层闸门的对照物（声明）与
    正文就失了同步，判据当场作废。声明本身写错属于「计划错了」，走「停下 → 重跑分段
    规划」，不是在这里偷偷改一句。

    判据是**字面**比对，不是「出现过就报」：模型照抄一遍由组装器再注入一次无害，只有
    **改过**的才算越权。调用方（``ref_segments.revise_ref_segment_body``）据此**不采用**
    这一轮改写、保持原词——静默放行等于让用户拿着一份自相矛盾的提示词去生成。
    """
    altered = sorted({match.group(0).strip()
                      for match in re.finditer(_annotation_like_shape(), str(text or ""))
                      if match.group(0).strip() not in str(locked or "")})
    if altered:
        raise ValueError(
            "改写结果动了锁死的时间轴注记/起止状态声明：" + "；".join(altered)
            + "——那两句是段间衔接的显式依据（相邻段的末态与下一段起始态是同一瞬间的"
            "同一画面），只许正文兑现它、不许改它；声明本身写错请停下重跑分段规划")


def assemble_ref_segment(*, subject_definitions: str, retention_analysis: str,
                         task_type: str, summary_body: str, detailed_body: str,
                         overall_soundscape: str, non_diegetic_music: str,
                         annotations: str = "") -> str:
    """拼装一段自包含 ref 六段式（issue #46 AC2）。

    ``subject_definitions`` 与 ``retention_analysis`` 由调用方给定、这里原样拼入，
    不让 LLM 重写——「标签语义逐段一致」于是是代码性质而不是服从性问题
    （官方 ref-en.txt §2：标签一经定义，各段落保持同一含义）。

    但**两者给进来的内容不同**：定义块逐段重复整片（每段独立生成、段 2 不带定义
    就是无参照描述），留存分析只带**本段镜头**的条目（2026-10-26 实测：整片重复会
    把别的段的动作一起交给写段器，段 2 因此重演了段 1 的构图）。裁剪在调用方做
    （``segment_refs.scope_retention_to_shots``），本函数不区分只拼装。

    ``annotations``（时间轴注记＋起止状态声明）置于 detailed_description 顶部。
    summary 的任务类型前缀沿用整片的（同一批素材跨段保持同一外观，任务关系不变）。
    """
    summary = f"[{str(task_type).strip()}] {str(summary_body).strip()}".strip()
    detailed = f"{annotations}\n{detailed_body}".strip() if annotations else str(detailed_body).strip()
    return (
        f"subject_definitions:\n{str(subject_definitions).strip()}\n\n"
        f"summary:\n{summary}\n\n"
        f"retention_analysis:\n{str(retention_analysis).strip()}\n\n"
        f"detailed_description:\n{detailed}\n\n"
        f"overall_soundscape:\n{str(overall_soundscape).strip()}\n\n"
        f"non_diegetic_music:\n{str(non_diegetic_music).strip()}\n"
    )


def validate_ref_segment(text: str, *, duration: float | None = None,
                         segment_number: int = 1, total_segments: int = 1,
                         start_s: float = 0.0, total_s: float | None = None,
                         ref_meta: list[tuple[int, str, str]] | None = None,
                         subject_scope: tuple[int, ...] | list[int] | None = None,
                         ) -> list[ValidationIssue]:
    """多参分段提示词校验（issue #46）：ref 六段式 + 分段专属纪律。

    在 ``validate_ref`` 之上补五条 error 级：

    - ``SEGMENT_TIMELINE_MISSING``：时间轴注记（段序+起止秒）不在——拼接后段与段
      无从对位；注记句由 ``render_ref_segment_timeline_note`` 生成，判据与产出同源；
    - ``SEGMENT_STATE_MISSING``：起止状态声明缺失或空——段间叙事衔接失去显式依据；
    - ``SEGMENT_DEFS_INCOMPLETE``：登记图没全进 subject_definitions——「定义逐段
      重复」（AC2）必须有牙，缺定义的段就是无参照描述；
    - ``SEGMENT_RETENTION_UNDEFINED``：retention_analysis 点名了 subject_definitions
      里没有的 Subject——素材子集与留存分析裁剪不同源时会留下悬空标签（2026-10-26
      段 4 实测：定义块只剩 A，留存分析仍写着另外五个 Subject），模型拿不到那些实体
      的定义，而这一层此前没有任何检查、0 条 issue 静默放过；
    - ``SEGMENT_ANAPHORA``：指代前文的措辞——对无记忆模型是无效信息，出现即作废。

    另带两条既有纪律：时间戳疑似绝对片时（warning，同 ``validate_segment`` 的
    启发式）、「快机位＋多拍」共存（error，同基链；该阈值量于 4 步采样，引用见
    ``_check_fast_camera_multi_beat_coexist``——59- 工作流固定 8 步，纪律照旧执行，
    不自行放宽）。

    ``ref_meta`` / ``subject_scope`` 传**本段子集**（issue #75）：段内只喂用到的图，
    编号从 1 连续，所以「登记图全进定义」（``SEGMENT_DEFS_INCOMPLETE``）与 Subject
    连续性都得按本段范围判，拿整片登记判会把每一段都判红。
    """
    issues = validate_ref(text, duration=duration, ref_meta=ref_meta,
                          subject_scope=subject_scope)
    align_lines = [ln for ln in text.splitlines() if has_align_instruction(ln)]
    if align_lines:
        # issue #9「分段恒单图开场锚」不适用多参（ADR 0011）：锚定行出现在 ref 段
        # 就是把首尾帧链的帧槽语义误用进来——校验必须有牙，不能只靠写段指令自觉。
        issues.append(ValidationIssue(
            "error", "REF_SEGMENT_ALIGN_BANNED",
            f"{len(align_lines)} 行图片对齐指令（I2VA/FL2VA/L2VA 锚定行）混入多参分段"
            "——多参无帧槽可锚（锚定由共享参考素材承担，见 ADR 0011），整行删除"))
    end_s = float(start_s) + float(duration or 0.0)
    total = float(total_s) if total_s is not None else end_s
    expected_note = render_ref_segment_timeline_note(
        segment_number, total_segments, int(round(float(start_s))),
        int(round(end_s)), int(round(total)))
    if expected_note not in text:
        issues.append(ValidationIssue(
            "error", "SEGMENT_TIMELINE_MISSING",
            f"缺少时间轴注记（应含：{expected_note}）——段序+起止秒是拼接对位的唯一依据"))
    if (not re.search(re.escape(_REF_START_STATE_PREFIX) + r"[^.\n]", text)
            or not re.search(re.escape(_REF_END_STATE_PREFIX) + r"[^.\n]", text)):
        issues.append(ValidationIssue(
            "error", "SEGMENT_STATE_MISSING",
            "缺少起止状态声明（This segment begins with … / This segment ends with …）"
            "——多参无桥接帧，段间衔接全靠状态声明显式对位"))
    sections = split_ref_sections(text)
    # 悬空标签：留存分析点名的 Subject 必须有定义。素材子集按图裁、留存分析按镜头
    # 裁，两者不同源——段 4 只剩 A 时，缩到 Shot 4 的那条仍写着另外五个 Subject，
    # 模型引用它们却拿不到定义（2026-10-26 实测，此前这一层 0 条 issue）。
    defined_subjects = {int(n) for n in re.findall(
        r"<Subject\s+(\d+)>", sections.get("subject_definitions", ""))}
    cited_subjects = {int(n) for n in re.findall(
        r"<Subject\s+(\d+)>", sections.get("retention_analysis", ""))}
    dangling = sorted(cited_subjects - defined_subjects)
    if dangling:
        issues.append(ValidationIssue(
            "error", "SEGMENT_RETENTION_UNDEFINED",
            "retention_analysis 点名了 subject_definitions 里没有的 Subject："
            f"<Subject {'>, <Subject '.join(map(str, dangling))}>"
            "——悬空标签让模型引用拿不到定义的实体；"
            "留存分析裁剪必须按本段定义块过滤"))
    if ref_meta:
        defs = sections.get("subject_definitions", "")
        missing = [p for p, _name, _desc in ref_meta if f"<Picture {p}>" not in defs]
        if missing:
            issues.append(ValidationIssue(
                "error", "SEGMENT_DEFS_INCOMPLETE",
                f"subject_definitions 缺登记素材定义：<Picture "
                f"{'>, <Picture '.join(map(str, sorted(missing)))}>"
                "——定义性段落必须逐段重复整片定义（无参照定义＝无参照描述）"))
    anaphora = _ANAPHORA_RE.findall(text)
    if anaphora:
        found = sorted({m if isinstance(m, str) else m[0] for m in anaphora})
        issues.append(ValidationIssue(
            "error", "SEGMENT_ANAPHORA",
            f"出现指代前文的措辞：{'、'.join(found)}——每段是独立生成，"
            "模型无跨段记忆，此类措辞是无效信息（把被指代的内容改写成本段自足的表述）"))
    stamps = timestamps_seconds(text)
    _warn_if_timestamps_look_absolute(stamps, start_s, issues)
    _check_fast_camera_multi_beat_coexist(text, stamps, issues)
    return issues


# --- 多参写段请求（v2 规划式与规划失败回退式共用纪律核心，同改同测）-----------
# 词表切片（``_WRITING_WORDLIST``，ADR 0007 落笔侧同一份）同样只接一次、两条 ref
# 路径共用：写段请求各拼各的词表迟早漏一条，而漏掉的那条只在规划失败时才走。

_REF_SEGMENT_DISCIPLINES = f"""\
为长视频的其中一个 4-15 秒执行段写**可以直接喂给 MiniMax H3 的完整六段式提示词**（严格官方 ref-en.txt 格式，全部英文；对白、歌词、画面可见文字保留原文语言）。

H3 是执行型模型：你写什么它就做什么，含糊等于失控。要求：

## 多参锚定语义（与首尾帧链不同）
- 本段**没有**首帧图、**没有桥接帧**：画面锚定由**本段参考素材**承担（请求给定的 subject_definitions 就是本段要用的那几张图＋它们的定义；段与段各自只带自己用到的素材，外观靠同一批素材图跨段保持一致）
- **不写任何图片对齐指令行**（I2VA/FL2VA/L2VA 锚定行都属于首尾帧路线；「分段单图开场锚」裁定不适用于多参）

## ⚠️ 时间窗纪律（最高优先级）
- 产出的是「整片 N 秒中的第 [start-end] 秒」这一段自己的独立提示词
- 正文时间基相对**本段起点**（00:00.000＝本段第一帧），取值落在 `[0, 本段时长)`；绝对片时严禁写进时间戳
- 本段时间窗**之外**的事件（其他段的剧情）一个字都不能出现——违反＝本段作废
- **禁止指代前文的措辞**（as mentioned before / as described earlier / the previous segment / 如前所述 等）：每段是独立生成，**模型无跨段记忆**，此类措辞是无效信息；被指代的内容要么改写成本段自足的表述，要么不写

## 结构（严格官方 ref-en.txt 六段式，全部英文）
1. `subject_definitions:` **原样复制**请求给定的定义块，一字不改（定义性段落逐段重复；标签语义必须与给定一致，不得新造标签）
2. `summary:` 以给定的任务类型前缀开头（如 `[reference generation]`），一句话概括**本段**的主参照关系
3. `retention_analysis:` **原样复制**请求给定的定义块，一字不改
4. `detailed_description:` 本段正文——先一两句英文风格开场，然后 `[Shot 1]` 起的镜头（首镜无时间戳；后续镜 `At 00:0X.XXX` 相对本段起点）。指称以**语义名**为主，官方 `<Subject N>` / `<Picture N>` 标签在格式要求处兜底。**时间轴注记与起止状态声明由组装器注入，你不要写、也不要改动**。
5. `overall_soundscape:` 为本段重写的英文摘要句（1-4 句连续段落，无时间戳，官方 §4.6）
6. `non_diegetic_music:` 为本段重写的英文摘要句（1-3 句；无声写 `N/A`；无时间戳，官方 §4.7）

## 禁止（出现即作废）
- 不写 `GLOBAL_LOCK:` / `BRIDGE_FROM:` / `END_HOOK:` / 首行时长句 / 图片对齐指令行（均属首尾帧链或自创结构）
- 不写时间轴注记与起止状态声明（组装器注入）
- 不要把未来段的动作提前写进本段
- 不要输出任何解释/markdown/前言；只输出提示词纯正文

## 词表切片（ADR 0007 落笔侧同一份）
下面词表**前言**里「edge-stability 句」「分段首行的锚定行」两处指针是首尾帧链的义务，多参分段**一概不用**（见上方禁止清单；多参格式以官方 ref-en.txt 为准，其完整示例就没有这两样）。词表正文的选词与写法照用。

{_WRITING_WORDLIST}
"""

_REF_SEGMENT_V2_INSTRUCTION = (
    "你是 H3 视频提示词的\"分段编剧·多参（ref2va）\"。\n" + _REF_SEGMENT_DISCIPLINES
)
_REF_SEGMENT_REWRITE_INSTRUCTION = (
    "你是 H3 视频提示词工程师·多参（ref2va）。把整条视频的六段式提示词重写为**只覆盖指定时间窗的一段独立提示词**。\n"
    + _REF_SEGMENT_DISCIPLINES
)


def _ref_defs_request_block(sections: dict[str, str]) -> str:
    """请求里的「定义块」：v2 与回退式下发同一份（同改同测的物证点）。

    调用方传**本段作用域**的 sections（issue #75：子集＋段内新号）。请求里只出现本段
    要用的素材定义——整片 24 张定义下发给写段器，产出就会照着 24 张写，段内接线接不下。
    ``retention_analysis`` 同样只带本段镜头条目（``scope_retention_to_shots``）：那里面
    写的是逐镜头的动作，整片下发等于把别的段的剧情也交给写段器（2026-10-26 实测）。
    """
    return (
        "\nsubject_definitions 定义块（**原样复制**进你的输出，一字不改）：\n"
        f"{str(sections.get('subject_definitions', '')).strip()}\n"
        "\nretention_analysis 定义块（**原样复制**进你的输出，一字不改）：\n"
        f"{str(sections.get('retention_analysis', '')).strip()}\n"
    )


def build_ref_segment_v2_request(plan, all_plans: list, *,
                                 sections: dict[str, str],
                                 shot_texts: list[str], task_type: str,
                                 total_s: float) -> str:
    """组装多参 v2 写段请求（issue #46）。

    入参全部显式（不依赖 state）：多参段的锚定素材是**本段**定义块＋本段镜头原文，
    修订/帧锚/画面决策那套是首尾帧链的注入通道，多参路线没有它们的语义。
    注记提示句取 ``render_ref_segment_annotations``（与组装器/校验器同源），
    请求里不手抄第二份注记形态。
    """
    shots_block = (
        "本段 Shot 原文（只含本段覆盖的镜头，按时间顺序；仅供理解，输出要按本段时间基重写）：\n"
        + "\n\n".join(shot_texts)
        if shot_texts else
        "（缺本段镜头原文：按本段时间窗、剧情概述与起止状态写，不得虚构窗外事件）"
    )
    future_events = [
        f"Segment {p.index + 1} ({p.start_s}-{p.end_s}s): {p.summary}"
        for p in all_plans if p.index > plan.index
    ]
    future_block = (
        "\n【时间窗外剧情禁区】以下事件属于其他视频段，本段绝对不能出现其中任何一个：\n"
        + "\n".join(f"- {s}" for s in future_events)
        if future_events else ""
    )
    annotations_hint = render_ref_segment_annotations(
        plan.index + 1, len(all_plans), plan.start_s, plan.end_s, int(round(total_s)),
        plan.start_state, plan.end_hook)
    return (
        f"{_REF_SEGMENT_V2_INSTRUCTION}\n\n"
        f"--- \n"
        f"本段编号：Segment {plan.index + 1}/{len(all_plans)}\n"
        f"写段时间窗：0-{plan.duration_s}s（本段时长 {plan.duration_s}s；"
        f"绝对片时 {plan.start_s}-{plan.end_s}s 仅作剧情定位，严禁写进提示词的时间戳）\n"
        f"时间轴注记与起止状态声明（组装器注入，你不要写；正文叙事须与状态一致）：\n"
        f"{annotations_hint}\n"
        f"本段剧情概述（中文，仅供你理解剧情，不得写进提示词）：{plan.summary}\n"
        f"{future_block}\n"
        f"\n{shots_block}"
        f"{_ref_defs_request_block(sections)}"
    )


def rewrite_ref_segment_prompt(*, base_prompt: str, plan, total_segments: int,
                               total_s: int, shot_texts: list[str], llm) -> str | None:
    """回退式（规划失败）：LLM 把整片六段式重写为本段时间窗的独立提示词；失败返回 None。

    调用走直调看门狗（issue #70）：卡住会重试，失败那句把病因与尝试次数打给用户，
    再由调用方回退机械拼装——回退本身是对的，静默才是错的（ADR 0019）。

    ``base_prompt`` 是**本段作用域**的整片提示词（issue #75：定义块已裁成子集＋段内
    新号）。它是「取材上下文」，塞整片 24 张定义进去，模型就会照着 24 张写。

    产出同样要过 ``normalize_ref_segment_output`` 归一（调用方做）：定义逐段重复
    与注记注入不靠 LLM 服从性。纪律声明与 v2 请求共用 ``_REF_SEGMENT_DISCIPLINES``
    ——两条路径各拼各的纪律文本，漏一条就等规划一失败掉一个档次。注记提示句同取
    ``render_ref_segment_annotations``（plan 携带段序/起止/状态，不拆成参数串）。
    """
    sections = split_ref_sections(base_prompt)
    shots_block = (
        "本段 Shot 原文（按时间顺序；输出要按本段时间基重写并重新编号为 [Shot 1] 起）：\n"
        + "\n\n".join(shot_texts)
        if shot_texts else
        "（缺本段镜头原文：按本段时间窗写，不得虚构窗外事件）"
    )
    annotations_hint = render_ref_segment_annotations(
        plan.index + 1, total_segments, plan.start_s, plan.end_s,
        int(round(total_s)), plan.start_state, plan.end_hook)
    request = (
        f"{_REF_SEGMENT_REWRITE_INSTRUCTION}\n\n"
        f"--- \n"
        f"写段时间窗：0-{plan.duration_s}s（本段时长 {plan.duration_s}s；"
        f"绝对片时 {plan.start_s}-{plan.end_s}s 仅作剧情定位，严禁写进提示词的时间戳）\n"
        f"时间轴注记与起止状态声明（组装器注入，你不要写；正文叙事须与状态一致）：\n"
        f"{annotations_hint}\n"
        f"\n{shots_block}"
        f"{_ref_defs_request_block(sections)}"
        f"\n完整提示词（按本段参考素材裁剪后的六段式，供你取材；只准取本段时间窗内的内容）："
        f"\n{base_prompt}"
    )
    return _direct_write(
        llm, request, role=REF_SEGMENT_REWRITER_ROLE,
        label=f"第 {plan.index + 1} 段多参回退写段")


# summary 任务类型前缀（官方 ref-en.txt §3）：解析**只此一份**，组装归一与编排取任务关系共用。
_TASK_TYPE_PREFIX_RE = re.compile(r"\s*\[([a-z\s+]+)\]\s*")


def split_task_type(summary: str) -> tuple[str, str]:
    """summary 段 → (任务类型, 去掉前缀的正文)。无前缀时任务类型返回空串。"""
    match = _TASK_TYPE_PREFIX_RE.match(str(summary))
    if not match:
        return "", str(summary).strip()
    return match.group(1).strip(), str(summary)[match.end():].strip()


def normalize_ref_segment_output(text: str, *, sections: dict[str, str],
                                 plan, total_segments: int, total_s: float,
                                 task_type: str) -> str:
    """LLM 产出 → 自包含六段式的归一化（v2 与回退式共用，issue #46 AC2）。

    定义性段落换成**本段作用域**的原文（issue #75：子集＋段内新号，逐段重复由代码
    保证），时间轴注记＋起止状态声明按 plan 注入 detailed_description 顶部；LLM 若
    自写了注记行，先剥掉再注入（注记以组装器为准，重复两遍只会挤掉正文）。
    """
    parts = split_ref_sections(text)
    if not parts:
        return str(text).strip()  # 段落都没解析出来：原样交回，由校验器报缺段（不拼假内容）
    annotations = render_ref_segment_annotations(
        plan.index + 1, total_segments, plan.start_s, plan.end_s, int(round(total_s)),
        plan.start_state, plan.end_hook)
    effective_task, summary_body = split_task_type(parts.get("summary", ""))
    effective_task = effective_task or task_type
    # 剥掉 LLM 自写的注记行（注记以组装器为准，重复两遍只会挤掉正文）。判据与
    # 「改写只动正文」共用 ``split_ref_segment_detailed``——两处各写一遍「首行是不是
    # 注记」就是又一个会各自漂移的切点。
    _self_written, detailed = split_ref_segment_detailed(
        parts.get("detailed_description", ""))
    return assemble_ref_segment(
        subject_definitions=sections.get("subject_definitions")
        or parts.get("subject_definitions", ""),
        retention_analysis=sections.get("retention_analysis")
        or parts.get("retention_analysis", ""),
        task_type=effective_task,
        summary_body=summary_body,
        detailed_body=detailed,
        overall_soundscape=parts.get("overall_soundscape", ""),
        non_diegetic_music=parts.get("non_diegetic_music", ""),
        annotations=annotations,
    )


def write_ref_segment_v2(plan, all_plans: list, *, sections: dict[str, str],
                         shot_texts: list[str], task_type: str, llm,
                         total_s: float,
                         ref_meta: list[tuple[int, str, str]] | None = None,
                         subject_scope: tuple[int, ...] | list[int] | None = None,
                         ) -> str | None:
    """多参 v2 写段（issue #46）：LLM 细写 → 归一化 → 校验，有界重写。

    契约同 ``write_segment_v2``：LLM 调用失败返回 None；重试耗尽仍不合格时
    **交回原文**（当面给用户看问题），不返回 None 装死。调用走直调看门狗
    （issue #70）：卡住会先重试，失败那句把病因与尝试次数打给用户再回退。

    ``sections`` / ``ref_meta`` / ``subject_scope`` 都是**本段作用域**（issue #75）：
    请求只带本段定义块，校验也按本段登记判——拿整片登记判段产物，``PICTURE_UNUSED``
    会把每一段都判红（本段本来就用不到别的图）。
    """
    base_request = build_ref_segment_v2_request(
        plan, all_plans, sections=sections,
        shot_texts=shot_texts, task_type=task_type, total_s=total_s)
    request = base_request
    text: str | None = None
    for attempt in range(MAX_SEGMENT_ATTEMPTS):
        raw = _direct_write(
            llm, request, role=REF_SEGMENT_WRITER_ROLE,
            label=f"第 {plan.index + 1} 段多参写段")
        if raw is None:
            # 调用失败：病因已当面打出（issue #70），照老契约返回 None 由调用方
            # 回退机械拼装——回退是对的，静默才是错的。
            return None
        text = normalize_ref_segment_output(
            raw, sections=sections, plan=plan,
            total_segments=len(all_plans), total_s=total_s, task_type=task_type)
        errors = [i for i in validate_ref_segment(
            text, duration=plan.duration_s, segment_number=plan.index + 1,
            total_segments=len(all_plans), start_s=plan.start_s, total_s=total_s,
            ref_meta=ref_meta, subject_scope=subject_scope) if i.severity == "error"]
        if not errors:
            return text
        if attempt + 1 == MAX_SEGMENT_ATTEMPTS:
            break  # 重试预算用尽：交回原文，由调用方把 issue 明细当面打给用户
        request = (
            f"{base_request}\n\n"
            "【上一次产出被校验器判为不合格，请按下列问题修正后重写】\n"
            + "\n".join(f"- {i.code}: {i.message}" for i in errors)
        )
    return text


# 行内节拍时间戳（At MM:SS.mmm）与块首切点时间戳：机械切片整体重基用。
# 块首只认 `\A` 锚定的切点时间——不锚定会把首块**句中**的第一个节拍当切点删掉
# （对抗验证 B1 实证：合法句 "Wide view. At 00:02.500, gulls scatter" 被静默吞节拍）。
_AT_TIME_RE = re.compile(r"At\s+(\d{2}):(\d{2})\.(\d{3})")
_LEADING_AT_RE = re.compile(r"\AAt\s+\d{2}:\d{2}\.\d{3},?\s*")


def _rebase_at(match: re.Match, start_s: float) -> str:
    """绝对片时 → 相对本段起点（负值归零）；段窗口 ≤15s，分恒为 00。"""
    total = int(match.group(1)) * 60 + int(match.group(2)) + int(match.group(3)) / 1000.0
    rebased = max(0.0, total - float(start_s))
    secs = int(rebased)
    millis = int(round((rebased - secs) * 1000))
    return f"At 00:{secs:02d}.{millis:03d}"


def rebase_ref_shot_blocks(shot_texts: list[str], start_s: float) -> str:
    """多参机械回退的镜头切片：重新编号 [Shot 1] 起、时间戳归零重基（issue #46）。

    每块去掉原 [Shot N] 标记；段内**首镜**再剥掉块首切点时间（官方规范：[Shot 1]
    无切点时间），后续镜的切点时间保留但重基为相对本段起点。块内其余 `At MM:SS.mmm`
    节拍同样整体重基——绝对片时进正文会把节拍排到片段长度之外（GEN003 坏片根因）。
    """
    out: list[str] = []
    for index, block in enumerate(shot_texts, 1):
        text = _SHOT_BLOCK.sub("", str(block), count=1).lstrip()
        if index == 1:
            text = _LEADING_AT_RE.sub("", text, count=1)
        text = _AT_TIME_RE.sub(lambda m: _rebase_at(m, start_s), text)
        out.append(f"[Shot {index}] {text.strip()}")
    return "\n".join(out)
