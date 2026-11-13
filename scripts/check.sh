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
#   scripts/check.sh full     # 全量测试 + 前端/契约守卫 + 变异检验（交验收、推送前跑）
#   scripts/check.sh lint     # 全量 src 的 ruff（装了才跑）
#   scripts/check.sh mutate   # 守卫变异检验（不需要额外依赖）
#   scripts/check.sh types    # mypy（装了才跑；先只查叶子模块）
#   scripts/check.sh coverage # 分支覆盖度量（装了才跑；不设门槛）
#   scripts/check.sh clean    # 干净环境（无 .env）跑全量，模拟 CI
#   scripts/check.sh worktree # 工作树生命周期冒烟（add/remove）
#   scripts/check.sh rehash   # 打印关键帧角色提示词的 sha256，供更新基线
set -Eeuo pipefail

cd "$(dirname "$0")/.." || exit 1

# Windows 控制台默认 cp936：pytest/mypy 的中文输出会变乱码，失败时连断言里的
# 中文片段都读不出来（2026-11-10 实测）。CI 已设同一个变量。
export PYTHONIOENCODING="${PYTHONIOENCODING:-utf-8}"

# 本树的 src 必须排在 PYTHONPATH 最前。工作树里没有自己的 .venv，解释器回落到**主树**
# 的 .venv，而那是 editable 安装（指向主树）——不显式指定，就会出现「变异打在 A 树、
# 用例读的是 B 树」，绿得毫无意义（2026-11-08 #199 实测：mutation_check 因此把两条
# 真守卫报成「摆设」）。主树里这行是恒等变换。
SRC_DIR="$(pwd -W 2>/dev/null || pwd)/src"
export PYTHONPATH="$SRC_DIR${PYTHONPATH:+:$PYTHONPATH}"

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

# 超时保护：pyproject 的 ``timeout = 300`` 只在 pytest-timeout 装着时生效，插件不在时
# 那条配置静默无效——「规则在、接线不在」的老形态。装没装都照跑，但要说一声：
# 2026-11-08 实测一条卡死的全量吃到 24 GB 工作集 / 95 GB 私有提交，机器只剩 1.4 GB。
report_timeout_guard() {
  if "$PY" -c "import pytest_timeout" >/dev/null 2>&1; then
    return 0
  fi
  echo "[check] timeout: pytest-timeout 未安装，本次全量没有单条用例超时保护" \
       "（安装由用户执行：uv add --dev pytest-timeout）"
}

# 快档只放「秒级 + 结构性强」的几条：词条形态、配置解析、直呼覆盖、出网守卫。
# 刻意不按文件大小自动挑——那会让档位随仓库演化和意图脱钩。
# test_network_guard 是守卫本身的变异检验：谁把 conftest 的 no_real_network 删了，
# 这两条会当场变红，而不是等全量里被其他红淹没（2026-10-31 实测基线因此常红）。
FAST_TESTS=(
  tests/test_context_glossary.py
  tests/test_config.py
  tests/test_direct_call_coverage.py
  tests/test_network_guard.py
  tests/test_mutation_anchors.py
  tests/test_script_line_endings.py
  tests/test_adr_guard.py
  tests/test_doc_pointer_shape.py
  tests/test_spike_exe.py
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
  # tests/ 也查：2026-11-02 扩到全仓时，tests 里还躺着 34 条（含一条 F821
  # 「赋值前引用」）。只查 src 等于把一半代码排除在机械检查之外。
  # scripts/ 也查（retro 2026-11-10）：#226 里 accept_d3.py 带着 F541 进了提交，
  # 因为验收驱动器不在任何 lint 范围；只查改动文件，历史告警（accept_140、
  # measure_seam_motion 各一条）不会一次性刷屏。
  # 未跟踪文件也算「改动」（retro 2026-11-13）：`git diff HEAD` 看不见新建文件，
  # 于是每一个新加的 .py 都从 lint 与 mypy 底下溜过去，直到提交之后才第一次被看见
  # （实测：新建的 scripts/start_web.py 在盘上，check.sh fast 打印「没有改动」）。
  files=$({ git diff --name-only --diff-filter=ACMR HEAD -- 'src' 'tests' 'scripts'
            git ls-files --others --exclude-standard -- 'src' 'tests' 'scripts'; } \
          | grep -E '\.py$' | sort -u || true)
  if [ -z "$files" ]; then
    echo "[check] lint: 没有改动的 src/tests/scripts 下的 .py，跳过"
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

# mypy：2026-11-02 起覆盖全仓（72 个文件零错误）。未安装时提示并放行——安装由
# 用户本人执行（全局规则：下载一律由用户做）。**full 档必须跑它**：类型守卫不进
# full 就等于没有（CI 也一样，见 .github/workflows/ci.yml）。
run_types() {
  if ! "$PY" -m mypy --version >/dev/null 2>&1; then
    echo "[check] types: mypy 未安装，跳过（安装由用户执行：uv add --dev mypy）"
    return 0
  fi
  echo "[check] types: mypy（全仓）"
  "$PY" -m mypy
  # scripts/ 走「改动的文件」（retro 2026-11-10）：验收驱动器原先完全在 mypy 之外，
  # #226 里 accept_d3.py 的 arg-type 错误只能靠复审手跑才发现。历史脚本还有存量
  # 错误，所以只查改动过的，不把整目录并进配置。
  local script_files
  # 同样要带上未跟踪文件（retro 2026-11-13）：新建的脚本在提交前也得过一遍类型。
  script_files=$({ git diff --name-only --diff-filter=ACMR HEAD -- 'scripts'
                   git ls-files --others --exclude-standard -- 'scripts'; } \
                 | grep -E '\.py$' | sort -u || true)
  if [ -n "$script_files" ]; then
    echo "[check] types: mypy（改动的 scripts）"
    # shellcheck disable=SC2086 - 文件名按行拆分是刻意的（路径不含空格是本仓约定）
    "$PY" -m mypy $script_files
  fi
}

# 类型守卫进快档（retro 2026-11-13）：#252 七张票逐票 `fast` 全绿，拆回 main 跑
# `full` 才炸出三处 `ActionItemOut | None`——类型错误在实现切片里本可以当场红。
# mypy 全仓热缓存约 2.5s，只在 src 有改动时跑，其余提交不被拖慢。
run_types_if_src_changed() {
  local files
  files=$({ git diff --name-only --diff-filter=ACMR HEAD -- 'src'
            git ls-files --others --exclude-standard -- 'src'; } \
          | grep -E '\.py$' | sort -u || true)
  [ -n "$files" ] || return 0
  run_types
}

# 前端守卫（retro 2026-11-09）：前端原先只在 CI 有 lint/typecheck，本地提交坏 TS 不会红。
# 装了依赖才跑（未装像 ruff 一样提示跳过）；fast 档只在「前端有改动」时触发，其余提交
# 不被拖慢。用 frontend/node_modules/.bin 直调，绕开 npx pnpm 解析错版本（实测会去拉 12.x）。
#
# Prettier --check 是 2026-11-10 #225 retro 补的：CI 的 frontend-quality 一直有
# `format:check`，本地没有——main 因此红了好几次而没人看见。它与 eslint 不同：
# 只判格式，秒级，所以放在同一档。
frontend_ready() {
  local frontend=$1
  # 判据是**可执行入口**而不是目录存在：目录联接被误删后会留下一个空的
  # `node_modules`，`-d` 仍为真，守卫会跑进 `./node_modules/.bin/eslint` 并报
  # "No such file or directory"——把「依赖没装」误报成代码坏了（2026-11-10 实测）。
  [ -e "$frontend/node_modules/.bin/eslint" ] \
      && [ -e "$frontend/node_modules/.bin/vue-tsc" ] \
      && [ -e "$frontend/node_modules/.bin/prettier" ] \
      && [ -e "$frontend/node_modules/.bin/tsc" ]
}

run_frontend_guard() {
  local force=${1:-false}
  local root frontend
  root=$(git rev-parse --show-toplevel)
  frontend="$root/frontend"
  if ! frontend_ready "$frontend"; then
    echo "[check] frontend: 跳过（前端依赖未安装或不完整：cd frontend && npx pnpm@11.7.0 install）"
    return 0
  fi
  if [ "$force" != true ] && [ -z "$(git status --porcelain -- frontend)" ]; then
    echo "[check] frontend: 没有前端改动，跳过"
    return 0
  fi
  echo "[check] frontend: eslint + prettier --check + vue-tsc + e2e 类型"
  ( cd "$frontend" && ./node_modules/.bin/eslint . )
  # 与 CI 的 `pnpm run format:check` 同一条命令（`.prettierignore` 已排除 generated.ts
  # 与构建产物）。
  ( cd "$frontend" && ./node_modules/.bin/prettier --check . )
  ( cd "$frontend" && ./node_modules/.bin/vue-tsc --noEmit )
  # e2e 不在主 tsconfig 的 include 里（配置/规格文件的类型由 Playwright 转译，不做
  # 类型检查）：retro 2026-11-10 补独立 tsconfig.e2e.json，别让 e2e 成为类型盲区。
  ( cd "$frontend" && ./node_modules/.bin/tsc --noEmit -p tsconfig.e2e.json )
}

# 契约守卫（retro 2026-11-10）：OpenAPI 与前端生成类型的 drift 原先只在 CI 有，
# D2 验收时还要手跑两条命令才拿得到 AC14 的证据。这里把同一套判据搬进本地：
# 导出/生成必须与**工作区里的文件**逐字节一致；装了前端依赖再生成 TS 并比。
# 生成失败/漂移都算红——契约是类型链的源头，看着绿而实际分叉比直接报错更贵。
#
# 为什么比工作区文件、不比 HEAD（2026-11-10 #223 实测）：开发期契约改动还没提交时，
# `git diff --exit-code` 必然红，把「未提交」误报成「漂移」，白跑一轮全量。
# 「提交后的文件是否与代码一致」由 CI 的 git diff --exit-code 负责（ci.yml 已有）。
run_contract_guard() {
  local root frontend git_dir exported generated
  root=$(git rev-parse --show-toplevel)
  frontend="$root/frontend"
  # 临时文件放 git 目录：不在工作区，不会被别的守卫或 git status 看见。
  # 取 ``--absolute-git-dir`` 而不是 ``$root/.git``：工作树里的 ``.git`` 是
  # **一个文件**（指向主仓 gitdir 的链接），在它下面 mkdir 会直接 FileExistsError。
  # 也不能用 ``--git-dir``：主树里它返回相对的 ``.git``，而下面两个消费者
  # （python 导出 / frontend 下的 node 生成器）工作目录不同，相对路径会各自解析到
  # 错误位置（2026-11-11 #228 复审实测：主树契约守卫报 frontend/.git/... 不存在）。
  git_dir=$(git rev-parse --absolute-git-dir)
  exported="$git_dir/contract-check.$$.json"
  generated="$git_dir/generated-check.$$.ts"
  echo "[check] contracts: 重新导出 OpenAPI 并与工作区文件比对"
  "$PY" "$root/scripts/export_openapi.py" --out "$exported"
  if ! cmp -s "$exported" "$root/contracts/openapi.json"; then
    echo "[check] contracts: 导出结果与 contracts/openapi.json 不一致（改了契约没重导）：" >&2
    diff -u "$root/contracts/openapi.json" "$exported" | head -40 >&2 || true
    rm -f "$exported"
    return 1
  fi
  rm -f "$exported"
  if ! command -v node >/dev/null 2>&1 \
      || [ ! -e "$frontend/node_modules/openapi-typescript" ] \
      || ! frontend_ready "$frontend"; then
    echo "[check] contracts: 跳过 TS 生成（node 或前端依赖未就绪）"
    return 0
  fi
  ( cd "$frontend" && node scripts/generate-api.mjs "$generated" >/dev/null )
  if ! cmp -s "$generated" "$frontend/src/api/generated.ts"; then
    echo "[check] contracts: frontend/src/api/generated.ts 与契约不一致（重新生成后有差异）：" >&2
    diff -u "$frontend/src/api/generated.ts" "$generated" | head -40 >&2 || true
    rm -f "$generated"
    return 1
  fi
  rm -f "$generated"
}

# 守卫变异检验（2026-11-10 #225 retro）：CI 的 check job 一直跑它，本地 `full` 没跑。
# 后果实测：main 上 `cancel_ignored_at_node_boundary` 的锚点从 #219 起出现 3 次，
# 变异静默失效、CI 连红数次，本地毫无感知。它慢（约 2 分钟），所以只进 full。
run_mutations() {
  echo "[check] mutate: 守卫变异检验"
  # ``-u``：重定向到文件时 Python 默认块缓冲，等待期只能看到「开始」一行（2026-11-13
  # 实测：用户问「为什么 10 分钟」时没有任何进度可读）。脚本自己逐条打印耗时。
  "$PY" -u scripts/mutation_check.py
}

run_fast() {
  echo "[check] fast: ${#FAST_TESTS[@]} 个结构守卫文件"
  report_timeout_guard
  "$PY" -m pytest -q "${FAST_TESTS[@]}"
  run_docstring_guard
  lint_changed
  run_types_if_src_changed
  run_frontend_guard false
  # 只有改了契约相关路径才跑：每次提交都导出会把快档拖慢，而契约漂移只可能由
  # src/契约/生成链的改动引入（整个 src 都算：路由的 schema 也可能来自 store 模型）。
  if [ -n "$(git status --porcelain -- src/minimax_h3_prompt contracts frontend/src/api frontend/scripts 2>/dev/null)" ]; then
    run_contract_guard
  fi
}

# 基线哈希重算（retro 2026-11-03）：`tests/test_base_prompt_bytes.py` 的常量在并行
# 分支上会各改各的，合并时必冲突，冲突后方要手跑一段带导入路径的 python 才能拿到
# 合并后的真实值。一个子命令把这段查表变成一条命令——基线仍要人显式更新。
run_rehash() {
  local role=${1:-frame_prompt_engineer}
  local src_dir
  src_dir="$(pwd -W 2>/dev/null || pwd)/src"
  echo "[check] rehash: $role 的 sha256（更新基线用）"
  PYTHONPATH="$src_dir${PYTHONPATH:+:$PYTHONPATH}" "$PY" -c \
    'import hashlib, sys; from minimax_h3_prompt.agents import load_role_prompt; print(hashlib.sha256(load_role_prompt(sys.argv[1]).encode("utf-8")).hexdigest())' \
    "$role"
}

# 唯一的结论行：`| tail -N` 这类管道会把退出码吞掉，曾经把红的检查显示成绿
# （2026-11-01 漏看一次 collection error，AGENTS.md 因此留了一段 PIPESTATUS 咒语）。
# 结论行让「最后一行」自己就是判据，不用再让每个人记那串咒语。
MODE="${1:-fast}"
trap 'rc=$?; echo "[check] ${MODE}: FAIL（退出码 ${rc}）" >&2; exit "$rc"' ERR

case "$MODE" in
  fast)
    run_fast
    ;;
  full)
    echo "[check] full: 全量测试"
    report_timeout_guard
    "$PY" -m pytest -q
    run_docstring_guard
    lint_changed
    run_types
    # full 是交验收/推送前的最后一关：前端与契约漂移必须在这里一起看到，不能留着
    # 等 CI 才红（2026-11-10 实测：full 绿而 CI 的前端/契约 job 红）。
    run_frontend_guard true
    run_contract_guard
    # 变异放最后：它最慢，前面的红先停，不白等两分钟。
    run_mutations
    ;;
  frontend)
    run_frontend_guard true
    ;;
  lint)
    if ! ruff_available; then
      echo "[check] lint: ruff 未安装，跳过（安装由用户执行：uv add --dev ruff）"
      exit 0
    fi
    echo "[check] lint: ruff check src tests scripts"
    "$PY" -m ruff check src tests scripts
    ;;
  clean)
    # 干净环境（retro 2026-11-02）。2026-11-12 起测试**默认**就隔离本机 .env，
    # 这一档因此与 full 等价；保留它是因为推送前的习惯命令与 CI 口径都指向这里，
    # 也保留 H3_TEST_NO_ENV 这个兼容别名（现在与默认行为相同）。
    echo "[check] clean: 干净环境（无 .env / 无模型环境变量）下跑全量"
    report_timeout_guard
    H3_TEST_NO_ENV=1 "$PY" -m pytest -q
    run_docstring_guard
    run_contract_guard
    ;;
  mutate)
    # 变异检验（体检 P6）：守卫测试必须真的会咬人。不需要额外依赖。
    echo "[check] mutate: 守卫变异检验"
    "$PY" -u scripts/mutation_check.py
    ;;
  types)
    run_types
    ;;
  coverage)
    if ! "$PY" -m coverage --version >/dev/null 2>&1; then
      echo "[check] coverage: coverage 未安装，跳过（安装由用户执行：uv add --dev coverage）"
      echo "[check] ${MODE}: SKIP"
      exit 0
    fi
    echo "[check] coverage: 全量测试 + 分支覆盖（只度量，不设门槛）"
    report_timeout_guard
    "$PY" -m coverage run -m pytest -q
    "$PY" -m coverage report
    ;;
  worktree)
    # 2026-11-04 #157 实测：add 产生 docs/docs、remove 被自己的刷新拦住——脚本是
    # /implement-spec 并行的骨架，却没有会跑的用例。全量测试也会收这条（tests/）。
    echo "[check] worktree: 工作树生命周期冒烟"
    "$PY" -m pytest -q tests/test_worktree_script.py
    ;;
  rehash)
    run_rehash "${2:-frame_prompt_engineer}"
    ;;
  *)
    echo "用法: $0 [fast|full|lint|frontend|mutate|types|coverage|clean|worktree|rehash [角色名]]" >&2
    printf '[check] %s: SKIP（无此档位）\n' "$MODE" >&2
    exit 2
    ;;
esac

trap - ERR
echo "[check] ${MODE}: PASS"
