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

## 票据工作流（2026-10-30 修订）

- **默认不开工作树**：只有用户明确要求并行执行任务时才 `git worktree add worktree/ticket-NN`（`/worktree/` 已 gitignore）；单票在主树上切分支做，不在主分支直改。
- **工作树的提交不进历史**：工作树里可以提交到它自己的临时分支，但那些提交**永不 push**；合并回原分支走 `git merge --squash`，**最终提交只落在主分支上、一票一次**。
- **推送放最后，验收后推**：改动碰到 `src/` 或 `tests/` 时必须重跑全量；全量绿只到「交给用户验收」这一步，代码改动要**用户真实验收通过后**才合并并 push（口径见全局 CLAUDE.md「🌿 代码改动走分支」）。**纯文档/占位改动**沿用最近一次绿的结果，并在推送时说明依据哪一次，可直接 push。
- 工作树内跑测试先设 `PYTHONPATH=<工作树>\src` 再用主仓 `.venv`，否则测的是主树（判据：`CONFIG_PATH` 打印应指向该工作树）。
- 每合一张票后主树跑全量：票各自绿 ≠ 合起来绿；语义冲突的典型形态＝测试仍按旧流程喂输入。
- **提交只圈定路径**：`git add <票涉及的路径>`，不用 `git add -A`——主树常带着未提交的模型/网关切换改动（`config/agent.yaml`、`model_factory.py` 等），`-A` 会把它们一起裹进票的提交。
- **工作树用完即删**：`git worktree remove worktree/ticket-NN`，不留残树（残树占盘，还会搅乱 `git worktree list`）。
- 协调会话只负责验证、合并、提交、关票，**不代开实现代理**（除非用户明确要求）。

## `/implement-spec`（2026-10-21 起）

用它即视为**用户要求并行**——它会为每张票自己开工作树 + 实现子代理，不必再手动 `git worktree add`。它与本仓规则有三处接缝：

- **它收尾停在集成分支上，不落 main、不 push**（第 8 步只「resolve each ticket」并报告分支）。本仓接手：集成分支按**票**拆回主分支提交（一票一次），全量绿后交用户真实验收，通过再 push。
- **它第 2 步的探索笔记默认存到仓库外**（原文 "a directory outside the repo"）。本仓文档一律落仓——这一步要指定写进 `docs/research/`。
- **它第 8 步按 issue tracker 的方式关票**。本仓口径是先发证据评论再 `close`（见 `CODING_STANDARDS.md` 第七节）；它关完若缺证据评论，由协调会话补上。

## spec 与工单的交接（2026-10-21 起）

`/to-spec` 产出的 spec **先整篇贴进对话给用户审查——这一步不发 issue**（覆盖该 skill 默认的 `gh issue create`）；用户审查通过后才发布成 GitHub issue，然后**由用户手动调 `/to-tickets`** 派发工单。不自动接着调 `/to-tickets`，也不自行拆票。
