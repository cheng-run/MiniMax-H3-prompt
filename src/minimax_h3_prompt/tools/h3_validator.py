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


@dataclass
class ValidationIssue:
    severity: str  # "error" | "warning"
    code: str
    message: str


def _time_to_seconds(tt: re.Match[str]) -> float:
    minutes = int(tt.group(1))
    seconds = int(tt.group(2))
    millis = int(tt.group(3))
    return minutes * 60 + seconds + millis / 1000.0


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


def _check_shots(text: str, duration: float | None, issues: list[ValidationIssue]) -> None:
    """[Shot N] 序号连续、首镜无时间戳、切点单调递增且在时长内。"""
    shots: list[tuple[int, float | None, int]] = []  # (N, time_sec, line_no)
    for i, line in enumerate(text.splitlines(), 1):
        for sm in _SHOT_RE.finditer(line):
            n = int(sm.group(1))
            # 时间只取「本镜标记 → 本行下一个 [Shot M]」之间的片段，避免把下一镜的时间算给本镜
            next_shot = _SHOT_RE.search(line, sm.end())
            seg_end = next_shot.start() if next_shot else len(line)
            tm = _TIME_RE.search(line[sm.end():seg_end])
            time_sec = _time_to_seconds(tm) if tm else None
            if n == 1 and tm:
                issues.append(ValidationIssue("error", "FIRST_SHOT_TIMESTAMP",
                                              f"行 {i}：[Shot 1] 是首镜，不应带时间戳（官方规范）"))
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
    # 镜头/对白/说话人只在描述正文里检查（其他段落里的 [Shot N] 是引用，不是镜头标记）
    _check_shots(body, duration, issues)
    _check_dialogues(body, issues)
    _check_speakers(body, issues)
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
