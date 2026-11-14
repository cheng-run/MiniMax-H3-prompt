"""``check.sh`` 的变更集必须包含**本分支已提交**的改动（retro 2026-11-14）。

为什么要有这条守卫：提交之后再跑 `fast` 是验收前的常见动作，而旧判据只看
``git diff HEAD``——那时它为空，mypy / 前端 / 契约守卫会静默跳过，代理拿到假绿，
直到拆回 main 跑 ``full`` 才炸（#253 实测三处 mypy 错）。

判据直接打函数本身：把 ``changed_paths()`` 从脚本里切出来，用**假 git**喂它
三种回答（merge-base / 对基线的 diff / 对工作区的 diff），断言「对基线的 diff」
那一支真的在输出里。

为什么不用真仓库：Windows 的 ``CreateProcess`` 会先命中 ``System32`` 下的 WSL
启动器而不是 Git Bash，CI runner 上直接退出 1。假 git 把环境差异消掉，判据仍是
同一条。
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHECK_SH = ROOT / "scripts" / "check.sh"


def _extract_changed_paths() -> str:
    text = CHECK_SH.read_text(encoding="utf-8")
    match = re.search(r"^changed_paths\(\) \{.*?^\}", text, re.S | re.M)
    assert match is not None, "check.sh 里找不到 changed_paths()（改名要同步这条守卫）"
    return match.group(0)


# 假 git：只回答 changed_paths 会问的三件事。用 shell 函数而不是 PATH 上的脚本：
# Git Bash 会把自身的 /mingw64/bin 排在调用方 PATH 之前，脚本版 fake 抢不过它。
FAKE_GIT = """git() {
  for arg in "$@"; do
    if [ "$arg" = "merge-base" ]; then
      echo BASE
      return 0
    fi
  done
  if [ "$1" = "diff" ]; then
    for arg in "$@"; do
      if [ "$arg" = "BASE" ]; then
        # 只有「基线..HEAD」那一支会带 BASE；工作区那一支不会。
        echo "src/committed.py"
      fi
    done
  fi
  return 0
}
"""


def _find_git_bash() -> str | None:
    """找 Git Bash 的绝对路径。

    为什么不能直接 ``["bash", ...]``：Windows 的 ``CreateProcess`` 搜索顺序里
    ``System32`` 在 PATH 之前，会先命中 ``C:\\Windows\\System32\\bash.exe``（WSL
    启动器）。本机装了发行版时它恰好能跑，CI runner 没装发行版就直接退出 1
    （2026-11-14 实测：同一条用例本地绿、CI 红）。候选顺序显式列 Git 的安装位置，
    再退回 PATH 上不是 System32 / WindowsApps 的那个。
    """
    candidates = [
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
        r"D:\Git\bin\bash.exe",
        r"D:\Git\usr\bin\bash.exe",
    ]
    for candidate in candidates:
        if Path(candidate).is_file():
            return candidate
    import shutil

    found = shutil.which("bash")
    if found and "system32" not in found.lower() and "windowsapps" not in found.lower():
        return found
    return None


GIT_BASH = _find_git_bash()


@pytest.mark.skipif(GIT_BASH is None, reason="需要 Git Bash 来跑 check.sh 的函数定义")
def test_committed_changes_on_a_branch_are_in_scope() -> None:
    # 用 stdin 喂脚本：临时文件的路径在 Windows/Git Bash 之间要转换，stdin 不用。
    # 传 bytes 而不是 text：text=True 的 stdin 在 Windows 上会把换行翻成 CRLF，
    # bash 会把 ``pipefail\\r`` 当成非法选项名（2026-11-14 实测）。
    payload = (
        "set -euo pipefail\n" + FAKE_GIT + _extract_changed_paths()
        + "\nchanged_paths src\n"
    ).encode("utf-8")
    result = subprocess.run(
        [GIT_BASH, "-s", "--", "src"],
        input=payload,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    stdout = result.stdout.decode("utf-8", "replace")
    # 「基线..HEAD」那一支的输出必须在：漏掉它＝提交后跑 fast 会静默跳过守卫。
    assert "src/committed.py" in stdout.splitlines()
