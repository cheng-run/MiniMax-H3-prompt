# Spec：提示词层迁移至官方 base-en.txt 格式

- **日期**：2026-09-22
- **状态**：待批准
- **来源**：2026-09-21「深夜便利店橘猫」实测的 4 个问题诊断（根因链见对话记录，物证：first.png / 桥接帧 / v1_t2.5 抽帧 / audit 报告 `2026-09-21T104025.md`）
- **决策记录**：grilling 会话 Q1–Q8，全部按推荐路线 A 执行

## 1. 背景与根因（为什么改）

实测发现 4 个问题，其中 3 个的根因指向**提示词层偏离官方格式**：

| 问题 | 根因 | 对策（本 spec） |
|---|---|---|
| ① 猫提前出现（0-1s 干净，2.5s 故事已演完） | GLOBAL_LOCK 详述未出场实体的外观，诱导模型提前画；关键节拍压在 3.9s 无展开空间 | 实体定义只写在首次出场的 Shot；出场节拍距段尾 ≥1s |
| ② 室内开门+跳桌 | BRIDGE_FROM 文字（门缝15-20cm）与桥接帧图片（门大敞、猫已入店）互相矛盾，模型各取一半 | 删 BRIDGE_FROM；图片是唯一事实源，文字只做 Picture 1 锚定 |
| ③ 闪光/异动（两遍复现） | 自创格式偏离模型训练分布，服从度下降 | 全面迁移到官方格式 |
| ④ 切换不流畅 | 数据面已改善（每缝 15.7 帧 vs 基线 24.2），`end_hook` 验证有效，非本轮目标 | 不动 |

关键事实：本次生成 variant 为 **I2VA**（每段单首帧参考）。官方格式对应模板为 I2VA 指令行。

## 2. 改什么

### 2.1 提示词输出格式（核心改动）

新格式严格照官方 `base-en.txt`（skill: h3-prompt-writing）：

**删除的自创结构**（官方不存在）：
- `GLOBAL_LOCK` —— 实体外观只写在**首次出场的 Shot** 内；未出场的实体只在句中出现（如 `No cat is visible in the frame`）或完全不提
- `BRIDGE_FROM` —— 段首状态由 Picture 1 锚定句表达（Case 2：`preserving her appearance, clothing, seat position...`）
- `END_HOOK` —— 段尾状态自然收在最后一个 Shot 的末句（不再复用进下一段文字）
- `This is a N-second continuous shot.` —— 时长由 I2VA 指令行承载
- 每个 Shot 块尾的防波纹咒语（「全程保持…无波纹、扭曲或边缘抖动」）

**新增/对齐的官方结构**：
- 首行 I2VA 指令：`For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.`
- `[Shot 1]` 开头先声明风格（`Live-action, cinematic, ...`）+ 初始构图（§4.1）
- 时间戳 `At 00:XX.XXX` 写进句子内（§4.2）；**关键出场节拍距段尾 ≥1s**（新纪律，validator 检查）
- 段内默认**单 Shot**；段内切镜是显式例外（仅景别跳变等确有必要时），切点严格递增
- `overall_soundscape`：1–4 句英文连续段落，无时间戳（§4.6）
- `non_diegetic_music`：1–3 句英文，或 `N/A`（§4.7）
- **全文英文**；对白/歌词/画面可见文字保留原文语言

**每段中文摘要（用户 Q1 决策）**：沿用 `plan.json` 中每段的中文 `summary` + `end_hook`，在分段输出时逐段展示（已有机制，保持），供用户核对视频走向。**中文摘要不进 H3 提示词**。

### 2.2 涉及文件

| 文件/模块 | 改动 |
|---|---|
| `src/minimax_h3_prompt/agents/prompt_engineer.py` 及相关提示词模板（prompts/*.md） | 输出格式指令重写为官方结构；新增「实体定义只在首次出场 Shot」纪律 |
| `src/minimax_h3_prompt/tools/h3_validator.py` | 删除 GLOBAL_LOCK/BRIDGE_FROM/END_HOOK/防波纹 相关规则；新增：I2VA 指令行存在性、出场节拍距段尾 ≥1s、soundscape/music 句数上限 |
| 分段规划（segment plan）层 | plan.json 结构不变（summary/end_hook 保留）；soundscape/music **按段重写**（英文摘要句），删除时间窗裁切逻辑 |
| `CONTEXT.md` | 术语表更新：段尾钩子降级为 plan 层概念（不进提示词）；删除「防波纹约束」条目；「中文 H3 提示词」→「英文 H3 提示词（附中文摘要）」 |
| 测试 | 上述规则的增删对应单测 |

**不改动**：graph/ui/pipeline/session_store 编排层、ledger/audit/preflight 工具、桥接帧机制本身。

### 2.3 官方样例参照（shot-01 改写稿，已在对话中确认）

见对话记录 2026-09-22 的 shot-01 官方格式改写稿（I2VA 指令行 + Live-action cinematic 开头 + At 00:03.500/00:03.900 节拍 + settle 句尾 + 两句英文 soundscape + N/A music）。

## 3. 预期行为变化

- `launch.py generate-prompts` / `create-video` 产出的分段提示词为官方英文格式，每段附中文摘要展示
- h3_validator 对旧格式（GLOBAL_LOCK 等）报错/警告，对新格式全绿
- 段间衔接职责转移：文字复用（BRIDGE_FROM）→ 桥接帧图片 + Picture 1 锚定句
- 对用户可见的变化：提示词正文变英文；中文走向判断走 summary 层

## 4. 风险与回滚

| 风险 | 对策 |
|---|---|
| 删防波纹咒语后剥尾帧人物识别率下降 | 上下文标注：若下轮生成出现此问题，第一嫌疑即此项，恢复一句咒语成本极低 |
| 英文提示词对中文场景词（如「中华田园猫」）表达偏差 | 生成后由用户核对中文摘要与画面；发现偏差迭代 |
| LLM 按段重写 soundscape 可能跨段风格不接 | plan 层的 summary 已约束每段情绪；真出问题再引入跨段音乐提示传递 |

## 5. 测试方式

1. **离线（交付门槛）**：pytest 全绿（现有 239 条不回归 + 新规则单测）；用本次会话的 shot-01.md 旧文件作为 validator 负样本（应报 GLOBAL_LOCK 等错误）、对话中确认的官方格式改写稿作为正样本（应全绿）
2. **真实验收（用户择机）**：同一 brief「深夜便利店橘猫」重新生成，对照问题①②③逐条检查 + `audit run` 接缝数据对比 15.7 帧基线

## 6. 明确不做

- 桥接帧风格漂移的工具层对策（本轮无站得住的动作，官方格式本身是对策）
- 编排层（graph/ui/pipeline）改动
- FL2VA/L2VA 变体适配（本次实测 I2VA，其他变体等用到再按官方 Case 3/4 模板加）
