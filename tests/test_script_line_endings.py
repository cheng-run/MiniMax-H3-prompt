"""行尾守卫：shell 脚本不许有 CRLF（2026-11-02 实测事故），Windows 批处理反过来不许有裸 LF。

shell 侧现场：`bash scripts/check.sh types` 报 `invalid option name`——`set -euo pipefail`
末尾粘了 `\\r`，bash 把它当成选项名。`.gitattributes` 只保证**提交内容**是 LF，工作区
那份在下次 checkout 之前一直是 CRLF，任何 `bash` 调用当场失败；而 `.git/hooks/pre-commit`
调的正是它。

批处理侧现场（2026-11-13）：`start_web.cmd` 用 LF 写盘后双击，报 `'dp0"' 不是内部或外部
命令`——`cd /d "%~dp0"` 被 cmd.exe 劈成两半。方向相反，同样的坑。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _shell_files() -> list[Path]:
    return sorted(
        [*ROOT.glob("scripts/*.sh"), *ROOT.glob("scripts/hooks/*")]
    )


def _batch_files() -> list[Path]:
    """仓库里所有交给 cmd.exe 执行的文件（根目录与 scripts/ 下）。"""
    return sorted([*ROOT.glob("*.cmd"), *ROOT.glob("*.bat"),
                   *ROOT.glob("scripts/*.cmd"), *ROOT.glob("scripts/*.bat")])


def test_shell_scripts_have_lf_line_endings():
    offenders = [path.relative_to(ROOT).as_posix()
                 for path in _shell_files()
                 if path.is_file() and b"\r\n" in path.read_bytes()]
    assert not offenders, (
        f"这些 shell 脚本是 CRLF 行尾，bash 会报 “invalid option name”：{offenders}。"
        "修法：用 newline='\\n' 重写一次（git 提交内容本来是 LF，坏的是工作区那份）。")


def test_the_list_is_not_empty():
    """守卫自身别退化：路径写错导致一个文件都没查，本用例会红。"""
    assert _shell_files(), "没找到任何 shell 脚本，守卫在空转"


def test_batch_files_have_crlf_line_endings():
    """批处理反过来：cmd.exe 遇到 LF-only 的文件会从中间劈开执行。

    2026-11-13 实测：`start_web.cmd` 用 LF 写盘后双击，报
    `'dp0"' 不是内部或外部命令`——`cd /d "%~dp0"` 被劈成两半。`.gitattributes`
    的 `eol=crlf` 只保证**检出**是 CRLF，会话里新写的文件在工作区仍是 LF。
    """
    offenders = [path.relative_to(ROOT).as_posix()
                 for path in _batch_files()
                 if path.is_file() and re.search(rb"(?<!\r)\n", path.read_bytes())]
    assert not offenders, (
        f"这些批处理是裸 LF 行尾，cmd.exe 会劈开命令执行：{offenders}。"
        "修法：按 CRLF 重写一次（`eol=crlf` 只管检出，不管工作区）。")


def test_the_batch_list_is_not_empty():
    assert _batch_files(), "没找到任何批处理文件，守卫在空转"
