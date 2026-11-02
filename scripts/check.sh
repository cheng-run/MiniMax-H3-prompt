#!/usr/bin/env bash
# 仓内统一检查入口（retro 2026-10-30 候选 1；2026-10-31 补出网守卫与 lint）。
#
# 为什么要有这个文件：本仓原先没有任何自动守卫——无 CI、无 git 钩子、无 lint/typecheck，
# 全部规矩靠人读 CODING_STANDARDS.md 加复审子代理。复审子代理不跑代码，机械错误（导入
# 形状、出网、提交信息格式）因此没有确定性拦截。
#
# 为什么分两级：全量约 1700+ 条、跑一轮以分钟计，挂在每次提交上会逼人 `--no-verify`；
# 而 `--no-verify` 一旦成为习惯，守卫就等于不存在（「永不为红＝没有」的同族）。所以
# 提交只跑秒级的结构守卫，全量留给推送前或合并前。
#
# 用法：
#   scripts/check.sh          # 快档（默认）：结构守卫 + 改动文件的 lint
#   scripts/check.sh fast
#   scripts/check.sh full     # 全量测试 + 改动文件的 lint
#   scripts/check.sh lint     # 全量 src 的 ruff（装了才跑）
set -euo pipefail

cd "$(dirname "$0")/.." || exit 1

# 解释器回落链：环境变量 → 本地 .venv → **主工作树**的 .venv → PATH 上的 python。
# 为什么要有第三档：工作树里没有 .venv，而本机 ``python`` 是 ComfyUI 的内嵌解释器
# （/d/Comfyui/python_embeded/python）。不回落主树时，工作树里的 pre-commit 会跑在
# 错的解释器上（2026-11-01 retro 实测；主树路径的取法与 worktree.sh 相同）。
MAIN_ROOT=$(git worktree list --porcelain | awk '/^worktree /{print substr($0,10); exit}')
PY=${PYTHON:-.venv/Scripts/python.exe}
if [ ! -x "$PY" ] && [ -n "$MAIN_ROOT" ]; then
  PY="$MAIN_ROOT/.venv/Scripts/python.exe"
fi
[ -x "$PY" ] || PY=python

# ruff 走 ``python -m ruff``，不用 ``command -v ruff``：本仓把 ruff 装在 .venv 里
# （[dependency-groups].dev），而 .venv/Scripts 不在 PATH 上——旧写法因此每次都打印
# 「未安装，跳过」，lint 从来没真正跑过（2026-11-01 retro 扩开后有 6 条真错误，
# 含 main.py 的 F821 未定义名）。
ruff_available() { "$PY" -m ruff --version >/dev/null 2>&1; }

# 快档只放「秒级 + 结构性强」的几条：词条形态、配置解析、直呼覆盖、出网守卫。
# 刻意不按文件大小自动挑——那会让档位随仓库演化和意图脱钩。
# test_network_guard 是守卫本身的变异检验：谁把 conftest 的 no_real_network 删了，
# 这两条会当场变红，而不是等全量里被其他红淹没（2026-10-31 实测基线因此常红）。
FAST_TESTS=(
  tests/test_context_glossary.py
  tests/test_config.py
  tests/test_direct_call_coverage.py
  tests/test_network_guard.py
)

# lint 只查**改动过的** src/*.py：ruff 未入库，且全仓历史告警会一次性刷屏；
# 改动文件的范围在提交/合并时最相关，也足以拦住新引入的机械错误。
# ruff 未安装时打印提示并放行——安装由用户本人执行（uv add --dev ruff）。
lint_changed() {
  if ! ruff_available; then
    echo "[check] lint: ruff 未安装，跳过（安装由用户执行：uv add --dev ruff）"
    return 0
  fi
  local files
  files=$(git diff --name-only --diff-filter=ACMR HEAD -- 'src' | grep -E '\.py$' || true)
  if [ -z "$files" ]; then
    echo "[check] lint: 没有改动的 src/*.py，跳过"
    return 0
  fi
  echo "[check] lint: ruff check（改动文件）"
  # shellcheck disable=SC2086 - 文件名按行拆分是刻意的（路径不含空格是本仓约定）
  "$PY" -m ruff check $files
}

# f-string 冒充 docstring：ruff 的 F 规则不抓（2026-11-01 实测 All checks passed），
# 而全量测试也照绿——``__doc__`` 变 None 没有任何现有守卫会报。见脚本自己的 docstring。
run_docstring_guard() {
  echo "[check] 结构守卫：文档字符串不许写成 f-string"
  "$PY" scripts/check_docstring_shape.py src
}

run_fast() {
  echo "[check] fast: ${#FAST_TESTS[@]} 个结构守卫文件"
  "$PY" -m pytest -q "${FAST_TESTS[@]}"
  run_docstring_guard
  lint_changed
}

case "${1:-fast}" in
  fast)
    run_fast
    ;;
  full)
    echo "[check] full: 全量测试"
    "$PY" -m pytest -q
    run_docstring_guard
    lint_changed
    ;;
  lint)
    if ! ruff_available; then
      echo "[check] lint: ruff 未安装，跳过（安装由用户执行：uv add --dev ruff）"
      exit 0
    fi
    echo "[check] lint: ruff check src"
    "$PY" -m ruff check src
    ;;
  *)
    echo "用法: $0 [fast|full|lint]" >&2
    exit 2
    ;;
esac
