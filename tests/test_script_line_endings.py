"""shell 脚本不许有 CRLF（2026-11-02 实测事故）。

现场：`bash scripts/check.sh types` 报

```
: invalid option namee 20: set: pipefail
```

——`set -euo pipefail` 末尾粘了 `\\r`，bash 把它当成选项名。触发条件是**在工作区
用写文件的工具改过脚本**（Windows 默认写 CRLF），而 `.gitattributes` 只保证
**提交内容**是 LF：工作区那份在下次 checkout 之前一直是 CRLF，任何 `bash` 调用
当场失败。

`.git/hooks/pre-commit` 调的正是 `scripts/check.sh`，所以它坏掉时提交会被拦住——
但错误信息（"invalid option name"）不会告诉人「是行尾问题」。这条用例把它变成
一句人话，同时保护 hooks 目录里同样靠 shebang 执行的 `pre-commit` / `commit-msg`。
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _shell_files() -> list[Path]:
    return sorted(
        [*ROOT.glob("scripts/*.sh"), *ROOT.glob("scripts/hooks/*")]
    )


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
