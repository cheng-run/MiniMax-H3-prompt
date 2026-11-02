#!/usr/bin/env bash
# 新本机装回守卫（retro 2026-10-31 候选 7）。
#
# 为什么需要：`.git/hooks/` 是每台机器自己的目录、不随 clone 走；tests/ 与 scripts/
# 自 2026-11-02 起已入库（体检 P0-1），干净检出即有。这个脚本只负责把仓里
# `scripts/hooks/` 的两个钩子装进 `.git/hooks/`。它不下载任何东西。
set -euo pipefail

cd "$(dirname "$0")/.."

for dir in tests scripts; do
  [ -d "$dir" ] || {
    echo "[错误] 缺 $dir/：tests/ 与 scripts/ 自 2026-11-02 起随仓库发布，请确认 clone 完整。" >&2
    exit 1
  }
done

chmod +x scripts/check.sh scripts/check_commit_msg.sh scripts/worktree.sh scripts/bootstrap-dev.sh
mkdir -p .git/hooks
cp scripts/hooks/pre-commit .git/hooks/pre-commit
cp scripts/hooks/commit-msg .git/hooks/commit-msg
chmod +x .git/hooks/pre-commit .git/hooks/commit-msg

echo "已安装 pre-commit / commit-msg 钩子。"
echo "提示：ruff 未入库；需要 lint 时由你执行：uv add --dev ruff"
