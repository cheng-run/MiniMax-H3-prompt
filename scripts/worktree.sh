#!/usr/bin/env bash
# 工作树生命周期助手（retro 2026-10-31 候选 3）。
#
# 为什么需要：tests/ 与 scripts/ 自 2026-10-30 起不入库，`git worktree add` 出来的树
# 里没有它们，每次都要手动复制；删除时 `git worktree remove --force` 又会绕过脏检查
# ——#112 实测因此丢过两条未提交的 src 文案（子代理还在处理追加意见，工作树已被删）。
# 这个脚本把「建树复制」与「删树先核对」都做成显式动作。
#
# 用法：
#   scripts/worktree.sh add NN [base]
#   scripts/worktree.sh remove NN [--force]
set -euo pipefail

MAIN_ROOT=$(git worktree list --porcelain | awk '/^worktree /{print substr($0,10); exit}')
[ -n "$MAIN_ROOT" ] || { echo "[错误] 找不到主工作树" >&2; exit 1; }
PY="$MAIN_ROOT/.venv/Scripts/python.exe"
[ -x "$PY" ] || PY=python

usage() {
  cat >&2 <<'USAGE'
用法：
  scripts/worktree.sh add NN [base]
      建 worktree/ticket-NN（分支 ticket/NN，基线默认=主树当前分支），
      复制主树的 tests/、scripts/、docs/ 进去，并留一份 tests 基线快照
      （worktree/.base-NN）供 harvest 判断「哪些测试文件是这张票改的」。
  scripts/worktree.sh harvest NN
      把工作树里改过的测试文件回灌主树：主树仍是基线版本才覆盖；
      主树与工作树都改过同一文件时列出冲突并退出 1（不猜）。
  scripts/worktree.sh remove NN [--force]
      默认先自动 harvest，再同步主树最新 tests 副本；删前检查跟踪文件脏改动。
      任一项不满足就拒绝；--force 会打印警告后跳过。
USAGE
  exit 2
}

# 把工作树里的测试改动回灌主树（tests/ 被 .gitignore 挡住，不随 git merge 回来）。
# 判据只有一条：**主树仍是基线版本才覆盖**；两边都改过就报冲突，不猜谁对。
harvest() {
  local id=$1 target base rel f changed=0
  local -a conflicts=()
  target="$MAIN_ROOT/worktree/ticket-$id"
  base="$MAIN_ROOT/worktree/.base-$id/tests"
  if [ ! -d "$target/tests" ]; then
    echo "[跳过] 没有测试副本：$target/tests" >&2
    return 0
  fi
  if [ ! -d "$base" ]; then
    echo "[跳过] 没有基线快照 $base（建树早于本版脚本）；请人工比对 tests/" >&2
    return 0
  fi

  while IFS= read -r -d '' f; do
    rel=${f#"$target/tests/"}
    case "$rel" in *__pycache__*|*.pytest_cache*) continue ;; esac
    if [ -f "$base/$rel" ] && cmp -s "$f" "$base/$rel"; then
      continue   # 这张票没动这个文件
    fi
    if [ -f "$MAIN_ROOT/tests/$rel" ] && cmp -s "$f" "$MAIN_ROOT/tests/$rel"; then
      continue   # 主树已经是这一版
    fi
    if [ ! -e "$MAIN_ROOT/tests/$rel" ]; then
      cp -p "$f" "$MAIN_ROOT/tests/$rel"; echo "  + $rel"; changed=$((changed+1))
      continue
    fi
    if [ ! -e "$base/$rel" ] || cmp -s "$MAIN_ROOT/tests/$rel" "$base/$rel"; then
      cp -p "$f" "$MAIN_ROOT/tests/$rel"; echo "  ~ $rel"; changed=$((changed+1))
      continue
    fi
    conflicts+=("$rel")
  done < <(find "$target/tests" -type f -print0)

  # 工作树里删掉的测试：只在主树仍是基线版本时跟着删。
  while IFS= read -r -d '' f; do
    rel=${f#"$base/"}
    case "$rel" in *__pycache__*|*.pytest_cache*) continue ;; esac
    [ -e "$target/tests/$rel" ] && continue
    [ -e "$MAIN_ROOT/tests/$rel" ] || continue
    if cmp -s "$MAIN_ROOT/tests/$rel" "$f"; then
      rm -f "$MAIN_ROOT/tests/$rel"; echo "  - $rel"; changed=$((changed+1))
    else
      conflicts+=("$rel（工作树删除、主树有改动）")
    fi
  done < <(find "$base" -type f -print0)

  if [ ${#conflicts[@]} -gt 0 ]; then
    echo "[冲突] 主树与工作树都改过同一批测试文件，不自动覆盖：" >&2
    printf '  %s\n' "${conflicts[@]}" >&2
    return 1
  fi
  echo "[harvest] $changed 个测试文件已回灌主树"
  return 0
}

case "${1:-}" in
  add)
    id=${2:-}
    [ -n "$id" ] || usage
    case "$id" in *[!A-Za-z0-9_-]*) echo "[错误] 票号只允许字母/数字/下划线/短横：$id" >&2; exit 2 ;; esac
    base=${3:-$(git -C "$MAIN_ROOT" branch --show-current)}
    target="$MAIN_ROOT/worktree/ticket-$id"
    branch="ticket/$id"
    if [ -e "$target" ]; then
      echo "[错误] 目标已存在：$target（先 remove 或换票号）" >&2
      exit 1
    fi
    if git -C "$MAIN_ROOT" show-ref --verify --quiet "refs/heads/$branch"; then
      echo "[错误] 分支已存在：$branch（先删旧分支或换票号）" >&2
      exit 1
    fi
    git -C "$MAIN_ROOT" worktree add "$target" -b "$branch" "$base"

    missing=()
    # docs/ 也复制：规格与设计文档住在 docs/design/，而它和 tests/scripts 一样被
    # .gitignore 挡住——不复制的话，工作树里的实现代理看不到票面指向的规格
    # （2026-11-01 retro：协调会话只能给每个代理手写主树绝对路径）。
    for dir in tests scripts docs; do
      if [ -d "$MAIN_ROOT/$dir" ]; then
        cp -r "$MAIN_ROOT/$dir" "$target/$dir"
      else
        missing+=("$dir")
      fi
    done

    # tests 基线快照：harvest 靠它区分「这张票改的」与「主树后来多的」。
    # 放 worktree/ 下——整目录已 gitignore，pytest 的 testpaths 也只收 tests/。
    if [ -d "$MAIN_ROOT/tests" ]; then
      rm -rf "$MAIN_ROOT/worktree/.base-$id"
      mkdir -p "$MAIN_ROOT/worktree/.base-$id"
      cp -r "$MAIN_ROOT/tests" "$MAIN_ROOT/worktree/.base-$id/tests"
    fi

    cat <<EOF
已建工作树：$target
  分支：$branch（基线 $base）
  tests/scripts/docs：$([ ${#missing[@]} -eq 0 ] && echo "已复制" || echo "主树缺 ${missing[*]}（本仓只发布 src，开发机需自备）")
  tests 基线快照：$MAIN_ROOT/worktree/.base-$id/tests（remove 时自动 harvest）

测试命令（在任意目录执行）：
  cd "$target"
  PYTHONPATH="\$(pwd -W)/src" "$PY" -m pytest -q tests/...
判据：python -c "import minimax_h3_prompt; print(minimax_h3_prompt.__file__)" 必须指向该工作树的 src。
EOF
    ;;
  remove)
    id=${2:-}
    [ -n "$id" ] || usage
    force=0
    case "${3:-}" in
      "") ;;
      --force) force=1 ;;
      *) usage ;;
    esac
    target="$MAIN_ROOT/worktree/ticket-$id"
    [ -d "$target" ] || { echo "[错误] 没有这个工作树：$target" >&2; exit 1; }
    branch=$(git -C "$target" branch --show-current || true)
    if [ -z "$branch" ]; then
      echo "[错误] 工作树不在分支上（detached HEAD）：$target" >&2
      exit 1
    fi

    # 先收割测试改动，再同步主树最新副本：tests/ 不入库，工作树里的用例改动不随
    # git merge 回来。并行票据期间主树本来就会多出别的票的用例，所以刷新副本是
    # 必须的——但只在「收割确认没有未回灌改动」之后才做，否则会悄悄丢用例。
    if [ "$force" -eq 0 ] && [ -d "$MAIN_ROOT/worktree/.base-$id/tests" ]; then
      harvest "$id" || {
        echo "[拒绝] 测试收割有冲突，先人工合并再删树（确认丢弃才用 --force）。" >&2
        exit 1
      }
      rm -rf "$target/tests"
      cp -r "$MAIN_ROOT/tests" "$target/tests"
    fi

    # 跟踪文件（含未跟踪但未忽略的）有改动：默认拒绝。
    dirty=$(git -C "$target" status --porcelain)
    if [ -n "$dirty" ]; then
      echo "[拒绝] $target 有未提交改动：" >&2
      printf '%s\n' "$dirty" >&2
      if [ "$force" -eq 0 ]; then
        echo "  先提交/同步；确认要丢弃时再显式加 --force。" >&2
        exit 1
      fi
      echo "[警告] --force：这些改动会被丢弃。" >&2
    fi

    # 复制进去的 tests/ 副本若与主树有差异，说明有测试改动没同步回主树。
    if [ -d "$MAIN_ROOT/tests" ] && [ -d "$target/tests" ]; then
      test_diff=$(diff -rq --exclude=__pycache__ --exclude=.pytest_cache \
        "$MAIN_ROOT/tests" "$target/tests" || true)
      if [ -n "$test_diff" ]; then
        echo "[拒绝] 工作树的 tests/ 副本与主树有差异（先同步回主树）：" >&2
        printf '%s\n' "$test_diff" >&2
        if [ "$force" -eq 0 ]; then
          echo "  同步后重试；确认要丢弃测试改动时再显式加 --force。" >&2
          exit 1
        fi
        echo "[警告] --force：测试副本的差异会被丢弃。" >&2
      fi
    fi

    # 复制进去的 tests/scripts 是未跟踪副本；先删掉，git worktree remove 才会接受。
    rm -rf "$target/tests" "$target/scripts"
    # 此时跟踪文件已干净、测试副本已同步（或用户明确 --force）：允许清掉忽略文件。
    git -C "$MAIN_ROOT" worktree remove --force "$target"
    rm -rf "$MAIN_ROOT/worktree/.base-$id"
    echo "已删除工作树：$target（分支 $branch 保留；按仓规合回 main 后再删分支）"
    ;;
  harvest)
    id=${2:-}
    [ -n "$id" ] || usage
    target="$MAIN_ROOT/worktree/ticket-$id"
    [ -d "$target" ] || { echo "[错误] 没有这个工作树：$target" >&2; exit 1; }
    harvest "$id"
    ;;
  *) usage ;;
esac
