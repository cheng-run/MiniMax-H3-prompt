# 官方 edge-stability 句不是自创结构：恢复为逐块要求，缺失报 warning 而非 error

2026-09-22 的官方格式迁移把「防波纹咒语」列为自创结构删除，理由是「官方靠 Picture 锚定句做一致性」。**这个判断反了**：那句中文本，正是官方 `base-en.txt:92-96` **明令要求**的 edge-stability 句的译文。

| | |
|---|---|
| 被删的（自创？） | 全程保持**每个人物的轮廓、面部边缘与服装边缘清晰稳定**，**无波纹、扭曲或边缘抖动**。 |
| 官方要求（`base-en.txt:95`） | Keep every character's **silhouette, facial outline, and clothing edges crisp and stable** throughout; **no rippling, warping, or edge shimmer**. |

官方给这句的理由逐字写在同一段里：**「end every shot block with this exact edge-stability sentence, so the extracted tail frame keeps a crisp character outline for the next segment's first-frame reference」**——即 ADR 0002 保留的那条桥接帧链。删掉它，等于删掉官方为这条链配的尾帧质量防线。

**物证**：全仓库 grep `rippling|shimmer|crisp and stable|silhouette` 只命中 `base-en.txt` 本身；GEN004/GEN005 全部 **9 份**真实分段产物该句出现 **0 次**。这不是潜在缺陷，是每一段产出都缺。

**裁定**：恢复该句，作为对**每个镜头块**的要求（官方措辞是 *end every shot block with*，故按块尾判、逐块判，不是只在段尾加一次）；**缺失报 `warning`，不报 `error`**（2026-09-23 用户裁定）。理由与本项目一贯的定阈值纪律一致：**规则刚落地、合规真实样本为零**，拿 n=0 立 error 级硬闸门，风险是把还没验证的规则变成阻断交付的墙——同 #11 的教训（n=3 的 8 步样本不足以放宽一条 error 闸门，反之亦然）。

**判据宿主**：常量 `h3_validator.EDGE_STABILITY_SENTENCE` 是**代码侧唯一来源**，写段模板从它取。宿主必须是 validator 而非 `segment_prompts`——后者已从 validator 导入（`ALIGN_TEMPLATES` 同理），反向放置即成循环导入。校验器是裁判，判据随裁判走。另有**两份无法插值的硬拷贝**：`prompts/prompt_engineer.md`（人工文本）与正样本 fixture，分别由 `tests/test_role_prompts.py` 与 `tests/test_h3_validator.py` 守着——常量注释里不得写成「唯一来源」，那只对代码侧成立。

**范围**：只接进 `validate_base`。`ref-en.txt` 无此要求，ref 模式不受影响（grep 确认）。这是**同一禁令的第四处**：票面只点了 `_SEGMENT_V2_INSTRUCTION` 一处，实际另有回退式模板、`prompt_engineer.md:51`（整片路径）与 validator 的 `RIPPLE_SPELL_BANNED`（error）——只改票面点的那一处，其余三处会继续禁它。

**考虑过并放弃**：

- **报 error、硬阻断交付**：见上，n=0 不足以立 error 闸门。升级为 error 需要先攒到合规真实样本。
- **接受中文译文、不要求英文逐字符**：官方措辞是 *this exact* sentence，译文脱离训练分布，等于把刚恢复的东西又改回自创。
- **常量放进 `segment_prompts`**（历史计划 `2026-09-17-segment-lock-scoping` §约束第 3 条就是这么记的）：循环导入。
- **只改两条分段路径、不动 `prompt_engineer.md`**：整片路径会继续把官方要求列为禁令，与分段路径自相矛盾。票面未授权这一处，实施时一并改并在提交信息里记明。
- **顺手给 ref 模式也加**：`ref-en.txt` 没有这条要求，加了是凭感觉扩权。

**教训（比本裁定本身更值钱）**：这个知识**当时已经在仓库里**。ADR 0002（2026-09-23 写）第 3 行就引用了这句，并写明「edge-stability 句就是为『抽出的尾帧留给下一段当首帧』而设」——而同一批模板仍然禁着它。这是「规则在、接线不在」的变体：**知识在，但没人把它接到模板上**。写段模板与同一句要求一个禁一个要，缺口横跨两个文件，中间没有任何东西会报错。

**票面归属**：#8。severity 级别由用户在 2026-09-23 会话裁定；其余由该会话产出。
