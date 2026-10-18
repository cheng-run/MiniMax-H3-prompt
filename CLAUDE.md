# MiniMax-H3-prompt

生成 MiniMax H3 视频生成提示词并把长视频拆成可逐段执行的工具。
项目术语与架构见 `CONTEXT.md`；动手前先读它。

## Agent skills

### Issue tracker

Issues 活在 GitHub Issues（github.com/cheng-run/MiniMax-H3-prompt），用 `gh` CLI 操作。见 `docs/agents/issue-tracker.md`。

### Triage labels

五档默认标签（needs-triage / needs-info / ready-for-agent / ready-for-human / wontfix），无映射。见 `docs/agents/triage-labels.md`。

### Domain docs

单上下文：根 `CONTEXT.md`（术语表）+ `docs/adr/`（架构决策）。见 `docs/agents/domain.md`。

## 并行票据工作流（2026-10-18 起）

- 用户在 `worktree/ticket-NN`（`/worktree/` 已 gitignore）里开会话实现票；协调会话只负责验证、合并、提交、关票，**不代开实现代理**（除非用户明确要求）。
- 工作树内跑测试先设 `PYTHONPATH=<工作树>\src` 再用主仓 `.venv`，否则测的是主树（判据：`CONFIG_PATH` 打印应指向该工作树）。
- 每合一张票后主树跑全量：票各自绿 ≠ 合起来绿；语义冲突的典型形态＝测试仍按旧流程喂输入。
