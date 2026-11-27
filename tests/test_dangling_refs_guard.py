"""死规则守卫：活文档里的引用必须指向真实存在的东西。

为什么：规范里大量是「按 X 写」「别碰 Y」的指针。X 被删了、改名了、或从一开始就写错，
规则**不报错**——它只是悄悄变成噪音。2026-11-27 手工扫查出 15 处：`ui/menu.py` 删除后
仍被 6 处记忆引用、`CODING_STANDARDS.md` 指着一个不存在的记忆名、`tools/flicker_metric.py`
实际在 `scripts/`。现有守卫都抓不到：`test_doc_references_tracked.py` 只查
`docs/(research|design|handoffs)/` 这一种形态，且不管记忆名。

判据只认两条：路径在 `git ls-files` 里、记忆名在记忆目录里。刻意不查代码符号。

变异：`scripts/mutation_check.py` 的 `dangling_ref_ignores_missing_path`。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))

import check_dangling_refs as guard  # noqa: E402


def test_live_docs_have_no_dangling_references() -> None:
    """活文档与仓库根规范里引用的路径/记忆名，必须真实存在。"""
    tracked = set(guard._tracked(guard.REPO))
    memory_names, _ = guard._memory_files()
    bad = guard.scan(guard.REPO, tracked, memory_names)
    assert not bad, (
        "这些引用指向不存在的东西（规则会变成噪音）：\n  " + "\n  ".join(bad))


def test_scan_catches_a_missing_path(tmp_path: Path) -> None:
    """反例自检：不存在的路径被抓住，存在的与模板/通配放行。"""
    present = "docs/agents/frontend.md"
    missing = "/".join(["docs", "agents", "2099-01-01-不存在.md"])
    template = "/".join(["docs", "adr", "NNNN-slug.md"])
    globbed = "/".join(["docs", "research", "YYYY-MM-DD-主题.md"])

    (tmp_path / "AGENTS.md").write_text(
        f"`{present}` `{missing}` `{template}` `{globbed}`\n", encoding="utf-8")

    bad = guard.scan(tmp_path, {"AGENTS.md", present}, set())
    assert bad == [f"AGENTS.md -> 路径不存在: {missing}"], bad


def test_scan_catches_a_missing_memory_name(tmp_path: Path) -> None:
    """反例自检：引用了不存在的记忆名要报；存在的放行。"""
    body = "见 auto memory `存在的记忆` 与 [[也不存在]]\n"
    (tmp_path / "AGENTS.md").write_text(body, encoding="utf-8")

    bad = guard.scan(tmp_path, {"AGENTS.md"}, {"存在的记忆"})
    assert "AGENTS.md -> 记忆不存在: auto memory `存在的记忆`" not in bad
    assert any("也不存在" in line for line in bad), bad


def test_history_docs_are_not_scanned() -> None:
    """ADR 与调研/设计是历史记录，引用过已删模块属正常留痕，不该报。"""
    assert "docs/adr/" in guard._HISTORY_PREFIXES
    assert "docs/design/" in guard._HISTORY_PREFIXES
