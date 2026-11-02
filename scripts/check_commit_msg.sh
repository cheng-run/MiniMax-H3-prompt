#!/usr/bin/env bash
# 提交信息格式守卫（retro 2026-10-31 候选 2）。
#
# 为什么必须有：CODING_STANDARDS.md 第七节要求 `type(scope): 摘要（issue #N）`，
# 但本次 /implement-spec 的 6 个实现提交全写成了 `#N 摘要`——机械格式靠复审子代理抓
# 既晚又不可靠（复审在合入之后才跑）。钩子只判第一行，成本是毫秒级。
#
# 允许两种可追溯后缀：
#   （#123）               issue / 票号
#   （retro 2026-10-31）  没有 issue 的 retro 类改动，用日期追溯
# Merge / Revert / fixup! / squash! 等特殊提交放行。
set -euo pipefail

msg_file=${1:?用法: check_commit_msg.sh <commit-msg-file>}
subject=$(sed -n '1p' "$msg_file" | sed 's/[[:space:]]*$//')

case "$subject" in
  Merge\ *|merge:*|Revert\ *|revert:*|fixup!*|squash!*) exit 0 ;;
esac

# 类型白名单与 CODING_STANDARDS 第七节一致；scope 允许中文（仓内已有 `fix(导演台)`），
# 所以用 `[^()]+` 而不是只认英文字母。
pattern='^(fix|feat|refactor|docs|test|chore|perf|build|ci|style)\([^()]+\): .+（(#[0-9]+|retro[ -]?[0-9]{4}-[0-9]{2}-[0-9]{2})）$'

if [[ "$subject" =~ $pattern ]]; then
  exit 0
fi

cat >&2 <<'MSG'
[commit-msg] 提交信息格式不符。请用：
    type(scope): 摘要（#N）
  例：fix(director): 陪跑改以成片视频为证据（#112）
  没有 issue 的 retro 类改动用：（retro YYYY-MM-DD）
  Merge / Revert / fixup! / squash! 自动放行。
MSG
exit 1
