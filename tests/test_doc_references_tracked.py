"""被 tracked 文件引用的 docs 路径必须已入库（#157 复审采纳，2026-11-04）。

为什么需要：Standards 轴抓到 ``tests/test_director_canvas_aspect.py`` 引用
``docs/research/2026-11-04-质量验收-快过年返乡.md``，而该文件当时还没入库——干净检出会丢掉
5% 容差的唯一证据，且没有任何守卫会报。``tests/test_doc_pointer_shape.py`` 只管行号指针、
且把 ``docs/research/`` 排除在外，正好漏掉这一类。

判据：扫描 ``git ls-files`` 里的文本文件，找 ``docs/(research|design|handoffs)/*.md`` 的
具体字面路径；模板（YYYY/NNNN/主题）、glob（*）与 brace（{}）不是具体引用，跳过。每个
具体路径必须是 ``git ls-files`` 的成员。
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).parents[1]
TEXT_SUFFIXES = {".md", ".py", ".sh", ".toml", ".yaml", ".yml", ".txt", ".json"}
_CITATION = re.compile(r"docs/(?:research|design|handoffs)/[^\s`'\"<>|]+?\.md")
_NON_CONCRETE = ("YYYY", "NNNN", "主题", "*", "{", "}")

# 变异检验脚本里**故意**放着一条不存在的路径作扰动源（守卫必须咬住它），
# 那不是对 docs 的引用。排除这一份，守卫才不会被自己的测试夹具绊倒。
_EXCLUDED = {"scripts/mutation_check.py"}


def _git_files(repo: Path) -> list[str]:
    raw = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "-z"],
        capture_output=True, check=True).stdout
    return [item.decode("utf-8") for item in raw.split(b"\0") if item]


def scan(repo: Path, tracked: set[str]) -> list[str]:
    """返回 ``[引用方 -> 未入库的 docs 路径]``；具体路径才算，模板/glob/brace 跳过。"""
    bad: list[str] = []
    for relative in sorted(tracked - _EXCLUDED):
        if Path(relative).suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            text = (repo / relative).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for match in _CITATION.finditer(text):
            cited = match.group(0).rstrip(".,;:）)")
            if any(token in cited for token in _NON_CONCRETE):
                continue
            if cited not in tracked:
                bad.append(f"{relative} -> {cited}")
    return bad


def test_every_concrete_docs_reference_is_tracked() -> None:
    """干净检出必须能拿到被引用的证据文件；悬空引用即红。"""
    tracked = _git_files(REPO)
    bad = scan(REPO, set(tracked))
    assert not bad, (
        "这些 docs 路径被 tracked 文件引用，但文件本身没入库"
        "（干净检出会丢掉证据）：\n  " + "\n  ".join(bad))


def test_scan_catches_an_untracked_reference(tmp_path: Path) -> None:
    """反例自检：具体路径被抓住；模板/glob/brace 与已入库路径放行。"""
    # 路径在源码里拆开拼，避免守卫把这份测试文件自己也判成悬空引用。
    missing = "/".join(["docs", "research", "2099-01-01-不存在.md"])
    template = "/".join(["docs", "research", "YYYY-MM-DD-主题.md"])
    globbed = "/".join(["docs", "research", "2099-01-01-*.md"])
    present = "/".join(["docs", "research", "2026-11-04-质量验收-快过年返乡.md"])
    (tmp_path / "cite.md").write_text(
        f"`{missing}` `{template}` `{globbed}` `{present}`\n", encoding="utf-8")

    bad = scan(tmp_path, {"cite.md", present})
    assert bad == [f"cite.md -> {missing}"], bad
