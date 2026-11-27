"""新增的测试文件必须当场表态归属快档（#370）。

为什么要有这条守卫：``FAST_TESTS`` 是手工维护的清单，新写的秒级守卫是否挂进去完全
靠人记——不记得也不报错，只是悄悄退化成「全量才跑到」。retro 反复抓到同一形态
（``test_role_prompt_inputs.py`` / ``test_api_problems.py`` / ``test_theme_guard.py``），
``scripts/check.sh`` 原本只能事后逐条手补，没有任何机制阻止下一条重犯。

判据是**纯文本**，不跑新增文件、不计时。票面已实测证伪另外三条形态：「跑一遍测速」
（重型文件先白跑几十秒）、「按文件名 ``guard`` 匹配」与「静态分类建 app/LLM」
（对真实事故的覆盖率是 0/3）。规则只有一条：变更集里**新增**的 ``tests/test_*.py``，
要么出现在 ``FAST_TESTS``，要么在文件里写一行 ``# fast-tier: excluded — <理由>``。

只对**新增**生效：存量两百多个非快档文件 grandfather，改历史文件不追责——事故形态
永远是「新写的守卫没挂」，一次性回填只是往并行票节奏里塞一个大提交。

变更集的取法与 ``scripts/check.sh`` 的 ``changed_paths()`` 同源：本分支相对 ``main``
的已提交改动 ∪ 工作区（含未跟踪）。浅检出没有 ``main`` 引用时退化成工作区，与
``changed_paths()`` 的回落一致。
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHECK_SH = ROOT / "scripts" / "check.sh"
SELF = "tests/test_fast_tier_coverage.py"

# `FAST_TESTS=( … )` 数组的边界；条目一行一个，允许 `#` 注释与行尾注释。
_ARRAY = re.compile(r"^FAST_TESTS=\(\n(.*?)^\)", re.S | re.M)

# 显式声明：整行以 `#` 开头（纯注释行），标题后必须跟一个破折号与**非空**理由。
# 破折号接受 em/en/hyphen 三种写法，避免因为记不住是哪一个而白红。
_DECLARED = re.compile(
    r"^[ \t]*#[ \t]*fast-tier:[ \t]*excluded\b[ \t]*[—–-][ \t]*\S", re.M)


# 子进程里要摘掉的 git 环境变量：pre-commit 钩子跑本守卫时会导出 ``GIT_INDEX_FILE``
# （指向主库的 index.lock）与 ``GIT_DIR``，子进程若继承它们，``git -C <临时仓库>``
# 会去动**主库的 index**、甚至``commit`` 直接失败（2026-11-27 实测：临时仓库 commit
# 退出 1、存量文件判成新增）。只靠 ``-C`` 定位仓库，环境必须干净。
_GIT_ENV_VARS = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_PREFIX",
    "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CEILING_DIRECTORIES", "GIT_NAMESPACE",
)


def _git_env() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key not in _GIT_ENV_VARS}


def _git(repo: Path, *args: str) -> str:
    """只读地调一次 git，返回 stdout；失败直接抛（读不到变更集时不许静默放行）。"""
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
        env=_git_env(),
    ).stdout


def _is_new_test(relative: str) -> bool:
    """仓库相对路径是不是一个测试文件（``tests/**/test_*.py``）。"""
    if not relative.startswith("tests/"):
        return False
    name = Path(relative).name
    return name.startswith("test_") and name.endswith(".py")


def newly_added_tests(repo: Path = ROOT, base_ref: str = "main") -> list[str]:
    """变更集里**新增**的测试文件（仓库相对路径，已排序）。

    「新增」＝相对 ``base_ref`` 的 ``A`` 状态，外加工作区里未跟踪的文件。只用 ``A``
    而不是全部改动：存量文件被修改/重命名不算「新写的守卫」，不追责。
    """
    try:
        base = _git(repo, "merge-base", "HEAD", base_ref).strip()
    except subprocess.CalledProcessError:
        # 浅检出/无 main 引用：退化成只看工作区，与 check.sh 的 changed_paths() 一致。
        base = ""
    added: set[str] = set()
    if base:
        added.update(_git(
            repo, "diff", "--name-only", "--diff-filter=A", base, "HEAD", "--", "tests",
        ).splitlines())
    # `git diff HEAD` 看得见已 `git add` 的新文件；未跟踪的只能靠 ls-files 补。
    added.update(_git(
        repo, "diff", "--name-only", "--diff-filter=A", "HEAD", "--", "tests",
    ).splitlines())
    added.update(_git(
        repo, "ls-files", "--others", "--exclude-standard", "--", "tests",
    ).splitlines())
    return sorted(path for path in added if _is_new_test(path))


def fast_tests(check_sh: Path = CHECK_SH) -> set[str]:
    """``check.sh`` 里 ``FAST_TESTS`` 数组的条目（含行尾注释的行只取第一段）。"""
    text = check_sh.read_text(encoding="utf-8")
    match = _ARRAY.search(text)
    assert match is not None, (
        "scripts/check.sh 里找不到 FAST_TESTS=( … ) 数组；改名或改形态要同步这条守卫")
    entries: set[str] = set()
    for line in match.group(1).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        entries.add(stripped.split()[0])
    return entries


def missing_declarations(
    added: list[str], fast: set[str], repo: Path = ROOT,
) -> list[str]:
    """新增测试文件里既不在 ``FAST_TESTS``、又没有 ``# fast-tier: excluded`` 的那些。"""
    missing: list[str] = []
    for relative in added:
        if relative in fast:
            continue
        path = repo / relative
        # HEAD 里新增、工作区已删掉的路径无从判断归属，跳过（它不是「新写的守卫」）。
        if not path.is_file():
            continue
        if _DECLARED.search(path.read_text(encoding="utf-8", errors="replace")):
            continue
        missing.append(relative)
    return missing


def test_new_test_files_declare_their_fast_tier() -> None:
    missing = missing_declarations(newly_added_tests(), fast_tests())
    assert not missing, (
        "这些新增的测试文件没有当场表态归属快档（#370）：\n  "
        + "\n  ".join(missing)
        + "\n补法：要么加进 scripts/check.sh 的 FAST_TESTS（秒级 + 结构性强），"
        "\n要么在文件里加一行 `# fast-tier: excluded — <理由>`（说明为何只进全量）。")


def test_this_guard_itself_is_in_the_fast_tier() -> None:
    """守卫自己也得挂快档：它漏挂就退化成「全量才跑到」，正是本票要堵的形态。"""
    assert SELF in fast_tests(), (
        f"{SELF} 自身必须进 scripts/check.sh 的 FAST_TESTS（#370）")


# ---- 自检：用 tmp git 仓库证明守卫既能放行也能报红（永不为红＝没有） ----

def _git_do(repo: Path, *args: str) -> None:
    """写操作要带作者身份：CI/临时仓库没有全局 user.name/user.email。"""
    env = _git_env()
    env.update({
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    })
    subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, check=True, env=env,
    )


def _write_check_sh(repo: Path, entries: list[str]) -> None:
    body = "".join(f"  {entry}\n" for entry in entries)
    (repo / "scripts" / "check.sh").write_text(
        f"FAST_TESTS=(\n{body})\n", encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """一个只有 ``main`` 上一个存量测试文件的临时仓库，当前分支 ``ticket``。"""
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "scripts").mkdir()
    _git_do(root, "init", "-b", "main")
    (root / "tests" / "test_old.py").write_text(
        "# 存量文件，改它不追责\n", encoding="utf-8")
    _write_check_sh(root, [])
    _git_do(root, "add", "-A")
    _git_do(root, "commit", "-m", "base")
    _git_do(root, "switch", "-c", "ticket")
    return root


def test_detects_new_files_and_requires_a_declaration(repo: Path) -> None:
    # 三种情形同批落盘：进 FAST_TESTS、有声明、什么都没有。
    _write_check_sh(repo, ["tests/test_in_fast.py"])
    (repo / "tests" / "test_in_fast.py").write_text("", encoding="utf-8")
    (repo / "tests" / "test_excluded.py").write_text(
        "# fast-tier: excluded — 需要真实模型，约 20s\n", encoding="utf-8")
    (repo / "tests" / "test_bare.py").write_text("", encoding="utf-8")
    _git_do(repo, "add", "tests", "scripts")
    _git_do(repo, "commit", "-m", "add three")

    added = newly_added_tests(repo)
    # 存量 ``test_old.py`` 在 main 上，不算新增。
    assert set(added) == {
        "tests/test_in_fast.py", "tests/test_excluded.py", "tests/test_bare.py"}

    missing = missing_declarations(added, fast_tests(repo / "scripts" / "check.sh"), repo)
    assert missing == ["tests/test_bare.py"]


def test_untracked_new_file_is_checked(repo: Path) -> None:
    (repo / "tests" / "test_untracked.py").write_text("", encoding="utf-8")
    assert newly_added_tests(repo) == ["tests/test_untracked.py"]


def test_staged_new_file_is_checked(repo: Path) -> None:
    (repo / "tests" / "test_staged.py").write_text("", encoding="utf-8")
    _git_do(repo, "add", "tests/test_staged.py")
    assert "tests/test_staged.py" in newly_added_tests(repo)


def test_modified_old_file_is_not_new(repo: Path) -> None:
    path = repo / "tests" / "test_old.py"
    path.write_text("# 改一行\n", encoding="utf-8")
    _git_do(repo, "add", "tests/test_old.py")
    _git_do(repo, "commit", "-m", "touch old")
    assert newly_added_tests(repo) == []


def test_declaration_forms(repo: Path) -> None:
    cases = {
        "test_em_dash.py": "# fast-tier: excluded — 依赖真实模型\n",
        "test_en_dash.py": "# fast-tier: excluded – 全量专用\n",
        "test_hyphen.py": "# fast-tier: excluded - 全量专用\n",
        "test_no_reason.py": "# fast-tier: excluded —\n",
        "test_in_string.py": "X = '# fast-tier: excluded — 字符串里的不算'\n",
    }
    for name, text in cases.items():
        (repo / "tests" / name).write_text(text, encoding="utf-8")
    added = sorted(f"tests/{name}" for name in cases)
    missing = missing_declarations(added, fast=set(), repo=repo)
    assert missing == ["tests/test_in_string.py", "tests/test_no_reason.py"]


def test_fast_tests_parser_ignores_comments(tmp_path: Path) -> None:
    check = tmp_path / "check.sh"
    check.write_text(
        "FAST_TESTS=(\n"
        "  # 注释行不是条目\n"
        "  tests/test_a.py\n"
        "  tests/test_b.py  # 行尾注释\n"
        ")\n",
        encoding="utf-8",
    )
    assert fast_tests(check) == {"tests/test_a.py", "tests/test_b.py"}
