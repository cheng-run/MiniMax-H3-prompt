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
前端工程（技术栈、本地坑、`PW_CHANNEL` 跑 E2E、起前后端与关端口、契约生成）见 `docs/agents/frontend.md`。
工作台会话与 Headless API（**启动入口**、模块地图、事件/写入/产物不变量、契约链）见 `docs/agents/workbench.md`。
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

- **动手改文件前先开分支**：`git switch -c <类型>/<slug>`，不是提交前才开；main 工作区不留未提交的实现改动（2026-11-13 实际反例：在 main 上写完两个新文件才建分支）。
- **默认不开工作树**：只有用户明确要求并行执行任务时才 `git worktree add worktree/ticket-NN`（`/worktree/` 已 gitignore）；单票照常在主树做。
- **工作树的提交不进历史**：工作树里可以提交到它自己的临时分支，但那些提交**永不 push**；合并回原分支走 `git merge --squash`，**最终提交只落在主分支上、一票一次**。
- **推送放最后，验收通过才推**：整条 spec 拆回 main 后跑一次全量（`full` ＋ `clean`）；**纯文档/占位改动沿用最近一次绿的结果**，并在推送时说明依据哪一次。全量绿只是**交验收的前提**——用户说「验收通过／可以合了」之后才合 main 并 `git push`；验收前不推。全局 `~/.claude/AGENTS.md` 的验收门是唯一真源（覆盖 2026-10-21 版「绿了即自动 push」）。
- 工作树内**直接**跑 pytest 要先设 `PYTHONPATH=<工作树>\src` 再用主仓 `.venv`，否则测的是主树（`scripts/check.sh` 已自设，走它就不用管；判据：`CONFIG_PATH` 打印应指向该工作树）。
- **跨工作树查改动用 `git -C worktree/...`（或绝对路径），不要 `cd`**：`cd` 会把主会话的工作目录切走，后续命令全部落在工作树里（2026-11-03 实测）。
- **全量只在整条 spec 收尾跑一次（2026-11-12 用户裁定，覆盖旧「每合一张票跑全量」）**：集成分支上逐票合并不跑全量，每张票自己的 `fast` 绿即可——一轮 full 约 2 分钟，十几张票逐票跑纯属浪费。spec 全部完成、按票拆回 main 后，在 main 跑一次 `scripts/check.sh full` ＋ `clean`。合并/解冲突后需要兜底就只跑**受影响的测试文件**（`fast` 档已含秒级用例）。拆完 `git diff --stat main <集成分支>` 必须为空（内容保真的重放）；非空说明漏了提交或解冲突改坏了内容，停下别 push。交验收时说明依据哪一次全量。
- **并行期全量耗时不可比**：多个工作树同时跑全量时 CPU 争用会把 80s 拉成 180s+（2026-11-01 实测 183s／195s，空载复测 78–84s）。判「接线回归」前先空载复测一次，别拿并行期的墙钟下结论。
- **提交只圈定路径**：`git add <票涉及的路径>`，不用 `git add -A`——主树常带着未提交的模型/网关切换改动（`config/agent.yaml`、`model_factory.py` 等），`-A` 会把它们一起裹进票的提交。
- **工作树用完即删，但删前先核对**：优先用 `scripts/worktree.sh remove NN`——默认拒绝「跟踪文件有未提交改动」或「测试副本与主树有差异」的树，别用 `--force` 绕过。删前确认分支 tip 已含实现代理报告的最新提交、且代理已 idle；**追加意见后要等第二次回报再删**（2026-10-31 实测：提前删树，两条未提交文案丢失，只能重建工作树补提交）。建树用 `scripts/worktree.sh add NN`：自动复制 `tests/`、`scripts/` 并打印 `PYTHONPATH` 测试命令。**合入 main 后**用 `remove NN --branch` 一并删票分支（票分支是 squash 合入、不是 main 祖先，脚本用 `-D`；手工删容易漏，2026-11-14 一次清理漏了 11 个）。**自建工作树（`integration-*` 等）用 `remove-path <绝对路径>`**；工作树里的 `frontend/node_modules` / `.venv` 只能是目录联接，且**永远别绕过脚本直接 `git worktree remove`**——它会顺着联接把主树依赖删空（2026-11-12 实测，`.bin` 整份消失）。
- 协调会话只负责验证、合并、提交、关票，**不代开实现代理**（除非用户明确要求）。

## 守卫（2026-11-02 起入库）

`tests/`、`scripts/`、`docs/` 与 `AGENTS.md` 自 2026-11-02 起恢复入库（体检 P0-1）：干净检出即可跑测试与结构守卫，CI 与本机跑同一套命令。`.git/hooks/` 仍是每台机器自己的目录、不随 clone 走——新机器跑一次 `scripts/bootstrap-dev.sh` 装回；钩子本体在 `scripts/hooks/`，已被跟踪。
- **时机**：提交前 `scripts/check.sh fast`；交验收/推送前 `full` + `clean`（`clean` 把本机 `.env` 从测试里摘掉，模拟 CI 的干净检出；`full` 已含变异检验）。
- **跑动顺序**：实现 → **复审 → 全量**。复审还可能改代码时先别跑全量，那一轮会被作废；`/implement` 的步骤串默认把全量排在复审之前，本仓不照做（2026-11-10 #225：上一次 retro 只把这条写进 cadence，57 分钟后仍白跑 129 秒——规则要住在这里）。长检查（full / 变异 / 打包）只跑一次、放后台等完成通知，别在等待期周期性探查；实现切片用 `fast`。
- **判据是最后一行**：`[check] <档位>: PASS` / `FAIL（退出码 N）`——管道随便接，`| tail -3` 也不会把红显示成绿（2026-11-08 起由脚本自己保证，不用再记 `PIPESTATUS`）。
  **退出码不可信**：接了管道之后拿到的是 `tail` 的 0。后台或管道跑检查，结束时**先读那一行**再继续（2026-11-14 实测：rebase 后的 fast 没读判据行，锚点红被当成绿，直到 full 才发现）。
- **提交信息格式**：`type(scope): 摘要（#N）`；无 issue 的 retro 用 `（retro YYYY-MM-DD）`。
- **新加守卫必须补一条变异**（`scripts/mutation_check.py` 的 `MUTATIONS`）：没有变异检验的守卫不算守卫。
- 各档位做什么、依赖缺装、契约/前端/工作树的命令细节见 `docs/agents/engineering-cadence.md`；变更记录见根 `CHANGELOG.md`。

## `/implement-spec`（2026-10-21 起）

用它即视为**用户要求并行**——它会为每张票自己开工作树 + 实现子代理，不必再手动 `git worktree add`（这也是「票据工作流」里「协调会话不代开实现代理」的例外：`/implement-spec` 自身即为用户明确要求）。它与本仓规则有六处接缝：

- **派发前先对票**：见 `docs/agents/engineering-cadence.md` 的「并行拆票前的对票」。手工按文件簇分组会漏票（2026-11-03 实测 12 票漏了 #150），**集合差非空不许开工**。
- **实现简报必须带「越权假设」条**：规格没写的取舍不自行决定；必须选时记为「越权假设」单列到报告，由协调会话裁定（2026-11-03：画布未选定继承 Z-Image 越出 spec D／US25，靠 Spec 轴复审才抓回）。简报还要交代**真实会话证据的位置**：`output/sessions/` 与 `output/accept-*` 只在主树（gitignore），工作树不复制；需要实跑对照时由协调会话执行，或给主树绝对路径。

- **实现简报要求代理边做边提交**：每完成一个可验证切片就提交一次；被中断（用户主动打断或会话异常）时，未提交改动会留在工作树里、甚至丢失（2026-11-10 实测 #217/#218 各被打断一次，靠中途追加「先提交」才保住进度）。

- **它收尾停在集成分支上，不落 main、不 push**（第 8 步只「resolve each ticket」并报告分支）。本仓接手：集成分支按**票**拆回主分支提交（一票一次），全量绿后**交用户真实验收**；用户说验收通过后再 push。**本节的「按票拆回 main」就是合 main 的时机**（覆盖工作流段「验收通过后才合 main」的表述）；push 仍然只在验收通过后做。
- **它第 2 步的探索笔记默认存到仓库外**（原文 "a directory outside the repo"）。本仓文档一律落仓——这一步要指定写进 `docs/research/`。
- **它第 8 步按 issue tracker 的方式关票**。本仓口径是先发证据评论再 `close`（见 `CODING_STANDARDS.md` 第七节）；它关完若缺证据评论，由协调会话补上。

## spec 与工单的交接（2026-10-21 起）

`/to-spec` 产出的 spec **先整篇贴进对话给用户审查——这一步不发 issue**（覆盖该 skill 默认的 `gh issue create`）；用户审查通过后才发布成 GitHub issue，然后**由用户手动调 `/to-tickets`** 派发工单。不自动接着调 `/to-tickets`，也不自行拆票。
