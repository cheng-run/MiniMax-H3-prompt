"""活文档里的提示词指针不许带行号（retro 2026-11-03）。

**为什么需要它**：``docs/`` 下用 ``prompts/xxx.md:66`` / ``xxx.md:69`` 这类行号指针
指向提示词真源。提示词文件一插行，这些指针就集体漂移且**一处都不报错**（#156 实测：
``frame_prompt_engineer.md`` 在 ``:18`` 后插两行，``:21`` 起整体 +1，人读文档按旧指针
读到的是别的内容）。小节标题是稳定锚点：改内容不改标题，指针仍然成立；标题真变了，
人读时当场能发现。

**文件名从真源目录动态收集**：只匹配 ``src/minimax_h3_prompt/prompts/*.md`` 与
``knowledge/*.md`` 的 stem。这样裸文件名（``frame_prompt_engineer.md:69``）与带任意
路径前缀（``prompts/frame_prompt_engineer.md:69``）都会被抓到——旧正则只认
``prompts|knowledge/`` 前缀，恰好在 ``docs/film-language-playbook.md`` 这类文档里漏光。

``docs/research/`` 是历史记录，按 ``AGENTS.md`` 的产物落点规则不改写，因此排除在外。
守卫自身的「会响」证明见 ``test_scan_recognises_bad_pointer``——永不为红的守卫等于没有。

**第二个面：指向本仓源码的行号（retro 2026-11-10）**。``CODING_STANDARDS.md`` 第六节
要求「代码与活文档里的指针一律不带行号」，但上面这条守卫只扫提示词 stem，`sessions.py:3-6`
这种指向本仓模块的写法一路绿到复审（2026-11-10 #224 实测）。下面按**本仓模块名**再扫一遍
活文档：模块名从 ``src/`` 现收，插件源码（``executor_core.py:851`` 这类）不归本仓编辑，
行号是合适合约，不判红。
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).parents[1]
DOCS_ROOT = ROOT / "docs"
SOURCE_DIRS = (
    ROOT / "src" / "minimax_h3_prompt" / "prompts",
    ROOT / "src" / "minimax_h3_prompt" / "knowledge",
)
RESEARCH_DIR = "research"

# 活文档：最常被改、又不算历史记录的那几份。`docs/adr/` 与 `docs/research/` 带日期，
# 行号是当时的证据，按产物落点规则不改写。
LIVE_DOCS = (ROOT / "AGENTS.md", ROOT / "CONTEXT.md")
LIVE_DOC_DIRS = (ROOT / "docs" / "agents",)
OWN_SOURCE_ROOT = ROOT / "src" / "minimax_h3_prompt"


def _pointer_pattern() -> re.Pattern[str]:
    """按真源文件名生成 ``<stem>.md:<行号>`` 的匹配式。"""
    stems = sorted(
        {path.stem for directory in SOURCE_DIRS for path in directory.glob("*.md")},
        key=len,
        reverse=True,
    )
    # 目录被挪走时宁可报错也不要退化成「永远不命中」——那等于守卫静默消失。
    if not stems:
        raise RuntimeError(f"没有找到提示词真源：{SOURCE_DIRS}")
    return re.compile(r"(?:" + "|".join(map(re.escape, stems)) + r")\.md:\d+")


def scan(root: Path) -> list[str]:
    """扫描 ``root/**/*.md``（跳过 ``root/research/``），返回命中的指针描述。"""
    pointer = _pointer_pattern()
    bad: list[str] = []
    for path in sorted(root.rglob("*.md")):
        relative = path.relative_to(root)
        if relative.parts and relative.parts[0] == RESEARCH_DIR:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for match in pointer.finditer(line):
                bad.append(f"{path}:{number} -> {match.group(0)}")
    return bad


def test_live_docs_use_section_anchors_not_line_numbers() -> None:
    """活文档里出现真源文件名 + 行号即判红（裸文件名与带路径前缀都算）。"""
    bad = scan(DOCS_ROOT)
    assert not bad, (
        "活文档里的提示词指针不许带行号，请改成小节标题锚点"
        "（`docs/research/` 除外）：\n  " + "\n  ".join(bad))


def test_scan_recognises_bad_pointer(tmp_path: Path) -> None:
    """反例自检：裸文件名、带前缀、``research/`` 排除三条都要被证明。"""
    (tmp_path / "design").mkdir()
    (tmp_path / "research").mkdir()
    (tmp_path / "design" / "bad.md").write_text(
        "见 `frame_prompt_engineer.md:69` 与 `knowledge/film-language-video.md:21`。\n",
        encoding="utf-8")
    # 历史记录同样含指针，但必须被排除——否则守卫会逼着改调研。
    (tmp_path / "research" / "history.md").write_text(
        "见 `frame_prompt_engineer.md:69`。\n", encoding="utf-8")

    bad = scan(tmp_path)
    assert len(bad) == 2, f"design 的两处都该被抓住、research 该被排除：{bad}"
    assert any("frame_prompt_engineer.md:69" in item for item in bad)
    assert any("film-language-video.md:21" in item for item in bad)
    assert all("history.md" not in item for item in bad)


# ---------------------------------------------------------------------------
# 指向本仓源码的行号（retro 2026-11-10）
# ---------------------------------------------------------------------------

def _own_module_stems(root: Path = OWN_SOURCE_ROOT) -> frozenset[str]:
    """本仓自己的模块名，从 ``src/`` 现收。

    目录被挪走时宁可报错也不要退化成「永远不命中」——那等于守卫静默消失。
    """
    stems = {path.stem for path in root.rglob("*.py")}
    if not stems:
        raise RuntimeError(f"没有找到本仓模块：{root}")
    return frozenset(stems)


def _own_pointer_pattern(stems: frozenset[str]) -> re.Pattern[str]:
    """``<本仓模块名>.py:<行号>`` 的匹配式。

    **只认模块名，不认路径**：插件源码（``executor_core.py:851``）不归本仓编辑，行号
    在那儿是合适合约。代价是同名文件分不开——本仓 ``director/pack.py`` 与插件
    ``pack.py`` 撞名，所以这条守卫只扫活文档；真要在活文档里指插件，写插件名 + 版本。
    """
    names = sorted(stems, key=len, reverse=True)
    return re.compile(r"(?:" + "|".join(map(re.escape, names)) + r")\.py:\d+")


def _live_docs() -> list[Path]:
    paths = [path for path in LIVE_DOCS if path.is_file()]
    missing = [str(path) for path in LIVE_DOCS if not path.is_file()]
    assert not missing, f"活文档不见了，守卫在空转：{missing}"
    for directory in LIVE_DOC_DIRS:
        paths.extend(sorted(directory.rglob("*.md")))
    return paths


def scan_own_module_pointers(
    paths: Iterable[Path], stems: frozenset[str] | None = None,
) -> list[str]:
    """在给定文件里找「本仓模块名 + 行号」的指针。"""
    pattern = _own_pointer_pattern(stems if stems is not None else _own_module_stems())
    bad: list[str] = []
    for path in paths:
        for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1):
            for match in pattern.finditer(line):
                bad.append(f"{path}:{number} -> {match.group(0)}")
    return bad


def test_live_docs_do_not_point_at_own_sources_by_line_number() -> None:
    """活文档指本仓模块时写 ``模块名.符号名``：行号会随实现漂且一处都不报错。"""
    bad = scan_own_module_pointers(_live_docs())
    assert not bad, (
        "活文档里指向本仓模块的指针不许带行号，请改成「模块名.符号名」"
        "（`docs/adr/` 与 `docs/research/` 是历史记录，不在此列）：\n  "
        + "\n  ".join(bad))


def test_own_module_scan_recognises_only_own_modules(tmp_path: Path) -> None:
    """反例自检：本仓模块名要咬住，插件模块名不许误伤（否则守卫会逼人改插件注释）。"""
    doc = tmp_path / "workbench.md"
    doc.write_text(
        "见 `segment_prompts.py:666`（本仓）与 `executor_core.py:851`（插件）。\n",
        encoding="utf-8")

    bad = scan_own_module_pointers([doc])

    assert len(bad) == 1, f"只该抓住本仓那一处：{bad}"
    assert "segment_prompts.py:666" in bad[0]
