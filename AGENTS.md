# MiniMax-H3-prompt

生成 MiniMax H3 视频生成提示词并把长视频拆成可逐段执行的工具。
项目术语与架构见 `CONTEXT.md`；动手前先读它。
skills 文档里说的 `GLOSSARY.md` 即本仓 `CONTEXT.md`——新词条一律写进 `CONTEXT.md`。

## Agent skills

### Issue tracker

Issues 活在 GitHub Issues（github.com/cheng-run/MiniMax-H3-prompt），用 `gh` CLI 操作。见 `docs/agents/issue-tracker.md`。

### Triage labels

五档默认标签（needs-triage / needs-info / ready-for-agent / ready-for-human / wontfix），无映射。见 `docs/agents/triage-labels.md`。

### Domain docs

单上下文：根 `CONTEXT.md`（术语表）+ `docs/adr/`（架构决策）。见 `docs/agents/domain.md`。
导演台路线另有实现与插件契约：`docs/design/2026-10-29-导演台路线实现设计.md`、`docs/research/2026-10-29-director-插件原生格式.md`；2026-10-31 的陪跑证据改造（#112–#114）见同目录的 `2026-10-31-director-fl2v-video-evidence-*.md`。

## 产物落点（新产物写哪，2026-10-21 起）

| 产物 | 落点 |
|---|---|
| 术语、架构说明 | `CONTEXT.md`（根） |
| 架构决策 | `docs/adr/NNNN-slug.md`（扫最大号 +1） |
| 调研笔记 | `docs/research/YYYY-MM-DD-主题.md` |
| 设计、根因分析 | `docs/design/YYYY-MM-DD-主题.md` |
| spec、票、关票证据 | GitHub Issues（**不落仓**） |
| handoff | `docs/handoffs/YYYY-MM-DD-主题.md` |

spec 与票住 issue 是因为 `/to-spec`、`/to-tickets` 按 `docs/agents/issue-tracker.md` 发布；**handoff 覆盖该 skill 默认的系统临时目录**，改落 `docs/handoffs/`——本项目不是临时项目，文档一律留在仓里。本仓自 2026-10-17 起（#39）不再往仓里落 spec 与计划文件。

## 票据工作流（2026-10-21 修订）

- **默认不开工作树**：只有用户明确要求并行执行任务时才 `git worktree add worktree/ticket-NN`（`/worktree/` 已 gitignore）；单票照常在主树做。
- **工作树的提交不进历史**：工作树里可以提交到它自己的临时分支，但那些提交**永不 push**；合并回原分支走 `git merge --squash`，**最终提交只落在主分支上、一票一次**。
- **推送放最后，验收通过才推**：改动碰到 `src/` 或 `tests/` 时必须重跑全量；**纯文档/占位改动沿用最近一次绿的结果**，并在推送时说明依据哪一次。全量绿只是**交验收的前提**——用户说「验收通过／可以合了」之后才合 main 并 `git push`；验收前不推。全局 `~/.claude/AGENTS.md` 的验收门是唯一真源（覆盖 2026-10-21 版「绿了即自动 push」）。
- 工作树内跑测试先设 `PYTHONPATH=<工作树>\src` 再用主仓 `.venv`，否则测的是主树（判据：`CONFIG_PATH` 打印应指向该工作树）。
- **跨工作树查改动用 `git -C worktree/...`（或绝对路径），不要 `cd`**：`cd` 会把主会话的工作目录切走，后续命令全部落在工作树里（2026-11-03 实测）。
- 每合一张票后主树跑全量：票各自绿 ≠ 合起来绿；语义冲突的典型形态＝测试仍按旧流程喂输入。
- **并行期全量耗时不可比**：多个工作树同时跑全量时 CPU 争用会把 80s 拉成 180s+（2026-11-01 实测 183s／195s，空载复测 78–84s）。判「接线回归」前先空载复测一次，别拿并行期的墙钟下结论。
- **提交只圈定路径**：`git add <票涉及的路径>`，不用 `git add -A`——主树常带着未提交的模型/网关切换改动（`config/agent.yaml`、`model_factory.py` 等），`-A` 会把它们一起裹进票的提交。
- **工作树用完即删，但删前先核对**：优先用 `scripts/worktree.sh remove NN`——默认拒绝「跟踪文件有未提交改动」或「测试副本与主树有差异」的树，别用 `--force` 绕过。删前确认分支 tip 已含实现代理报告的最新提交、且代理已 idle；**追加意见后要等第二次回报再删**（2026-10-31 实测：提前删树，两条未提交文案丢失，只能重建工作树补提交）。建树用 `scripts/worktree.sh add NN`：自动复制 `tests/`、`scripts/` 并打印 `PYTHONPATH` 测试命令。
- 协调会话只负责验证、合并、提交、关票，**不代开实现代理**（除非用户明确要求）。

## 守卫（2026-11-02 起入库）

`tests/`、`scripts/`、`docs/` 与 `AGENTS.md` 自 2026-11-02 起恢复入库（体检 P0-1）：干净检出即可跑测试与结构守卫，CI 与本机跑同一套命令。`.git/hooks/` 仍是每台机器自己的目录、不随 clone 走——新机器跑一次 `scripts/bootstrap-dev.sh` 装回；钩子本体在 `scripts/hooks/`，已被跟踪。
- `scripts/check.sh fast|full`：快档结构守卫（出网守卫、f-string docstring）＋改动文件的 lint ＋测试；`.git/hooks/pre-commit` 调快档。`full` 另跑 mypy 与全量测试；`clean` 把本机 `.env` 从测试里摘掉、模拟 CI 的干净检出（推送前跑它，2026-11-02 CI 首跑红了三条本地看不见的）。**看结尾而退出码不被吞**，直接用这条：
  `bash scripts/check.sh full 2>&1 | tail -5; echo "EXIT=${PIPESTATUS[0]}"`
  （裸 `| tail` 会让红的检查显示成绿——2026-11-01 漏看过一次 collection error。）
- `scripts/check_docstring_shape.py`：AST 守卫，docstring 写成 f-string 即报红（ruff 的 F 规则不抓，全量测试也不报）。
- `scripts/check_commit_msg.sh`：提交信息格式 `type(scope): 摘要（#N）`（无 issue 的 retro 用 `（retro YYYY-MM-DD）`）；`.git/hooks/commit-msg` 调用它。
- `scripts/worktree.sh add|harvest|remove NN`：建树自动复制 `tests/`、`scripts/`、`docs/` 并留一份 tests 基线快照；`harvest` 把工作树里的测试改动回灌主树（主树已分叉就报冲突、不覆盖）；删树先自动 harvest。
- `scripts/bootstrap-dev.sh`：新本机装回 `pre-commit` / `commit-msg` 钩子并检查 `tests/`、`scripts/` 是否就位。
- lint：`ruff` 在 `[dependency-groups].dev` 里、装在 `.venv`；`scripts/check.sh` 用 `"$PY" -m ruff`（不走 PATH）。缺装时 fast 会跳过并提示，安装由用户执行：`uv add --dev ruff`。
- `scripts/check.sh mutate`：守卫变异检验（`scripts/mutation_check.py`），把关键实现改坏、跑指定用例、还原；用例必须红。**新加守卫就往 `MUTATIONS` 补一条**——没有变异检验的守卫不算守卫。
- `scripts/check.sh types|coverage`：mypy（先只查叶子模块）与分支覆盖度量，两个都**装了才跑**，安装由用户执行（`uv add --dev mypy` / `uv add --dev coverage`）。
- `scripts/ac_trace.py`：按注释/docstring 里的 `#NNN` 汇总 issue → 代码 → 测试；用来发现「只有代码、没有测试」的票。不是门禁。
- `tests/test_adr_guard.py`（fast 档）：① 注释/docstring/文档里的 `ADR NNNN` 必须指向真实文件（外部 ADR 写 `ADR-NNNN`，连字符形式不核对）；② 27 份 ADR 必须有且只有一行合规状态行。ADR 写法与索引口径见 `docs/agents/engineering-cadence.md`；索引用 `scripts/adr_index.py` 现生成，不落静态清单。
- 工程化常态（检查档位、发布管理、追溯口径）见 `docs/agents/engineering-cadence.md`；变更记录见根 `CHANGELOG.md`。

## `/implement-spec`（2026-10-21 起）

用它即视为**用户要求并行**——它会为每张票自己开工作树 + 实现子代理，不必再手动 `git worktree add`。它与本仓规则有五处接缝：

- **派发前先对票**：见 `docs/agents/engineering-cadence.md` 的「并行拆票前的对票」。手工按文件簇分组会漏票（2026-11-03 实测 12 票漏了 #150），**集合差非空不许开工**。
- **实现简报必须带「越权假设」条**：规格没写的取舍不自行决定；必须选时记为「越权假设」单列到报告，由协调会话裁定（2026-11-03：画布未选定继承 Z-Image 越出 spec D／US25，靠 Spec 轴复审才抓回）。

- **它收尾停在集成分支上，不落 main、不 push**（第 8 步只「resolve each ticket」并报告分支）。本仓接手：集成分支按**票**拆回主分支提交（一票一次），全量绿后**交用户真实验收**；用户说验收通过后再 push。**本节的「按票拆回 main」就是合 main 的时机**（覆盖工作流段「验收通过后才合 main」的表述）；push 仍然只在验收通过后做。
- **它第 2 步的探索笔记默认存到仓库外**（原文 "a directory outside the repo"）。本仓文档一律落仓——这一步要指定写进 `docs/research/`。
- **它第 8 步按 issue tracker 的方式关票**。本仓口径是先发证据评论再 `close`（见 `CODING_STANDARDS.md` 第七节）；它关完若缺证据评论，由协调会话补上。

## spec 与工单的交接（2026-10-21 起）

`/to-spec` 产出的 spec **先整篇贴进对话给用户审查——这一步不发 issue**（覆盖该 skill 默认的 `gh issue create`）；用户审查通过后才发布成 GitHub issue，然后**由用户手动调 `/to-tickets`** 派发工单。不自动接着调 `/to-tickets`，也不自行拆票。
