"""``check.sh`` 的变更集必须包含**本分支已提交**的改动（retro 2026-11-14）。

为什么要有这条守卫：提交之后再跑 `fast` 是验收前的常见动作，而旧判据只看
`git diff HEAD`——那时它为空，mypy / 前端 / 契约守卫会静默跳过，代理拿到假绿，
直到拆回 main 跑 `full` 才炸（#253 实测三处 mypy 错）。

判据直接打函数本身：把 `changed_paths()` 从脚本里切出来，在一个临时仓库里造
「main 上的基线 + 分支上的一次提交」，断言它看得见那次提交。切函数而不是跑整个
`check.sh`，是因为后者会真的跑一遍检查（分钟级），而这里要测的只是一行 git 语义。
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECK_SH = ROOT / "scripts" / "check.sh"


def _extract_changed_paths() -> str:
    text = CHECK_SH.read_text(encoding="utf-8")
    match = re.search(r"^changed_paths\(\) \{.*?^\}", text, re.S | re.M)
    assert match is not None, "check.sh 里找不到 changed_paths()（改名要同步这条守卫）"
    return match.group(0)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=cwd, check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def test_committed_changes_on_a_branch_are_in_scope(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "guard@example.invalid")
    _git(tmp_path, "config", "user.name", "guard")
    # 关掉换行转换：本机 core.autocrlf=true，临时仓库里的 `git diff` 会把每个文件都
    # 算成「改动」，断言就测不到分支提交这一条判据了。
    _git(tmp_path, "config", "core.autocrlf", "false")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "base.py").write_text("x = 1\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "base")

    _git(tmp_path, "switch", "-c", "feature")
    (tmp_path / "src" / "committed.py").write_text("y = 2\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "feature")

    script = tmp_path / "changed_paths.sh"
    # newline="\n"：Windows 默认会写 CRLF，bash 读到的行尾带着 \r，
    # 函数体第一行就成了 `local base\r`（exit 127）——不是脚本的问题。
    script.write_text(
        "set -euo pipefail\n" + _extract_changed_paths() + '\nchanged_paths "$@"\n',
        encoding="utf-8", newline="\n")
    result = subprocess.run(
        # 传相对文件名：Git Bash 会把 `C:\...` 里的反斜杠吞掉（exit 127
        # "No such file or directory"），cwd 已经指向临时仓库。
        ["bash", script.name, "src"], cwd=tmp_path, check=True,
        capture_output=True, text=True)
    assert "src/committed.py" in result.stdout.splitlines()
    # 未改动过的基线文件不该被算进来（变更集不是「整个仓库」）。
    assert "src/base.py" not in result.stdout.splitlines()
