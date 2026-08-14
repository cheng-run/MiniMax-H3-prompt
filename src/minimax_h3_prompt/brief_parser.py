"""brief 解析器：把用户的创意/参考资产/草稿 markdown 解析成结构化输入。

brief 文件约定（示例见 examples/）：
```
模式: ref              # ref | base
变体: T2VA             # base 模式：T2VA/I2VA/FL2VA/L2VA
时长: 5                # 秒
风格: Cinematic 真人实拍
语言: Chinese

## 剧情
一段话描述创意……

## 角色与参考图
- Picture 1: 神师 — 白发老妪，玄色长袍
- Picture 2: 韩立 — 青衫青年

## 草稿提示词
（polish 模式可选）已有提示词/剧本……
```
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_HEADING_RE = re.compile(r"^#{1,6}\s+(.*)$")
_KV_RE = re.compile(r"^([^#:#：]+)[:：]\s*(.*)$")
# refs 行：`Picture 1: 名称` 或 `- Picture 1: 名称 — 描述`（描述可省），末尾可带 `(路径)`
_REF_RE = re.compile(r"^(?:[-*]\s*)?Picture\s+(\d+)\s*[:：]\s*(.*)$", re.IGNORECASE)
_PATH_RE = re.compile(r"\(([^)]*)\)\s*$")  # 行尾 (路径)，路径含引号/反斜杠也能接
_IMG_EXT_RE = re.compile(r"\.(?:png|jpe?g|webp|bmp|heic)\s*[\"']*\s*$", re.IGNORECASE)


def _normalize_path(p: str) -> str:
    """路径归一化：去掉引号、反斜杠→正斜杠、Windows 盘符→/mnt/<盘>/。"""
    p = p.strip().strip('"\'')
    if not p:
        return ""
    p = p.replace("\\", "/")
    m = re.match(r"^([A-Za-z]):/", p)
    if m:  # D:\... → /mnt/d/...
        p = "/mnt/" + m.group(1).lower() + "/" + p[m.end():]
    return p


@dataclass
class RefItem:
    picture: int  # <Picture N> 编号，从 1 起（官方规范）
    name: str
    description: str
    path: str = ""  # 参考图文件路径（可选；有则支持视觉审核）


@dataclass
class Brief:
    mode: str = "base"  # ref | base
    variant: str = "T2VA"  # base: T2VA/I2VA/FL2VA/L2VA
    duration: float = 5.0
    style: str = "Cinematic"
    language: str = "Chinese"
    plot: str = ""
    refs: list[RefItem] = field(default_factory=list)
    draft: str = ""
    raw: str = ""

    @property
    def is_ref(self) -> bool:
        return self.mode == "ref"


# 帧锚点变体：参考图是"首帧/尾帧"，不是角色/场景
FRAME_VARIANTS = ("I2VA", "FL2VA", "L2VA")


def brief_uses_refs(brief: Brief) -> bool:
    """该 brief 是否需要参考图：ref 模式（角色/场景）或帧变体（首/尾帧锚点）。"""
    return brief.mode == "ref" or brief.variant in FRAME_VARIANTS


def min_refs(brief: Brief) -> int:
    """该输入方式最少需要的参考图张数。"""
    if brief.mode == "ref":
        return 1
    if brief.variant == "FL2VA":  # 首尾帧：首帧 + 尾帧
        return 2
    if brief.variant in ("I2VA", "L2VA"):  # 仅首帧 / 仅尾帧
        return 1
    return 0  # T2VA 纯文字


def parse_brief(path: str | Path) -> Brief:
    text = Path(path).read_text(encoding="utf-8")
    return parse_brief_text(text)


def parse_brief_text(text: str) -> Brief:
    brief = Brief(raw=text)
    current_section = "meta"  # 首个标题前是配置区
    section_buf: dict[str, list[str]] = {"meta": []}

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            hashes = len(stripped) - len(stripped.lstrip("#"))
            if hashes >= 2:  # ## 及以上才是小节；一级标题只是文件标题，不切换 section
                current_section = stripped.lstrip("#").strip()
                section_buf.setdefault(current_section, [])
            continue
        section_buf.setdefault(current_section, []).append(stripped)

    # --- 配置区（meta / 无标题的键值对）---
    # 优先级：输入（首帧/尾帧/首尾帧/多参考） > 模型（fl2va/ref2va） > 模式/变体
    input_seen = model_seen = False
    for line in section_buf["meta"]:
        kv = _KV_RE.match(line.strip())
        if not kv:
            continue
        key, value = kv.group(1).strip(), kv.group(2).strip()
        if not value:
            continue
        value = re.split(r"\s+#", value, maxsplit=1)[0].strip()  # 去掉行内注释（空格+#）
        k = key.lower()
        if k in ("输入", "input"):
            input_seen = True
            m = value.strip().lower()
            if m in ("多参考", "ref2va"):
                brief.mode = "ref"
            elif m in ("首帧", "i2va"):
                brief.mode = "base"
                brief.variant = "I2VA"
            elif m in ("尾帧", "l2va"):
                brief.mode = "base"
                brief.variant = "L2VA"
            elif m in ("首尾帧", "fl2va"):
                brief.mode = "base"
                brief.variant = "FL2VA"
        elif k in ("模型", "model") and not input_seen:
            model_seen = True
            m = value.strip().lower()
            if m == "ref2va":
                brief.mode = "ref"
            elif m in ("t2va", "i2va", "fl2va", "l2va"):
                brief.mode = "base"
                brief.variant = m.upper()
        elif k in ("模式", "mode") and not (input_seen or model_seen):
            brief.mode = value.lower()
        elif k in ("变体", "variant") and not (input_seen or model_seen):
            brief.variant = value.strip().upper()
        elif k in ("时长", "duration"):
            m = re.search(r"\d+(?:\.\d+)?", value)
            if m:
                brief.duration = float(m.group())
        elif k in ("风格", "style"):
            brief.style = value
        elif k in ("语言", "language"):
            brief.language = value

    # --- 剧情 ---
    for sec, buf in section_buf.items():
        if "剧情" in sec or "创意" in sec:
            brief.plot = "\n".join(buf).strip()
            break

    # --- 角色与参考图 ---
    for sec, buf in section_buf.items():
        if "参考图" in sec or "角色" in sec:
            for line in buf:
                rm = _REF_RE.match(line.strip())
                if not rm:
                    continue
                picture = int(rm.group(1))
                rest = rm.group(2).strip()
                path = ""
                pm = _PATH_RE.search(rest)
                if pm and _IMG_EXT_RE.search(pm.group(1)):  # 行尾 (路径) 且以图片扩展名结尾
                    path = _normalize_path(pm.group(1))
                    rest = rest[: pm.start()].strip()
                name, desc = rest, ""
                # 分隔符：优先中文破折号 ——，其次空格包围的 - / –（避免 Z-Image 这类连字符误拆）
                sm = re.search(r"(?:—|－)", rest)
                if not sm:
                    sm = re.search(r"\s+[-–]\s+", rest)
                if sm:
                    name = rest[: sm.start()].strip()
                    desc = rest[sm.end():].strip()
                brief.refs.append(RefItem(picture=picture, name=name, description=desc, path=path))
            break

    # --- 草稿（polish 模式）---
    for sec, buf in section_buf.items():
        if "草稿" in sec or "草稿提示词" in sec or "draft" in sec.lower():
            brief.draft = "\n".join(buf).strip()
            break

    # 若没有任何剧情，把整个文件作为剧情（兜底）
    if not brief.plot and brief.mode == "base":
        brief.plot = text.strip()

    return brief
