#!/usr/bin/env bash
# 仓内统一检查入口（retro 2026-10-30 候选 1）。
#
# 为什么要有这个文件：本仓原先没有任何自动守卫——无 CI、无 git 钩子、无 lint/typecheck，
# 全部规矩靠人读 CODING_STANDARDS.md 加复审子代理。复审子代理不跑代码，机械错误（导入
# 形状、出网、提交信息格式）因此没有确定性拦截。
#
# 为什么分两级：全量约 1200+ 条、跑一轮以分钟计，挂在每次提交上会逼人 `--no-verify`；
# 而 `--no-verify` 一旦成为习惯，守卫就等于不存在（「永不为红＝没有」的同族）。所以
# 提交只跑秒级的结构守卫，全量留给推送前或合并前。
#
# 用法：
#   scripts/check.sh          # 快档（默认）
#   scripts/check.sh fast
#   scripts/check.sh full     # 全量
set -euo pipefail

cd "$(dirname "$0")/.." || exit 1

PY=${PYTHON:-.venv/Scripts/python.exe}
[ -x "$PY" ] || PY=python

# 快档只放「秒级 + 结构性强」的几条：词条形态、配置解析、直呼覆盖。
# 刻意不按文件大小自动挑——那会让档位随仓库演化和意图脱钩。
FAST_TESTS=(
  tests/test_context_glossary.py
  tests/test_config.py
  tests/test_direct_call_coverage.py
)

case "${1:-fast}" in
  fast)
    echo "[check] fast: ${#FAST_TESTS[@]} 个结构守卫文件"
    exec "$PY" -m pytest -q "${FAST_TESTS[@]}"
    ;;
  full)
    echo "[check] full: 全量测试"
    exec "$PY" -m pytest -q
    ;;
  *)
    echo "用法: $0 [fast|full]" >&2
    exit 2
    ;;
esac
