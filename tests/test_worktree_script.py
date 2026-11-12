"""worktree.sh 生命周期冒烟（retro 2026-11-04，复盘候选 4）。

为什么需要：2026-11-04 #157 并行实施暴露了两处脚本 bug——`add` 对已存在的 `docs/`
再 `cp -r` 生成 `docs/docs/`（`test_doc_pointer_shape` 报假红、pytest 重复收测）；
`remove` 把已入库的 `tests/` 刷新成主树版本，再被自己的脏检查拒绝删除。脚本是
`/implement-spec` 并行的骨架，却在这次之前没有一条会跑的用例。

做法：在临时目录造一个最小 git 仓库（main 一次提交、tracked `tests/scripts/docs`、
未跟踪的调研笔记、`.gitignore` 忽略 `worktree/`），把仓内 `scripts/worktree.sh`
复制进去，跑 add/remove，断言：未跟踪笔记被补进工作树、没有 `docs/docs` 之类的嵌套
副本、干净时 remove 成功、分支保留、脚本自己带入的副本不会把删除拦住。
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).parents[1]
BASH = shutil.which("bash")


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    assert BASH, "worktree.sh 的冒烟需要 bash（Git Bash / CI 的既有依赖）"
    return subprocess.run(
        [BASH, str(repo / "scripts" / "worktree.sh"), *args],
        cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace")


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=True)
    return result.stdout


def _init_repo(root: Path) -> None:
    root.mkdir()
    (root / "scripts").mkdir()
    (root / "tests").mkdir()
    (root / "docs" / "research").mkdir(parents=True)
    (root / ".gitignore").write_text("/worktree/\n.venv/\n", encoding="utf-8")
    (root / "scripts" / "worktree.sh").write_text(
        (REPO / "scripts" / "worktree.sh").read_text(encoding="utf-8"), encoding="utf-8")
    (root / "tests" / "test_dummy.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8")
    (root / "docs" / "research" / "tracked.md").write_text("tracked\n", encoding="utf-8")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", ".")
    _git(root, "-c", "user.email=t@example.com", "-c", "user.name=t",
         "commit", "-q", "-m", "init")
    # 主树独有的当天调研笔记：add 应把它补进工作树，remove 应认得它是脚本带入的。
    (root / "docs" / "research" / "untracked.md").write_text("note\n", encoding="utf-8")


def test_add_copies_untracked_notes_without_nested_directories(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)

    result = _run(repo, "add", "900")
    assert result.returncode == 0, result.stderr
    target = repo / "worktree" / "ticket-900"
    assert target.is_dir(), "工作树没建出来"
    assert (target / "docs" / "research" / "untracked.md").is_file(), \
        "主树的未跟踪调研笔记没有被带进工作树"
    assert (target / "tests" / "test_dummy.py").is_file(), "tracked tests 没有检出"
    for nested in ("docs/docs", "tests/tests", "scripts/scripts"):
        assert not (target / nested).exists(), f"出现了嵌套副本：{nested}"
    # tests 已入库时不再留 harvest 基线快照（那条口径只服务未入库副本）。
    assert not (repo / "worktree" / ".base-900").exists()


def test_remove_succeeds_and_keeps_the_branch(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    assert _run(repo, "add", "901").returncode == 0
    target = repo / "worktree" / "ticket-901"

    result = _run(repo, "remove", "901")
    assert result.returncode == 0, result.stderr
    assert not target.exists(), "工作树没删掉"
    assert "ticket/901" in _git(repo, "branch", "--list"), "分支应保留（按仓规合回 main 后再删）"
    assert not (repo / "worktree" / ".backfilled-901").exists(), \
        "脚本带入的副本清单应在删除时清掉"


def test_remove_refuses_a_user_edited_backfilled_note(tmp_path):
    """用户改过脚本带入的笔记时，删除必须被脏检查拦下（不静默丢用户改动）。"""
    repo = tmp_path / "repo"
    _init_repo(repo)
    assert _run(repo, "add", "902").returncode == 0
    note = repo / "worktree" / "ticket-902" / "docs" / "research" / "untracked.md"
    note.write_text("user edit\n", encoding="utf-8")

    result = _run(repo, "remove", "902")
    assert result.returncode != 0
    assert "未提交改动" in result.stderr
    assert note.is_file(), "用户改过的副本不能被静默删除"


def test_link_venv_then_remove_keeps_the_main_environment(tmp_path):
    """`.venv` 联接必须由 remove 摘下：删树不能穿透到主树环境（2026-11-12 实测）。"""
    if not shutil.which("powershell.exe"):
        pytest.skip("link-venv 用 Windows 目录联接，非 Windows 环境跳过")
    repo = tmp_path / "repo"
    _init_repo(repo)
    stub = repo / ".venv" / "Scripts" / "python.exe"
    stub.parent.mkdir(parents=True)
    stub.write_text("stub\n", encoding="utf-8")
    stub.chmod(0o755)

    assert _run(repo, "add", "903").returncode == 0
    linked = _run(repo, "link-venv", "903")
    assert linked.returncode == 0, linked.stderr
    venv_link = repo / "worktree" / "ticket-903" / ".venv"
    assert venv_link.exists(), "link-venv 没建出联接"

    result = _run(repo, "remove", "903")
    assert result.returncode == 0, result.stderr
    assert not (repo / "worktree" / "ticket-903").exists(), "工作树没删掉"
    assert stub.is_file(), "主树 .venv 被穿透删除"


def test_remove_refuses_a_real_venv_directory(tmp_path):
    """不是联接的真实 .venv 不能被静默删掉（Directory.Delete 对非空目录抛错）。"""
    repo = tmp_path / "repo"
    _init_repo(repo)
    assert _run(repo, "add", "904").returncode == 0
    real = repo / "worktree" / "ticket-904" / ".venv" / "Scripts"
    real.mkdir(parents=True)
    (real / "python.exe").write_text("real\n", encoding="utf-8")

    result = _run(repo, "remove", "904")
    assert result.returncode != 0
    assert "不是空的目录联接" in result.stderr
    assert (real / "python.exe").is_file(), "拒绝后不能留下半删状态"


def _make_junction(link: Path, target: Path) -> None:
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command",
         f"New-Item -ItemType Junction -Path '{link}' -Target '{target}' | Out-Null"],
        check=True, capture_output=True, text=True)


def test_remove_path_unlinks_frontend_junction_in_self_made_tree(tmp_path):
    """自建工作树走 remove-path 时也要先摘 node_modules 联接（2026-11-12 实测：
    绕过脚本直接 `git worktree remove`，顺着联接把主树依赖删空）。"""
    if not shutil.which("powershell.exe"):
        pytest.skip("remove-path 的联接保护依赖 Windows 目录联接")
    repo = tmp_path / "repo"
    _init_repo(repo)
    main_dep = repo / "frontend" / "node_modules" / "marker.txt"
    main_dep.parent.mkdir(parents=True)
    main_dep.write_text("keep me\n", encoding="utf-8")

    target = repo / "worktree" / "integration-999"
    _git(repo, "worktree", "add", str(target), "-b", "integ/999", "main")
    (target / "frontend").mkdir()
    _make_junction(target / "frontend" / "node_modules", repo / "frontend" / "node_modules")

    result = _run(repo, "remove-path", str(target))
    assert result.returncode == 0, result.stderr
    assert not target.exists(), "自建工作树没删掉"
    assert main_dep.is_file(), "主树 node_modules 被联接穿透删除"


def test_remove_path_refuses_unknown_top_level_junction(tmp_path):
    """顶层还有别的联接就拒绝：git worktree remove 会顺着它删进目标。"""
    if not shutil.which("powershell.exe"):
        pytest.skip("顶层联接检测依赖 PowerShell")
    repo = tmp_path / "repo"
    _init_repo(repo)
    outside = repo / "outside"
    (outside / "keep.txt").parent.mkdir(parents=True)
    (outside / "keep.txt").write_text("keep\n", encoding="utf-8")

    target = repo / "worktree" / "integration-998"
    _git(repo, "worktree", "add", str(target), "-b", "integ/998", "main")
    _make_junction(target / "models", outside)

    result = _run(repo, "remove-path", str(target))
    assert result.returncode != 0
    assert "顶层还有目录联接" in result.stderr
    assert (outside / "keep.txt").is_file(), "拒绝后联接目标不能被删"
