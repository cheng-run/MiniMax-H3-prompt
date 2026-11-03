# Handoff：多参分段 retention 裁剪与校验器（2026-10-26）

## 当前状态

分支 `spec/ref-capacity-2026-10-25`，工作区干净，本地比远端多 1 个提交（`ab9efe3`，推送时网络连续失败，待重推）。

本次会话从一个用户提问（多参能否像首尾帧那样用尾帧接续）出发，查到实际问题是**两段提示词内容重复**，进而定位到 `retention_analysis` 整片重复这个诱因，做了一轮对照实验和一组修复。完整诊断见 `docs/design/2026-10-26-多参分段产物不一致诊断.md`（已提交，含五个段文件的差异表、三层根因、我在过程中的三个错误）。

## 已完成（提交）

| 提交 | 内容 |
|---|---|
| `828cfe3` | `retention_analysis` 按本段镜头裁剪（`segment_refs.scope_retention_to_shots`） |
| `340d0f2` | 注释与旧口径对齐 |
| `3c92a1e` | ADR 0022（决策与实测证据） |
| `93923da` | 留存分析按本段定义块过滤悬空 Subject |
| `566024c` | 校验器新增 `SEGMENT_RETENTION_UNDEFINED` |
| `ab9efe3` | 诊断文档（**未推送**） |

实测依据：段 2 整片 retention 时首帧与段 1 首帧 MAD 27.68（重演），裁后 46.59。全量最近一次 **1392 passed / 5 skipped**。

## 未完成

1. **推送 `ab9efe3`**：`git push origin spec/ref-capacity-2026-10-25`（网络恢复后）。
2. **重写段 4**：`output/sessions/一个二次元游戏的CG动画-adc4c797/GEN001/segments/segment-04.md` 已改名为 `segment-04.pre-fix.md.bak`，系统会认为该段未写。用户跑终端走到第 4 段时会重写，应同时吃到镜头裁剪与悬空过滤。若仍报 `SEGMENT_RETENTION_UNDEFINED`，说明过滤没接上。
3. **画风 Subject 待观察**：本例 `<Subject 6>`（2D 手绘动画 CG 质感）因 Shot 4 原文未提它而被素材子集裁掉，段 4 定义块与正文都没有画风约束。等重写的段 4 出片比对风格，漂了才做「整片每帧都点名的 Subject 强制保留」。
4. **手工对照件**：`segment-02-retention-scoped.md` 已移入 `segments/_archive/`，不要当成系统产物解读。

## 关键事实（避免下个 agent 重查）

- **段 4 只收 A 是正确行为**，不是 bug：整片 Shot 4 原文只提 A（B 在画外），`plan.json`（10-25 生成）里段 4 也就是 1 图 / Subject [3]。用系统自身的 `_shot_blocks` + 真实台账复算与 plan 一致。
- **素材子集与留存分析裁剪不同源**：前者按图（`compute_segment_scope`），后者按镜头号（`scope_retention_to_shots`）。两者的交集就是悬空标签的成因。
- **画风 Subject 没有编号约定**（`reference_consistency.md` 的「你的产出（markdown）」只把「风格」列为 Subject 一类），所以「强制保留画风」只能靠结构启发式。
- **用户规则**：改任何代码前必须先给方案、经用户确认再动手。本会话因擅自改代码被明确批评过，这条不可再犯。
- **产物时间线**：段 1（10-25 19:01）/ 段 2（10-26 10:06）/ 段 3（11:38）都写于代码改动前，段 4（13:38）写于改动后——同批产物混用两套口径是历史事实，不要误判为随机性。

## 建议 skills

- `diagnosing-bugs`：若重写后的段 4 仍异常，用它的诊断循环，别凭猜。
- `code-review`：若要复审 `828cfe3..566024c` 这一段改动。
- `tdd`：若要为「画风 Subject 强制保留」继续加用例。
- `writing-for-agents`：若要把「改代码前先确认」写进项目 `AGENTS.md`（属于跨工具偏好，需用户确认）。

## 相关文档

- `docs/design/2026-10-26-多参分段产物不一致诊断.md`
- `docs/adr/0022-ref-retention-scoped-to-segment-shots.md`
- `docs/adr/0011-ref-segments-have-no-single-image-anchor.md`（多参无帧锚定的原始裁定）
- `CONTEXT.md` 的「多参生成路线」一节
