"""活文档里的提示词指针不许带行号（retro 2026-11-03）。

**为什么需要它**：``docs/`` 下用 ``prompts/xxx.md:66`` / ``knowledge/yyy.md:21-52``
这类行号指针指向提示词真源。提示词文件一插行，这些指针就集体漂移且**一处都不报错**
（#156 实测：``frame_prompt_engineer.md`` 在 ``:18`` 后插两行，``:21`` 起整体 +1，
人读文档按旧指针读到的是别的内容）。小节标题是稳定锚点：改内容不改标题，指针仍然
成立；标题真变了，人读时当场能发现。

``docs/research/`` 是历史记录，按 ``AGENTS.md`` 的产物落点规则不改写，因此排除在外。
守卫自身的「会响」证明见 ``test_scan_recognises_bad_pointer``——永不为红的守卫等于没有。
"""
from __future__ import annotations

import re
from pathlib import Path

DOCS_ROOT = Path(__file__).parents[1] / "docs"
RESEARCH_DIR = "research"

# 只认带目录前缀的指针：裸 ``:66`` 在本仓是 ``references/base-en.txt`` 的惯例写法，
# 不在本守卫范围内（那份是官方格式原件，不用小节锚点）。``\d+`` 而不是 ``\d``：
# 命中判据相同，但报错时能显示完整行号（``:12`` 不是 ``:1``）。
POINTER = re.compile(r"(?:prompts|knowledge)/[A-Za-z0-9_.-]+\.md:\d+")


def scan(root: Path) -> list[str]:
    """扫描 ``root/**/*.md``（跳过 ``root/research/``），返回命中的指针描述。"""
    bad: list[str] = []
    for path in sorted(root.rglob("*.md")):
        relative = path.relative_to(root)
        if relative.parts and relative.parts[0] == RESEARCH_DIR:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for match in POINTER.finditer(line):
                bad.append(f"{path}:{number} -> {match.group(0)}")
    return bad


def test_live_docs_use_section_anchors_not_line_numbers() -> None:
    """活文档里出现 ``prompts/…:NN`` / ``knowledge/…:NN`` 即判红。"""
    bad = scan(DOCS_ROOT)
    assert not bad, (
        "活文档里的提示词指针不许带行号，请改成小节标题锚点"
        "（`docs/research/` 除外）：\n  " + "\n  ".join(bad))


def test_scan_recognises_bad_pointer(tmp_path: Path) -> None:
    """反例自检：坏文本必须被抓住，且 ``research/`` 必须被排除。"""
    (tmp_path / "design").mkdir()
    (tmp_path / "research").mkdir()
    (tmp_path / "design" / "bad.md").write_text(
        "见 `prompts/foo.md:12` 与 `knowledge/bar.md:3-8`。\n", encoding="utf-8")
    (tmp_path / "research" / "history.md").write_text(
        "见 `prompts/foo.md:12`。\n", encoding="utf-8")

    bad = scan(tmp_path)
    assert len(bad) == 2, f"design 的两处都该被抓住、research 该被排除：{bad}"
    assert any("prompts/foo.md:12" in item for item in bad)
    assert any("knowledge/bar.md:3" in item for item in bad)
    assert all("history.md" not in item for item in bad)
