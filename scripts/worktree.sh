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
  scripts/worktree.sh link-frontend NN
      给工作树建 frontend/node_modules 的**目录联接**（Windows junction），
      指向主树依赖。只能用配套的 unlink-frontend 摘掉——rm -rf 联接会穿透
      清空主树依赖（2026-11-10 实测）。
  scripts/worktree.sh unlink-frontend NN
      删除工作树的 frontend/node_modules 目录联接（PowerShell Directory.Delete，
      只删联接、不动主树）。
  scripts/worktree.sh link-venv NN
      给工作树建 .venv 的**目录联接**，指向主树 .venv（跑 Playwright E2E 时
      serve-e2e.mjs 找的是工作树根的 .venv）。同样只能用 unlink-venv 摘掉。
  scripts/worktree.sh unlink-venv NN
      删除工作树的 .venv 目录联接（只删联接、不动主树 .venv）。
  scripts/worktree.sh remove NN [--force] [--branch]
      默认先自动 harvest，再同步主树最新 tests 副本；删前检查跟踪文件脏改动，
      并先摘掉 frontend/node_modules 与 .venv 两处目录联接。
      任一项不满足就拒绝；--force 会打印警告后跳过。
      --branch 在删树成功后一并删除分支 ticket/NN（内容应已在 main 上）。
  scripts/worktree.sh remove-path <绝对路径>
      删除**自建**工作树（integration-* 等非 ticket-NN 命名）。同样先摘
      frontend/node_modules 与 .venv 联接、并拒绝顶层还有其它联接的树。
      永远别绕过本脚本直接 `git worktree remove`（2026-11-12 实测会把主树
      frontend/node_modules 删空）。
USAGE
  exit 2
}

# 目录联接（Windows junction）：给工作树借用主树的前端依赖。创建与删除都必须走这里：
# rm -rf 联接会穿透删到主树（2026-11-10 实测把主树 node_modules 整份清空），而 Git
# Bash 的 rmdir 对 junction 报 "Not a directory"。PowerShell 的两个方向都稳；
# Directory.Delete 对非空真实目录会抛错，正好挡住「误删真实依赖」。
_link_dir() {
  local dest source
  dest=$(cygpath -w "$1")
  source=$(cygpath -w "$2")
  powershell.exe -NoProfile -Command \
    "New-Item -ItemType Junction -Path '$dest' -Target '$source' | Out-Null"
}

_unlink_dir() {
  local path
  path=$(cygpath -w "$1")
  powershell.exe -NoProfile -Command \
    "[System.IO.Directory]::Delete('$path')"
}

# 删树前摘掉某处目录联接。不是联接（非空真实目录）时 Directory.Delete 抛错 → 拒绝。
# 2026-11-12 实测：.venv 也可能是指向主树的联接（跑 E2E 时子代理临时建的），
# 只摘 node_modules 不够——`git worktree remove` 同样会穿透 .venv 联接。
_unlink_junction_or_refuse() {
  local path=$1 label=$2
  [ -e "$path" ] || return 0
  if ! _unlink_dir "$path" 2>/dev/null; then
    echo "[拒绝] $path 不是空的目录联接（可能是真实依赖）。" >&2
    echo "  人工确认并删除它后再删树；不要用 rm -rf 删联接。" >&2
    return 1
  fi
  echo "  已摘除联接：$label"
  return 0
}

# 顶层还有别的联接就成了未知风险：`git worktree remove` 会顺着它删进目标。
# 只扫顶层（不递归，避免 Get-ChildItem 跟着联接走），发现即拒绝。
_refuse_top_level_junctions() {
  local target=$1 found
  found=$(powershell.exe -NoProfile -Command \
    "Get-ChildItem -LiteralPath '$(cygpath -w "$target")' -Force -ErrorAction SilentlyContinue | Where-Object { \$_.Attributes -band [IO.FileAttributes]::ReparsePoint } | Select-Object -ExpandProperty Name" \
    2>/dev/null | tr -d '\r')
  [ -z "$found" ] && return 0
  echo "[拒绝] 工作树顶层还有目录联接，删除会穿透到目标：" >&2
  printf '  %s\n' $found >&2
  echo "  先人工确认并在目标侧摘掉这些联接（PowerShell Directory.Delete），再删树。" >&2
  return 1
}

# 两处已知依赖联接（frontend/node_modules、.venv）：先摘，摘不掉就拒绝。
_unlink_worktree_deps() {
  local target=$1 force=${2:-0} dep
  for dep in "frontend/node_modules" ".venv"; do
    if ! _unlink_junction_or_refuse "$target/$dep" "$dep"; then
      if [ "$force" -eq 0 ]; then
        return 1
      fi
      echo "[警告] --force：继续删树，$dep 交给 worktree remove。" >&2
    fi
  done
  return 0
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
    # 补进来的未跟踪文件要记一笔：它们是脚本带进工作树的（当天调研笔记等），
    # remove 时若仍与主树逐字节相同就清掉，否则脏检查会拒绝删除脚本自己的副本
    # （2026-11-04 实测：add 带入未跟踪问卷，remove 被自己的副本拦下）。
    backfilled="$MAIN_ROOT/worktree/.backfilled-$id"
    : > "$backfilled"
    # docs/ 也复制：规格与设计文档住在 docs/design/，不复制的话，工作树里的实现
    # 代理看不到票面指向的规格（2026-11-01 retro：协调会话只能给每个代理手写主树
    # 绝对路径）。
    #
    # 2026-11-02 起 tests/scripts/docs 已入库，git 检出时这些目录**已经存在**；
    # 此时再 `cp -r 主树/docs 目标/docs` 会复制成 `目标/docs/docs`（#159 实测：
    # docs/docs/ 让 test_doc_pointer_shape 报一堆假红、pytest 重复收测）。所以
    # 目录已存在时只补主树里**未跟踪**的文件（例如当天的调研笔记），已跟踪文件
    # 以分支检出为准。
    for dir in tests scripts docs; do
      if [ ! -d "$MAIN_ROOT/$dir" ]; then
        missing+=("$dir")
        continue
      fi
      if [ -d "$target/$dir" ]; then
        while IFS= read -r -d '' rel; do
          mkdir -p "$target/$(dirname "$rel")"
          cp -p "$MAIN_ROOT/$rel" "$target/$rel"
          printf '%s\n' "$rel" >> "$backfilled"
        done < <(git -C "$MAIN_ROOT" ls-files --others --exclude-standard -z -- "$dir")
      else
        cp -r "$MAIN_ROOT/$dir" "$target/$dir"
      fi
    done

    # tests 基线快照只在 tests 仍是未跟踪副本时需要（harvest 靠它区分「这张票改的」
    # 与「主树后来多的」）。2026-11-02 起已入库，git 自己管，快照只会占盘。
    base_snapshot=""
    if [ -d "$MAIN_ROOT/tests" ] \
        && [ -z "$(git -C "$MAIN_ROOT" ls-files -- tests | head -1)" ]; then
      rm -rf "$MAIN_ROOT/worktree/.base-$id"
      mkdir -p "$MAIN_ROOT/worktree/.base-$id"
      cp -r "$MAIN_ROOT/tests" "$MAIN_ROOT/worktree/.base-$id/tests"
      base_snapshot="$MAIN_ROOT/worktree/.base-$id/tests"
    fi

    cat <<EOF
已建工作树：$target
  分支：$branch（基线 $base）
  tests/scripts/docs：$([ ${#missing[@]} -eq 0 ] && echo "已复制" || echo "主树缺 ${missing[*]}（本仓只发布 src，开发机需自备）")
  tests 基线快照：${base_snapshot:-tests 已入库，跳过（git 管测试改动）}

测试命令（在任意目录执行）：
  cd "$target"
  PYTHONPATH="\$(pwd -W)/src" "$PY" -m pytest -q tests/...
判据：python -c "import minimax_h3_prompt; print(minimax_h3_prompt.__file__)" 必须指向该工作树的 src。
需要跑前端契约生成/守卫时：scripts/worktree.sh link-frontend $id（收工用 unlink-frontend 摘掉，别 rm -rf）。
需要跑 Playwright E2E 时：scripts/worktree.sh link-venv $id（收工用 unlink-venv 摘掉，别 rm -rf）。
EOF
    ;;
  link-frontend)
    # 工作树没有 node_modules，而契约生成/前端守卫要它。用目录联接而不是复制：
    # 复制一份依赖既慢又会让 pnpm 的两份状态各说各话。
    id=${2:-}
    [ -n "$id" ] || usage
    target="$MAIN_ROOT/worktree/ticket-$id"
    source="$MAIN_ROOT/frontend/node_modules"
    dest="$target/frontend/node_modules"
    [ -d "$target/frontend" ] || { echo "[错误] 工作树里没有 frontend/：$target" >&2; exit 1; }
    [ -e "$source/.bin/eslint" ] || {
      echo "[错误] 主树前端依赖未安装或不完整：$source" >&2
      echo "  先执行：cd frontend && npx pnpm@11.7.0 install（下载由用户执行）" >&2
      exit 1
    }
    [ -e "$dest" ] && { echo "[错误] 工作树的 frontend/node_modules 已存在：$dest" >&2; exit 1; }
    command -v powershell.exe >/dev/null 2>&1 || {
      echo "[错误] link-frontend 需要 Windows PowerShell 建目录联接。" >&2
      exit 1
    }
    _link_dir "$dest" "$source"
    echo "已建立目录联接：$dest -> $source"
    echo "  收工前用 scripts/worktree.sh unlink-frontend $id 摘掉；不要 rm -rf 联接。"
    ;;
  unlink-frontend)
    id=${2:-}
    [ -n "$id" ] || usage
    dest="$MAIN_ROOT/worktree/ticket-$id/frontend/node_modules"
    [ -e "$dest" ] || { echo "[跳过] 没有前端依赖联接：$dest"; exit 0; }
    # 只摘联接：Directory.Delete 对非空真实目录会抛错，正好把「误删真实依赖」挡下来
    # （2026-11-10 实测：rm -rf 联接会清空主树 node_modules）。
    if ! _unlink_dir "$dest" 2>/dev/null; then
      echo "[拒绝] $dest 不是空的目录联接（可能是真实依赖目录，或仍有文件）。" >&2
      echo "  人工确认后再处理；不要用 rm -rf 删联接。" >&2
      exit 1
    fi
    echo "已摘除目录联接：$dest（主树依赖未动）"
    ;;
  link-venv)
    # 工作树没有 .venv，而 frontend/scripts/serve-e2e.mjs 按工作树根找 .venv。
    # 与 link-frontend 同理：联接而不是复制，删树前必须用 unlink-venv 摘掉。
    id=${2:-}
    [ -n "$id" ] || usage
    target="$MAIN_ROOT/worktree/ticket-$id"
    source="$MAIN_ROOT/.venv"
    dest="$target/.venv"
    [ -d "$target" ] || { echo "[错误] 没有这个工作树：$target" >&2; exit 1; }
    [ -x "$PY" ] || {
      echo "[错误] 主树 Python 环境未安装：$source" >&2
      echo "  先执行：uv sync（下载由用户执行）" >&2
      exit 1
    }
    [ -e "$dest" ] && { echo "[错误] 工作树的 .venv 已存在：$dest" >&2; exit 1; }
    command -v powershell.exe >/dev/null 2>&1 || {
      echo "[错误] link-venv 需要 Windows PowerShell 建目录联接。" >&2
      exit 1
    }
    _link_dir "$dest" "$source"
    echo "已建立目录联接：$dest -> $source"
    echo "  收工前用 scripts/worktree.sh unlink-venv $id 摘掉；不要 rm -rf 联接。"
    ;;
  unlink-venv)
    id=${2:-}
    [ -n "$id" ] || usage
    dest="$MAIN_ROOT/worktree/ticket-$id/.venv"
    [ -e "$dest" ] || { echo "[跳过] 没有 .venv 联接：$dest"; exit 0; }
    if ! _unlink_dir "$dest" 2>/dev/null; then
      echo "[拒绝] $dest 不是空的目录联接（可能是真实环境目录，或仍有文件）。" >&2
      echo "  人工确认后再处理；不要用 rm -rf 删联接。" >&2
      exit 1
    fi
    echo "已摘除目录联接：$dest（主树 .venv 未动）"
    ;;
  remove-path)
    # 自建工作树（例如 worktree/integration-*）不走 ticket-NN 命名，但删除风险一样：
    # 必须先摘目录联接。2026-11-12 实测：绕过本脚本直接 `git worktree remove`，
    # 顺着 frontend/node_modules 联接把主树依赖删空。
    target=${2:-}
    [ -n "$target" ] || usage
    case "$target" in
      /*|[A-Za-z]:[\\/]*) ;;
      *) echo "[错误] remove-path 要绝对路径：$target" >&2; exit 2 ;;
    esac
    [ -d "$target" ] || { echo "[错误] 没有这个目录：$target" >&2; exit 1; }
    _unlink_worktree_deps "$target" 0 || exit 1
    _refuse_top_level_junctions "$target" || exit 1
    git -C "$MAIN_ROOT" worktree remove --force "$target"
    echo "已删除工作树：$target"
    ;;
  remove)
    id=${2:-}
    [ -n "$id" ] || usage
    force=0
    delete_branch=0
    for arg in "${@:3}"; do
      case "$arg" in
        --force) force=1 ;;
        --branch) delete_branch=1 ;;
        *) usage ;;
      esac
    done
    target="$MAIN_ROOT/worktree/ticket-$id"
    [ -d "$target" ] || { echo "[错误] 没有这个工作树：$target" >&2; exit 1; }
    branch=$(git -C "$target" branch --show-current || true)
    if [ -z "$branch" ]; then
      echo "[错误] 工作树不在分支上（detached HEAD）：$target" >&2
      exit 1
    fi

    # 2026-11-02 起 tests/ 已入库：git 自己管它，harvest 与「刷新主树副本」都不再
    # 需要。刷新反而会把工作树弄脏——主树带着别的票的测试改动时，刷新后的副本与
    # 本票分支不一致，紧接着的脏检查会拒绝删除（#159 实测：remove 被自己的刷新拦住）。
    tests_tracked=0
    [ -n "$(git -C "$target" ls-files -- tests | head -1)" ] && tests_tracked=1

    # 只有 tests/ 仍是「不入库的副本」时才需要收割与刷新（旧口径）。
    if [ "$force" -eq 0 ] && [ "$tests_tracked" -eq 0 ] \
        && [ -d "$MAIN_ROOT/worktree/.base-$id/tests" ]; then
      harvest "$id" || {
        echo "[拒绝] 测试收割有冲突，先人工合并再删树（确认丢弃才用 --force）。" >&2
        exit 1
      }
      rm -rf "$target/tests"
      cp -r "$MAIN_ROOT/tests" "$target/tests"
    fi

    # add 时补进工作树的未跟踪文件（当天调研笔记等）由脚本带入，不是用户的未提交
    # 改动。删之前只清掉与主树仍逐字节相同的副本；用户改过的留下，让脏检查拒绝。
    backfilled="$MAIN_ROOT/worktree/.backfilled-$id"
    if [ -f "$backfilled" ]; then
      while IFS= read -r rel; do
        [ -n "$rel" ] || continue
        if [ -f "$target/$rel" ] && [ -f "$MAIN_ROOT/$rel" ] \
            && cmp -s "$target/$rel" "$MAIN_ROOT/$rel"; then
          rm -f "$target/$rel"
        fi
      done < "$backfilled"
      rm -f "$backfilled"
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
    # 入库后这条判据失效（分支本来就可能落后主树），由上面的脏检查兜住。
    if [ "$tests_tracked" -eq 0 ] \
        && [ -d "$MAIN_ROOT/tests" ] && [ -d "$target/tests" ]; then
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
    # 已入库时它们受 git 管，删掉反而制造脏状态，交给 worktree remove 处理。
    if [ "$tests_tracked" -eq 0 ]; then
      rm -rf "$target/tests" "$target/scripts"
    fi
    # 目录联接必须先摘掉：`git worktree remove` 与 rm -rf 都可能穿透联接删到主树
    # 依赖（2026-11-10 实测主树 frontend/node_modules 被整份清空）。.venv 同样
    # 可能是指向主树的联接（2026-11-12 实测），一并处理。
    _unlink_worktree_deps "$target" "$force" || exit 1
    if ! _refuse_top_level_junctions "$target"; then
      if [ "$force" -eq 0 ]; then
        exit 1
      fi
      echo "[警告] --force：顶层联接仍在，继续删树。" >&2
    fi
    # 此时跟踪文件已干净、测试副本已同步（或用户明确 --force）：允许清掉忽略文件。
    git -C "$MAIN_ROOT" worktree remove --force "$target"
    rm -rf "$MAIN_ROOT/worktree/.base-$id"
    if [ "$delete_branch" -eq 1 ]; then
      # 票分支多半是 squash 合入的，不是 main 的祖先：用 -D。内容已在 main 上
      # （拆票提交），这里删的只是名字——留着会在 `git branch` 里堆成一片。
      if [ "$branch" = "main" ]; then
        echo "[错误] 拒绝删除 main 分支。" >&2
        exit 1
      fi
      git -C "$MAIN_ROOT" branch -D "$branch"
      echo "已删除工作树与分支：$target / $branch"
    else
      echo "已删除工作树：$target（分支 $branch 保留；要一并删分支加 --branch）"
    fi
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
