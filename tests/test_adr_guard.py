"""ADR 守卫（#143 收尾，2026-11-02）。

两条不变式，对应「ADR 的规范一直遵守」里最容易被静默破坏的两处：

1. **引用必须落地**：代码注释、docstring、文档里的 ``ADR 0019`` / ``docs/adr/0019``
   必须指向真实存在的文件。删/改号之后，引用变成悬空指针而没有任何东西会报错——
   #143 整顿里就抓到过三处（0017 指向随 #90 退役的文件）。
2. **状态行必须存在且格式固定**：每份 ADR 文首一行
   ``> **状态（YYYY-MM-DD 复核）**：…``。状态行只追加、不覆写（仓规），格式一旦
   各写各的，就再也没法机读「哪份还被取代了、哪份已撤除」。

**引用写法约定**：本仓引用写 ``ADR 0019``（空格）或 ``docs/adr/0019``；
外部项目的 ADR 写 ``ADR-0006``（连字符），本守卫不认这种形式——
`ADR 0015` 引 FlowGate 的 `ADR-0006` 就是外部引用，不能拿本仓的 0006 去套。

变异检验：把任一引用改成不存在的号、或删掉一份 ADR 的状态行，对应用例红。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ADR_DIR = ROOT / "docs" / "adr"

# 本仓引用形式（空格 / 路径）；连字符形式是外部 ADR，不参与核对。
_REF = re.compile(r"ADR[ ](\d{4})|adr/(\d{4})")
# 状态行：日期 + 「复核」+ 冒号后有内容。
_STATUS = re.compile(r"^> \*\*状态（(\d{4}-\d{2}-\d{2}) 复核）\*\*：\S")

_SCAN_SUFFIXES = {".py", ".md"}
_SKIP_DIRS = {".git", ".venv", "__pycache__", "output", "worktree", "node_modules"}


def _adr_numbers() -> set[str]:
    return {path.name[:4] for path in ADR_DIR.glob("*.md")}


def _iter_files():
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in _SCAN_SUFFIXES:
            continue
        if _SKIP_DIRS & set(part for part in path.relative_to(ROOT).parts):
            continue
        yield path


def _python_comment_and_docstring_text(path: Path) -> str:
    """只取 .py 的注释与 docstring：字符串字面量是**数据**，不是引用。

    为什么必须分开：``scripts/mutation_check.py`` 里存着一个**故意写坏的号**（用来
    检验本守卫会不会咬人）。把字面量也算进来，守卫会去咬自己的测试数据——那是
    「守卫咬测试夹具」而不是「守卫咬悬空指针」。本文件的 docstring 同样不许出现
    那个号，否则它咬完夹具接着咬自己。
    """
    import ast
    import io
    import tokenize

    text = path.read_text(encoding="utf-8", errors="replace")
    chunks: list[str] = []
    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type == tokenize.COMMENT:
                chunks.append(token.string)
    except (tokenize.TokenError, IndentationError):
        return text  # 解析不了就退回全文，宁多勿漏
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return "\n".join(chunks)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                chunks.append(doc)
    return "\n".join(chunks)


def test_every_referenced_adr_exists():
    """引用落地：写不出对应文件的号就是悬空指针。"""
    known = _adr_numbers()
    dangling: list[str] = []
    seen = 0
    for path in _iter_files():
        try:
            if path.suffix == ".py":
                text = _python_comment_and_docstring_text(path)
            else:
                text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        # .py 取的是注释/docstring 的拼接文本，行号对不回原文件——只报文件与号，
        # 不报假行号（「写不出文件:行号的事实句不算事实」同一条纪律：宁可少报一段）。
        for match in _REF.finditer(text):
            number = match.group(1) or match.group(2)
            seen += 1
            if number not in known:
                rel = path.relative_to(ROOT).as_posix()
                marker = f"{rel} → ADR {number}"
                if marker not in dangling:
                    dangling.append(marker)
    assert seen > 50, f"只扫到 {seen} 处 ADR 引用，守卫可能在空转"
    assert not dangling, (
        "这些引用指向不存在的 ADR：\n  " + "\n  ".join(dangling[:20]))


def test_every_adr_has_a_well_formed_status_line():
    offenders: list[str] = []
    for path in sorted(ADR_DIR.glob("*.md")):
        lines = path.read_text(encoding="utf-8").splitlines()
        # 只看标题之后的头 10 行：状态行是文首那一行，不是正文里的引用
        head = [line for line in lines[1:11] if _STATUS.match(line)]
        if len(head) != 1:
            offenders.append(f"{path.name}（找到 {len(head)} 行状态）")
    assert not offenders, (
        "这些 ADR 缺少（或多于一条）合规状态行，格式应为 "
        "`> **状态（YYYY-MM-DD 复核）**：…`：\n  " + "\n  ".join(offenders))


def test_the_adr_directory_is_not_empty():
    """守卫自身别退化：路径写错导致一份都没查，本用例会红。"""
    assert len(_adr_numbers()) >= 27, "docs/adr 里没有 27 份 ADR，路径或文件名约定变了？"
