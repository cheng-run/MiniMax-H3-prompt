#!/usr/bin/env python3
"""死规则检测：文档与记忆里引用的路径/记忆名，必须真的存在。

为什么：规范里大量是「按 X 写」「别碰 Y」这类指向别处东西的规则。X 和 Y 被删掉、
改名、或从一开始就没写对时，规则**不会报错**——它只是悄悄变成噪音，读者照它去找
一个不存在的地方。2026-11-27 手工扫了 114 个文件，查出 15 处：`ui/menu.py` 早已删除
（提交 7b78617）却仍被 6 处记忆引用、`tools/flicker_metric.py` 实际在 `scripts/`、
`CONTEXT.md` 引的 `lib/ref_images.py` 整个模块不存在。这些没有一条会被现有守卫抓到：
`test_doc_references_tracked.py` 只查 `docs/(research|design|handoffs)/` 一种形态。

判据只认「路径存在性」与「记忆名存在性」，不猜语义。刻意**不查代码符号**
（函数名/类名）：跨语言、跨写法，误报与漏报都比路径类高一个量级，收益不确定。

哪些引用不算：
- 带模板/通配的（`YYYY`、`NNNN`、`*`、`{`、`}`）；
- `output/` 开头的（运行期产物，gitignore，本来就不在仓里）；
- 已明标「已删」的说明性提及看不出来——这类若误报，把该文件加进 `_ALLOW` 并写清理由。
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).parents[1]

# 仓库根的路径引用。只认带目录的、有已知后缀的具体文件。
# 文件名允许中文——本仓 `docs/research/` 下大量是中文名，`test_doc_references_tracked.py`
# 已因此特意处理过；用纯 ASCII 字符类会让中文路径整个漏扫（2026-11-27 反例自检抓到）。
_PATH = re.compile(
    r"(?<![\w/])((?:docs|scripts|tests|src|prompts|contracts|frontend|tools)/"
    r"[^\s`'\"<>|()（）]+?\.(?:md|py|sh|ts|vue|json|ya?ml))"
)
# 记忆名可能是 ASCII 也可能含中文（`存储的目录名` 与 `wizard-new-step-breaks` 都出现过），
# 所以取「反引号之间的一切」，不假设字符集——2026-11-27 反例自检用中文名时，
# `[a-z0-9-]+` 当场漏匹配，守卫会静默放行。
_MEMORY_NAME = re.compile(r"auto memory\s*`([^`]+)`")
# 形如 [[slug]] 的记忆间链接。
_WIKILINK = re.compile(r"\[\[([^\]\s]+)\]\]")

_NON_CONCRETE = ("YYYY", "NNNN", "主题", "*", "{", "}", "...", "…", "NN", "xxx", "path/to")

# 只扫**活文档**。代码文件里的路径字符串多是运行期产物、临时夹具或示例
# （`test_not_a_file.py`、`prompts/segment-NN.md`、`src/.../desktop.py`），
# 把它们也纳入会让守卫满是噪音、最后被关掉。代码里的引用由别的守卫管
# （`test_doc_references_tracked.py` 管 docs 入库、`test_doc_pointer_shape.py` 管行号指针）。
_LIVE_DOCS = {
    "AGENTS.md",
    "CODING_STANDARDS.md",
    "CONTEXT.md",
    "CHANGELOG.md",
}
_LIVE_DOC_PREFIXES = ("docs/agents/",)
_TEXT_SUFFIXES = {".md"}

# 这个脚本自己写着反例路径，别把自己判成悬空引用。
_ALLOW = {"scripts/check_dangling_refs.py"}

# 明标「已删/已退役」的说明性引用是留痕，不是死规则。这些文件按整篇豁免：
# 它们已经降级成历史记录，扫出悬空路径是预期的。
_RETIRED_DOCS = {
    "memory/ui-menu-unreachable-in-production.md",
}

# 历史记录不扫：ADR 与调研/设计文档是「当时怎么想的」的存档，引用的模块后来删了
# 也是历史的一部分，改它等于篡改留痕（AGENTS.md「产物落点」与 CODING_STANDARDS 第六节
# 都把这两类定为历史记录）。活文档（根 AGENTS.md / CODING_STANDARDS.md / CONTEXT.md /
# docs/agents/）才需要跟着代码走。
_HISTORY_PREFIXES = ("docs/adr/", "docs/design/", "docs/research/", "docs/handoffs/")


def _tracked(repo: Path) -> list[str]:
    raw = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "-z"],
        capture_output=True, check=True).stdout
    return [item.decode("utf-8") for item in raw.split(b"\0") if item]


def _resolve(cited: str, tracked: set[str]) -> bool:
    """引用是否成立。

    三种写法在仓里都出现过：
    - 仓根直通（``docs/agents/x.md``）；
    - 前端包内——前端源码写作 ``src/...``，实际挂在 ``frontend/``；
    - 包内 prompts / tools——文档里写作 ``prompts/x.md``、``tools/x.py``，
      实际在 ``src/minimax_h3_prompt/`` 下（2026-11-27 实测这两类占了误报的大头）。
    """
    return (
        cited in tracked
        or f"frontend/{cited}" in tracked
        or f"src/minimax_h3_prompt/{cited}" in tracked
    )


def scan(
    repo: Path,
    tracked: set[str],
    memory_names: set[str],
    extra_files: dict[str, str] | None = None,
) -> list[str]:
    """返回 ``[引用方 -> 悬空目标]``；具体路径才算，模板/通配跳过。"""
    extra_files = extra_files or {}
    bad: list[str] = []
    for relative in sorted(tracked - _ALLOW):
        if relative.startswith(_HISTORY_PREFIXES):
            continue
        if relative not in _LIVE_DOCS and not relative.startswith(_LIVE_DOC_PREFIXES):
            continue
        if Path(relative).suffix.lower() not in _TEXT_SUFFIXES:
            continue
        try:
            text = (repo / relative).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        bad.extend(_scan_text(relative, text, tracked, memory_names))
    # 记忆目录在仓外，由调用方按 ``{文件名: 内容}`` 传进来。
    for name, text in sorted(extra_files.items()):
        if name in _RETIRED_DOCS:
            continue
        bad.extend(_scan_text(name, text, tracked, memory_names))
    return bad


def _scan_text(
    relative: str, text: str, tracked: set[str], memory_names: set[str]
) -> list[str]:
    bad: list[str] = []
    for match in _PATH.finditer(text):
        cited = match.group(1).rstrip(".,;:）)")
        if any(token in cited for token in _NON_CONCRETE):
            continue
        if cited.startswith("output/"):
            continue
        if not _resolve(cited, tracked):
            bad.append(f"{relative} -> 路径不存在: {cited}")
    for match in _MEMORY_NAME.finditer(text):
        name = match.group(1)
        if name not in memory_names:
            bad.append(f"{relative} -> 记忆不存在: auto memory `{name}`")
    for match in _WIKILINK.finditer(text):
        name = match.group(1)
        if name not in memory_names:
            bad.append(f"{relative} -> 记忆不存在: [[{name}]]")
    return bad


def _memory_dir() -> Path:
    return Path.home() / ".claude" / "projects" / "d-----python-MiniMax-H3-prompt" / "memory"


def _memory_files() -> tuple[set[str], dict[str, str]]:
    """返回（记忆名集合，记忆正文）。目录不存在时返回空——干净检出/别的机器上没有记忆。"""
    directory = _memory_dir()
    if not directory.is_dir():
        return set(), {}
    names: set[str] = set()
    texts: dict[str, str] = {}
    for path in sorted(directory.glob("*.md")):
        names.add(path.stem)
        try:
            texts[f"memory/{path.name}"] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
    return names, texts


def main(argv: list[str]) -> int:
    include_memory = "--memory" in argv[1:]
    tracked = set(_tracked(REPO))
    memory_names, memory_texts = _memory_files()
    extra = memory_texts if include_memory else None
    bad = scan(REPO, tracked, memory_names, extra)
    scope = "活文档" + ("＋记忆" if include_memory else "")
    if bad:
        print(f"死规则检测（{scope}）：这些引用指向不存在的东西：")
        for line in bad:
            print(f"  - {line}")
        return 1
    print(f"死规则检测（{scope}）：OK（0 处悬空引用）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(__import__("sys").argv))
