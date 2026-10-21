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

## 票据工作流（2026-10-21 修订）

- **默认不开工作树**：只有用户明确要求并行执行任务时才 `git worktree add worktree/ticket-NN`（`/worktree/` 已 gitignore）；单票照常在主树做。
- **工作树的提交不进历史**：工作树里可以提交到它自己的临时分支，但那些提交**永不 push**；合并回原分支走 `git merge --squash`，**最终提交只落在主分支上、一票一次**。
- **推送放最后，全量绿才推**：主树跑完全量测试、确认无冲突，即自动 `git push`，不再逐次询问。
- 工作树内跑测试先设 `PYTHONPATH=<工作树>\src` 再用主仓 `.venv`，否则测的是主树（判据：`CONFIG_PATH` 打印应指向该工作树）。
- 每合一张票后主树跑全量：票各自绿 ≠ 合起来绿；语义冲突的典型形态＝测试仍按旧流程喂输入。
- **提交只圈定路径**：`git add <票涉及的路径>`，不用 `git add -A`——主树常带着未提交的模型/网关切换改动（`config/agent.yaml`、`model_factory.py` 等），`-A` 会把它们一起裹进票的提交。
- **工作树用完即删**：`git worktree remove worktree/ticket-NN`，不留残树（残树占盘，还会搅乱 `git worktree list`）。
- 协调会话只负责验证、合并、提交、关票，**不代开实现代理**（除非用户明确要求）。

## spec 与工单的交接（2026-10-21 起）

`/to-spec` 产出的 spec **先整篇贴进对话给用户审查——这一步不发 issue**（覆盖该 skill 默认的 `gh issue create`）；用户审查通过后才发布成 GitHub issue，然后**由用户手动调 `/to-tickets`** 派发工单。不自动接着调 `/to-tickets`，也不自行拆票。
