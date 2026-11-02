# Handoff：路线产物分离（spec #77）实现完成，待用户实跑后推送 main

日期：2026-10-24
仓库：`D:\项目\python\MiniMax-H3-prompt`

## 一句话现状

spec #77 的五张工单（#78–#82）已实现、双轴复审、发证据评论并关闭；`archive/ref2va-full` 已推送
（`d01455e`）；本地 `main` 已合并该线（`c4a04fe`）并修掉一条合并后才暴露的用例（`be1c64b`），
全量绿，**尚未推送**——用户要先亲自跑项目验收，之后才决定 `git push origin main`。

## 关键坐标（不要重做）

- spec 与验收口径：GitHub issue #77（含 #78–#83 子票，证据评论都在票面）
  <https://github.com/cheng-run/MiniMax-H3-prompt/issues/77>
- 落地提交（`archive/ref2va-full` 与本地 `main` 都含）：
  - `21436dd` 勘察笔记 `docs/research/2026-10-24-route-product-separation-exploration.md`
  - `b4dde8a` #79 术语三条 + `docs/adr/0020-route-decides-artifacts-not-graph-copies.md`
  - `212475d` #78 源头守卫 + 四个出口统一取数口
  - `2e79ea0` #81 重跑确认门按路线收敛
  - `af31b59` #80 会话读回按路线丢三键
  - `d01455e` 双轴复审采纳（判据唯一化 `brief_parser.is_ref_mode`、三键常量 `graph/state.py:ASSET_PROMPT_KEYS`、删 `brief=None` 后门等）
  - `c4a04fe` merge archive → main（唯一冲突 `config/agent.yaml`，取 main 最新版，已验证该文件零差异）
  - `be1c64b` `tests/test_config.py` 的 ANTHROPIC 回退用例改为显式构造 `ModelSettings`（见下「已知坑」）
- 证据文件（工作树外，保留）：`C:/Users/XOS/AppData/Local/Temp/h3-t82/`
  （`issue-*-comment.md` 已发布评论的原文、`compare.py`、`state-ref.json`、`accept.py`、`pre/post-*.json`、`mutation-a/b.txt`）

## 分支/远端状态（2026-10-24 交接时）

| 分支 | 本地 | 远端 |
|---|---|---|
| `main` | `be1c64b`，领先 origin **79** 个提交 | `origin/main` 仍停在 `43a2c06` |
| `archive/ref2va-full` | `d01455e` | 已推送，与本地一致 |

工作树干净；`worktree/` 下已无残树。本地还留着已合并的临时分支（`spec77-integration`、
`review-fixes`、`ticket-78..82`），可按需清理。

## 下一步（按用户实跑结果二选一）

1. **用户跑通、认可** →
   - `git push origin main`（快进，无需 force；仓库规则「全量绿即自动 push」已满足）。
   - 关 #83：先贴证据评论（合并提交、合并后全量、配置文件处理），再 `gh issue close 83`。
   - 关 #77：引用 #82 的验收评论 + main 已合并。
2. **用户跑出问题** → 用 `diagnosing-bugs` 在本地区分「本次回归 vs 既有问题」；修复走 `tdd`，
   改完在主树跑全量（`PYTHONPATH=<repo>/src` + 仓内 `.venv`），再回到第 1 步。

## 已知坑 / 口径（省得重新踩）

- **路线判据只有一个真源**：`brief_parser.is_ref_mode(mode)`（`mode == "ref"`）。禁止用
  「变体是否 FL2VA」「有无首尾帧 bundle」代替——多参的变体是 T2VA、没有 bundle。
- **三键单一来源**：`graph/state.py:ASSET_PROMPT_KEYS`；白名单 `_STATE_KEYS` 与读回过滤都从它派生。
- **会话过滤在读回、不写盘**：#80 的「文件里不再有三键」口径是 `load_session → save` 后收敛；
  `find_awaiting_sessions` 的扫描不该改写用户目录。票面已按此说明。
- **没有真实多参旧会话样本**：`output/sessions/` 十份全是 base/I2VA；多参保留行为用自造夹具验证，
  已在测试与关票评论里注明「自造」。
- **`test_config.py::test_anthropic_auth_token_fallback` 的假绿**：它原先拿 YAML 的 primary 当夹具，
  以前靠 `.env` 真 key 兜住断言；用户把主模型换到网关后必然失败。已改为显式构造
  `ModelSettings(provider="anthropic", api_key_env="ANTHROPIC_API_KEY")`，与 YAML 解耦。
- **测试基线**：`1272 collected = 1267 passed + 5 skipped`（5 条 skip 是缺 ComfyUI 素材的环境性跳过）。
- 工作树内跑测试必须 `PYTHONPATH=<该树>/src`；主树 `.venv` 是 editable 指向主树。
- 合并后语义冲突的教训：自动合并零冲突 ≠ 正确，合并后必须重跑全量（本次正是这样抓出 `test_config`）。

## 建议下一会话使用的 skills

- `diagnosing-bugs` — 用户实跑若报错/行为不对，先诊断再改。
- `tdd` — 任何修复或补测都先写会红的用例。
- `code-review` — 若本轮又有成批改动，合入前跑双轴（固定的 spec 引用正是 #77）。
- `resolving-merge-conflicts` — 若以后还要把分支合回 main。
- 项目票据约定见 `docs/agents/issue-tracker.md`（`gh` CLI，证据评论后再 close）。
