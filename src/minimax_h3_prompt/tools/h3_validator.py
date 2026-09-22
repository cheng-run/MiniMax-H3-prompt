"""H3 提示词规则校验器。

以官方规范（references/base-en.txt、ref-en.txt）为唯一依据做确定性检查。
供质检节点 / 提示词工程师 / agent 工具调用。

严重级别：
- error   阻塞性问题，必须修（格式/标签/时间戳/段落顺序）
- warning 建议性问题（词数、句子数、编号跳跃等）
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_SHOT_RE = re.compile(r"\[Shot\s+(\d+)\]")
_TIME_RE = re.compile(r"At\s+(\d{2}):(\d{2})\.(\d{3})")
# 行首裸时间戳镜头：At MM:SS.mmm 开头且本行无 [Shot N]——长视频拆分器会漏掉
_BARE_TIMESTAMP_LINE = re.compile(r"^\s*At\s+\d{2}:\d{2}\.\d{3}")
_DIALOG_OPEN_RE = re.compile(r"<d>")
_DIALOG_CLOSE_RE = re.compile(r"</d>")
_LANG_TAG_RE = re.compile(r"<d>\s*\[([A-Za-z\-]{2,})\]")
_SPEAKER_RE = re.compile(r"\(S(\d+)\)")
_PICTURE_RE = re.compile(r"<Picture\s+(\d+)>")
_SUBJECT_RE = re.compile(r"<Subject\s+(\d+)>")
_AUDIO_RE = re.compile(r"<Audio\s+(\d+)>")
_VIDEO_RE = re.compile(r"<Video\s+(\d+)>")
_WORD_RE = re.compile(r"[A-Za-z]+")
_SENTENCE_RE = re.compile(r"[.!?]+")

# 官方格式迁移（2026-09-22 spec / issue #2）：自创结构一律禁止（行首标记式）
_BANNED_SECTION_MARKERS = ("GLOBAL_LOCK", "BRIDGE_FROM", "END_HOOK")
# 防波纹咒语（自创，官方一致性靠 Picture 锚定句）
_RIPPLE_SPELL_RE = re.compile(r"无波纹|边缘抖动")
# 首行时长句（官方由帧变体指令行承载时长）
_DURATION_SENTENCE_RE = re.compile(r"^This is a \d+(?:\.\d+)?-second continuous shot\.")
# 关键节拍距段尾最小余量（spec 决策 Q1：出场动作需要展开空间）
_END_MARGIN_S = 1.0

REF_SECTIONS = [
    "subject_definitions",
    "summary",
    "retention_analysis",
    "detailed_description",
    "overall_soundscape",
    "non_diegetic_music",
]
BASE_SECTIONS = [
    "integrated_multimodal_description",
    "overall_soundscape",
    "non_diegetic_music",
]
RETENTION_MARKERS = (
    "fully_preserved",
    "partially_preserved",
    "attribute_transfer",
    "weak_reference",
    "fully_copy",
    "partially_copy",
    "reference",
)

# 帧变体首行对齐指令：官方模板逐字符固定（base-en.txt 2.1）
ALIGN_TEMPLATES = {
    "I2VA": "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.",
    "FL2VA": "How the reference pictures align with the target video — Picture 1 (from Shot 1) aligns with the 0.00-second mark of the target video; Picture 2 (from Shot N) aligns with the S.SS-second mark of the target video.",
    "L2VA": "How the reference pictures align with the target video — <Picture 1> (from [Shot N]) aligns with the S.SS-second mark of the target video.",
}
_I2VA_INSTRUCTION_RE = re.compile(
    r"^For the target video, at 0\.00 seconds into the target video, "
    r"<Picture 1> \(from \[Shot (\d+)\]\) is fully referenced\.$"
)
_FL2VA_INSTRUCTION_RE = re.compile(
    r"^How the reference pictures align with the target video — "
    r"Picture 1 \(from Shot (\d+)\) aligns with the 0\.00-second mark of the target video; "
    r"Picture 2 \(from Shot (\d+)\) aligns with the (\d+)\.(\d{2})-second mark of the target video\.$"
)
_L2VA_INSTRUCTION_RE = re.compile(
    r"^How the reference pictures align with the target video — "
    r"<Picture 1> \(from \[Shot (\d+)\]\) aligns with the (\d+)\.(\d{2})-second mark of the target video\.$"
)


def _check_align_instruction(text: str, first_line: str, body: str, variant: str,
                             duration: float | None, issues: list[ValidationIssue]) -> None:
    """帧变体首行指令：模板逐字符一致、S.SS=时长两位小数、N=最终镜头、后跟一个空行。"""
    stripped = first_line.strip()
    matched = False
    if variant == "I2VA":
        m = _I2VA_INSTRUCTION_RE.match(stripped)
        if m:
            matched = True
            if int(m.group(1)) != 1:
                issues.append(ValidationIssue("error", "ALIGN_FIRST_SHOT_MISMATCH",
                                              f"I2VA 首行应为 (from [Shot 1])，实际 (from [Shot {m.group(1)}])"))
    elif variant == "FL2VA":
        m = _FL2VA_INSTRUCTION_RE.match(stripped)
        if m:
            matched = True
            first_shot, last_shot = int(m.group(1)), int(m.group(2))
            seconds = int(m.group(3)) + int(m.group(4)) / 100.0
            if first_shot != 1:
                issues.append(ValidationIssue("error", "ALIGN_FIRST_SHOT_MISMATCH",
                                              f"FL2VA 首帧应 from Shot 1，实际 Shot {first_shot}"))
            shot_nums = [int(n) for n in re.findall(r"\[Shot\s+(\d+)\]", body)]
            if shot_nums and last_shot != max(shot_nums):
                issues.append(ValidationIssue("error", "ALIGN_LAST_SHOT_MISMATCH",
                                              f"FL2VA 尾帧应对齐最终镜头 [Shot {max(shot_nums)}]，实际写 Shot {last_shot}（见 base-en.txt 2.1/3.2）"))
            if duration is not None and abs(seconds - duration) > 0.011:
                issues.append(ValidationIssue("error", "ALIGN_TIME_MISMATCH",
                                              f"FL2VA 尾帧对齐秒数应为时长两位小数 {duration:.2f}，实际 {seconds:.2f}"))
            if len(set(shot_nums)) > 1:
                issues.append(ValidationIssue("warning", "FL2VA_MULTI_SHOT",
                                              "官方规范建议 FL2VA 用单镜头连续插值；多镜头仅限用户明确指定时（base-en.txt 3.2）"))
    elif variant == "L2VA":
        m = _L2VA_INSTRUCTION_RE.match(stripped)
        if m:
            matched = True
            last_shot = int(m.group(1))
            seconds = int(m.group(2)) + int(m.group(3)) / 100.0
            shot_nums = [int(n) for n in re.findall(r"\[Shot\s+(\d+)\]", body)]
            if shot_nums and last_shot != max(shot_nums):
                issues.append(ValidationIssue("error", "ALIGN_LAST_SHOT_MISMATCH",
                                              f"L2VA 尾帧应对齐最终镜头 [Shot {max(shot_nums)}]，实际写 Shot {last_shot}"))
            if duration is not None and abs(seconds - duration) > 0.011:
                issues.append(ValidationIssue("error", "ALIGN_TIME_MISMATCH",
                                              f"L2VA 尾帧对齐秒数应为时长两位小数 {duration:.2f}，实际 {seconds:.2f}"))
    if not matched:
        issues.append(ValidationIssue("error", "ALIGN_INSTRUCTION_FORMAT",
                                      f"{variant} 首行对齐指令与官方模板不一致，必须逐字符按：{ALIGN_TEMPLATES[variant]}"))

    # 指令是第一行，之后必须有一个空行再接核心字段（base-en.txt 2.1）
    lines = text.splitlines()
    first_idx = next((i for i, l in enumerate(lines) if l.strip()), 0)
    if first_idx + 1 < len(lines) and lines[first_idx + 1].strip():
        issues.append(ValidationIssue("warning", "ALIGN_BLANK_LINE_MISSING",
                                      "对齐指令后应空一行再接核心字段"))


@dataclass
class ValidationIssue:
    severity: str  # "error" | "warning"
    code: str
    message: str


def only_errors(issues: list[ValidationIssue]) -> list[ValidationIssue]:
    """只取 error 级（warning 是建议性的，不阻塞交付）。"""
    return [i for i in issues if i.severity == "error"]


def _time_to_seconds(tt: re.Match[str]) -> float:
    minutes = int(tt.group(1))
    seconds = int(tt.group(2))
    millis = int(tt.group(3))
    return minutes * 60 + seconds + millis / 1000.0


def timestamps_seconds(text: str) -> list[float]:
    """正文里所有 `At MM:SS.mmm` 的秒数，按出现顺序。"""
    return [_time_to_seconds(m) for m in _TIME_RE.finditer(text)]


def _find_sections(text: str, headers: list[str]) -> dict[str, tuple[str, int]]:
    """定位各段头，返回 {header: (内容, 起始行号)}。内容为段头到下一段头之间。"""
    positions: list[tuple[int, int, str]] = []
    for h in headers:
        m = re.compile(rf"^{re.escape(h)}\s*[:：]", re.MULTILINE).search(text)
        if m:
            positions.append((m.start(), m.end(), h))
    positions.sort()
    result: dict[str, tuple[str, int]] = {}
    for i, (start, end, h) in enumerate(positions):
        next_start = positions[i + 1][0] if i + 1 < len(positions) else len(text)
        result[h] = (text[end:next_start].strip(), text.count("\n", 0, start))
    return result


def _check_banned_structures(text: str, issues: list[ValidationIssue]) -> None:
    """官方格式迁移：GLOBAL_LOCK/BRIDGE_FROM/END_HOOK/防波纹咒语/首行时长句出现即报 error。

    结构标记按行首匹配（字段式自创结构）；防波纹咒语按行内关键词扫全文——
    它历史上被附加在任意 Shot 块尾，不固定字段位置。
    """
    for marker in _BANNED_SECTION_MARKERS:
        if re.search(rf"^{marker}\s*[:：]", text, re.MULTILINE):
            issues.append(ValidationIssue(
                "error", f"{marker}_BANNED",
                f"自创结构 {marker}: 不存在于官方 base-en.txt 规范，必须删除"
                "（实体外观写进首次出场的 Shot；段首靠 Picture 1 锚定句；段尾自然收句）"))
    ripple_lines = [i for i, line in enumerate(text.splitlines(), 1)
                    if _RIPPLE_SPELL_RE.search(line)]
    if ripple_lines:
        issues.append(ValidationIssue(
            "error", "RIPPLE_SPELL_BANNED",
            f"行 {', '.join(map(str, ripple_lines))}：防波纹咒语（无波纹/边缘抖动）是自创结构，"
            "官方一致性机制是 Picture 锚定句，必须删除"))
    first_nonempty = next((l for l in text.splitlines() if l.strip()), "")
    if _DURATION_SENTENCE_RE.match(first_nonempty.strip()):
        issues.append(ValidationIssue(
            "error", "DURATION_SENTENCE_BANNED",
            "首行时长句「This is a N-second continuous shot.」是自创结构；"
            "帧变体时长由对齐指令行（S.SS-second mark）承载，T2VA 不写时长句"))


def _check_beat_and_shot_count(text: str, duration: float | None,
                               issues: list[ValidationIssue]) -> None:
    """官方格式纪律：最后节拍距段尾 ≥1s（error）；段内默认单 Shot（warning）。"""
    # 最后时间戳 ≤ 时长-1s（确定性代理：无法识别「关键出场节拍」，以最晚时间戳为准）
    if duration is not None:
        timestamps = timestamps_seconds(text)
        if timestamps:
            last = max(timestamps)
            if last > duration - _END_MARGIN_S:
                issues.append(ValidationIssue(
                    "error", "LAST_TIMESTAMP_TOO_CLOSE_TO_END",
                    f"最后节拍 {last:.3f}s 距段尾 {duration}s 不足 {_END_MARGIN_S:g}s——"
                    "关键出场节拍需要展开空间（spec 决策 Q1：问题①猫提前出现的对策）"))
    # 段内默认单 Shot（Q7：切镜是显式例外，warning 提醒不禁止）
    shot_nums = {int(m.group(1)) for m in _SHOT_RE.finditer(text)}
    if len(shot_nums) > 1:
        issues.append(ValidationIssue(
            "warning", "MULTI_SHOT_SEGMENT",
            f"段内含 {len(shot_nums)} 个 Shot——段内切镜是显式例外（仅景别跳变等确有必要时），"
            "单 Shot 让 I2VA 只锚定一个构图"))


def _check_shots(text: str, duration: float | None, issues: list[ValidationIssue]) -> None:
    """[Shot N] 序号连续、首镜无时间戳、切点单调递增且在时长内。

    额外检出"裸时间戳镜头"：行首为 At MM:SS.mmm 但同行没有 [Shot N] 标记——
    说明模型分了多个镜头却漏写标记，分段执行会无法拆分，属 error 级。
    """
    # 裸时间戳镜头检测：行首 At MM:SS.mmm 且行内无 [Shot N]
    bare_ts_lines = [
        i for i, line in enumerate(text.splitlines(), 1)
        if _BARE_TIMESTAMP_LINE.match(line) and not _SHOT_RE.search(line)
    ]
    if len(bare_ts_lines) >= 1 and duration is not None and duration > 10:
        issues.append(ValidationIssue(
            "error", "BARE_TIMESTAMP_SHOT",
            f"行 {', '.join(map(str, bare_ts_lines))}：检测到独立时间戳镜头但缺 [Shot N] 标记。"
            "长视频需要逐镜头拆分执行，每个镜头必须以 [Shot N] 开头（如 '[Shot 2] At 00:05.000, ...'）。"))

    shots: list[tuple[int, float | None, int]] = []  # (N, time_sec, line_no)
    for i, line in enumerate(text.splitlines(), 1):
        for sm in _SHOT_RE.finditer(line):
            n = int(sm.group(1))
            # 时间只取「本镜标记 → 本行下一个 [Shot M]」之间的片段，避免把下一镜的时间算给本镜
            next_shot = _SHOT_RE.search(line, sm.end())
            seg_end = next_shot.start() if next_shot else len(line)
            tm = _TIME_RE.search(line[sm.end():seg_end])
            time_sec = _time_to_seconds(tm) if tm else None
            # 官方格式：[Shot 1] 句子内部允许 At 00:XX.XXX 节拍（写在句中而非紧跟标记）；
            # 只有紧跟标记的切点式时间戳（官方切镜写法，首镜无切点）才报错
            if n == 1 and tm and line[sm.end():].lstrip().startswith("At"):
                issues.append(ValidationIssue("error", "FIRST_SHOT_TIMESTAMP",
                                              f"行 {i}：[Shot 1] 是首镜，不应带切点时间戳（官方规范）"))
            shots.append((n, time_sec, i))

    if not shots:
        issues.append(ValidationIssue("error", "NO_SHOT", "找不到任何 [Shot N] 标记"))
        return

    seen: set[int] = set()
    expected = 1
    for n, _t, ln in shots:
        if n in seen:
            issues.append(ValidationIssue("error", "SHOT_REPEAT", f"行 {ln}：[Shot {n}] 重复出现"))
        seen.add(n)
        if n > expected:
            issues.append(ValidationIssue("warning", "SHOT_GAP",
                                          f"行 {ln}：[Shot {n}] 跳过了 [Shot {expected}]"))
        expected = max(expected, n + 1)

    prev_time: float | None = None
    for n, time_sec, ln in shots:
        if n == 1:
            continue
        if time_sec is None:
            issues.append(ValidationIssue("error", "SHOT_NO_TIMESTAMP",
                                          f"行 {ln}：[Shot {n}] 缺少切点时间（应写 At MM:SS.mmm）"))
            continue
        if prev_time is not None and time_sec <= prev_time:
            issues.append(ValidationIssue("error", "TIMESTAMP_ORDER",
                                          f"行 {ln}：[Shot {n}] 时间 {time_sec:.3f}s 未严格递增"))
        if duration is not None and time_sec >= duration:
            issues.append(ValidationIssue("error", "TIMESTAMP_OVER_DURATION",
                                          f"行 {ln}：[Shot {n}] 时间 {time_sec:.3f}s 超出视频时长 {duration}s"))
        prev_time = time_sec


def _check_dialogues(text: str, issues: list[ValidationIssue]) -> None:
    opens = len(_DIALOG_OPEN_RE.findall(text))
    closes = len(_DIALOG_CLOSE_RE.findall(text))
    if opens != closes:
        issues.append(ValidationIssue("error", "DIALOG_UNBALANCED",
                                      f"<d> 开/闭不平衡（open={opens}, close={closes}）"))
    for i, line in enumerate(text.splitlines(), 1):
        if "<d>" in line and "</d>" in line and _LANG_TAG_RE.search(line) is None:
            issues.append(ValidationIssue("warning", "DIALOG_NO_LANGTAG",
                                          f"行 {i}：<d> 内缺少 [语言] 标签（应为 <d>[English] …</d>）"))


def _check_speakers(text: str, issues: list[ValidationIssue]) -> None:
    """(Sx) 首次出现顺序应从 1 连续。"""
    seen_order: list[int] = []
    for m in _SPEAKER_RE.finditer(text):
        num = int(m.group(1))
        if num not in seen_order:
            seen_order.append(num)
    for idx, num in enumerate(seen_order, 1):
        if num != idx:
            issues.append(ValidationIssue("warning", "SPEAKER_ORDER",
                                          f"(S{num}) 首次出现时 (S{idx}) 还未出现，编号建议按出场顺序从 1 连续分配"))


def _check_label_continuity(text: str, issues: list[ValidationIssue], prefix: str) -> None:
    nums = sorted({int(m.group(1)) for m in re.compile(rf"<{prefix}\s+(\d+)>").finditer(text)})
    if not nums:
        return
    missing = [n for n in range(1, nums[-1] + 1) if n not in nums]
    if missing:
        issues.append(ValidationIssue("error", f"LABEL_GAP_{prefix.upper()}",
                                      f"<{prefix} N> 编号有缺口，缺少 <{prefix} {'>, <'.join(map(str, missing))}>（应从 1 连续）"))


def _check_pictures_used(text: str, ref_meta: list[tuple[int, str, str]] | None,
                         issues: list[ValidationIssue]) -> None:
    """ref 模式：定义过的 <Picture N> 应在正文中被引用（反之亦然）。"""
    if not ref_meta:
        return
    defined = {p for p, _, _ in ref_meta}
    used = {int(m.group(1)) for m in _PICTURE_RE.finditer(text)}
    unused = defined - used
    if unused:
        issues.append(ValidationIssue("error", "PICTURE_UNUSED",
                                      f"参考图未在提示词中被引用：<Picture {'>, <Picture '.join(map(str, sorted(unused)))}>"))
    undefined = used - defined
    if undefined:
        issues.append(ValidationIssue("error", "PICTURE_UNDEFINED",
                                      f"提示词引用了未定义的 <Picture {'>, <Picture '.join(map(str, sorted(undefined)))}>"))


def validate_base(text: str, duration: float | None = None, variant: str = "T2VA",
                  ref_meta: list[tuple[int, str, str]] | None = None) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    sections = _find_sections(text, BASE_SECTIONS)
    body, _ = sections.get("integrated_multimodal_description", ("", 0))
    _check_banned_structures(text, issues)
    # 镜头/对白/说话人只在描述正文里检查（其他段落里的 [Shot N] 是引用，不是镜头标记）
    _check_shots(body, duration, issues)
    _check_dialogues(body, issues)
    _check_speakers(body, issues)
    _check_beat_and_shot_count(body, duration, issues)
    present_headers = list(sections.keys())
    if len(present_headers) != len(BASE_SECTIONS):
        missing = [h for h in BASE_SECTIONS if h not in sections]
        issues.append(ValidationIssue("error", "BASE_SECTION_MISSING", f"缺少段落：{', '.join(missing)}"))
    else:
        order = [h for h, _ in sorted(sections.items(), key=lambda kv: kv[1][1])]
        if order != BASE_SECTIONS:
            issues.append(ValidationIssue("error", "BASE_SECTION_ORDER",
                                          f"段落顺序错误：应为 {' → '.join(BASE_SECTIONS)}，实际 {' → '.join(order)}"))

    first_nonempty = next((l for l in text.splitlines() if l.strip()), "")
    if variant in ("I2VA", "FL2VA", "L2VA"):
        if "How the reference pictures align with the target video" not in first_nonempty \
           and "For the target video" not in first_nonempty:
            issues.append(ValidationIssue("error", "MISSING_ALIGN_INSTRUCTION",
                                          f"{variant} 模式首行必须有图片对齐指令（见 base-en.txt 2.1）"))
        _check_align_instruction(text, first_nonempty, body, variant, duration, issues)

    soundscape, _ = sections.get("overall_soundscape", ("", 0))
    music, _ = sections.get("non_diegetic_music", ("", 0))
    if soundscape and soundscape != "N/A":
        n = len([s for s in _SENTENCE_RE.split(soundscape) if s.strip()])
        if n > 4:
            issues.append(ValidationIssue("warning", "SOUNDSCAPE_TOO_LONG",
                                          f"overall_soundscape 建议 1–4 句，当前约 {n} 句"))
    if music and music != "N/A":
        n = len([s for s in _SENTENCE_RE.split(music) if s.strip()])
        if n > 3:
            issues.append(ValidationIssue("warning", "MUSIC_TOO_LONG",
                                          f"non_diegetic_music 建议 1–3 句，当前约 {n} 句"))
    return issues


def validate_ref(text: str, duration: float | None = None,
                 ref_meta: list[tuple[int, str, str]] | None = None) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    sections = _find_sections(text, REF_SECTIONS)
    body, _ = sections.get("detailed_description", ("", 0))
    _check_shots(body, duration, issues)
    _check_dialogues(body, issues)
    _check_speakers(body, issues)
    _check_label_continuity(text, issues, "Subject")
    _check_label_continuity(text, issues, "Picture")
    _check_label_continuity(text, issues, "Audio")
    _check_label_continuity(text, issues, "Video")
    _check_pictures_used(text, ref_meta, issues)
    present_headers = list(sections.keys())
    if len(present_headers) != len(REF_SECTIONS):
        missing = [h for h in REF_SECTIONS if h not in sections]
        issues.append(ValidationIssue("error", "REF_SECTION_MISSING", f"缺少段落：{', '.join(missing)}"))
    else:
        order = [h for h, _ in sorted(sections.items(), key=lambda kv: kv[1][1])]
        if order != REF_SECTIONS:
            issues.append(ValidationIssue("error", "REF_SECTION_ORDER",
                                          f"段落顺序错误：应为 {' → '.join(REF_SECTIONS)}，实际 {' → '.join(order)}"))

    summary, _ = sections.get("summary", ("", 0))
    if summary and not re.match(r"^\s*\[[a-z\s+]*\]", summary):
        issues.append(ValidationIssue("error", "SUMMARY_NO_TASKTYPE",
                                      "summary 必须以 [任务类型] 前缀开头（如 [reference generation]）"))

    retention, _ = sections.get("retention_analysis", ("", 0))
    if retention and not any(mk in retention for mk in RETENTION_MARKERS):
        issues.append(ValidationIssue("warning", "RETENTION_NO_MARKER",
                                      "retention_analysis 中没找到关系标记（fully_preserved 等，见 ref-en.txt 4.1）"))

    detailed, _ = sections.get("detailed_description", ("", 0))
    if detailed:
        n_words = len(_WORD_RE.findall(detailed))
        if n_words < 350:
            issues.append(ValidationIssue("warning", "DETAILED_TOO_SHORT",
                                          f"detailed_description 约 {n_words} 词，生成任务建议 350–500 词"))
        elif n_words > 500:
            issues.append(ValidationIssue("warning", "DETAILED_TOO_LONG",
                                          f"detailed_description 约 {n_words} 词，生成任务建议 350–500 词"))
    return issues


def validate_prompt(text: str, mode: str, duration: float | None = None, variant: str = "T2VA",
                    ref_meta: list[tuple[int, str, str]] | None = None) -> list[ValidationIssue]:
    if mode == "ref":
        return validate_ref(text, duration, ref_meta)
    return validate_base(text, duration, variant, ref_meta)


def format_issues(issues: list[ValidationIssue]) -> str:
    if not issues:
        return "通过：未发现格式问题。"
    lines = [f"[{i.severity.upper()}] {i.code}: {i.message}" for i in issues]
    errors = sum(1 for i in issues if i.severity == "error")
    return f"{len(lines)} 个问题（{errors} 个错误）：\n" + "\n".join(lines)
