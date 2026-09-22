# 分段提示词"锁定区越窗"修复设计（Segment Lock Scoping）

> ⚠️ **未实施 + 部分已废弃（2026-09-22 标注）**：本设计的 `GLOBAL_LOCK` 集中定义区与防波纹咒语部分，
> 已被 **2026-09-22 官方格式迁移废弃**（`tools/h3_validator` 报 error，见 `CONTEXT.md` 末节）。
> **根因分析仍然成立**，尤其「根因 4：`shot_text_{n}` 键根本不存在 → 回退整张分镜表」，
> 已由 2026-09-22 的首帧锚定修复落地（`segment_prompts._segment_shot_texts`，缺陷编号 H4）。
> 配套计划 `docs/superpowers/plans/2026-09-17-segment-lock-scoping.md` 同样未实施。

## 背景

实测一次 30s / 5-shot 长视频分段生成（会话 `古老图书馆里-少年撕下会发光的书页折成纸-9ac0bf14`），
逐段提示词出现严重的"全片剧情重复注入"，第 1、2 段视频均不及预期。

对落盘的 `segments/shot-01.md` / `shot-02.md` 做量化对比得到：

| 指标 | 第 1 段（0-6s） | 第 2 段（6-11s） |
|---|---|---|
| 总字符 | 52381 | 38115 |
| 锁定区（GLOBAL_LOCK 全字段）占比 | 90.6% | 84.8% |
| 本段执行区占比 | 9.4% | 15.2% |
| 官方示例体量参考 | 139–168 词 | 同左（本段 8330 / 5985 词） |

第 1 段（执行区只要求"指尖触碰发光书页"）的锁定区里出现
`fold into a paper bird` / `dragon-bone star sea` / `stained-glass window breaks` 等
第 3–6 镜才发生的节拍：`character` 字段中 `paper bird` 出现 25 次、`leap` 9 次、
`star sea` 18 次。H3 是执行型模型，锁定区的注意力权重压过了 9% 的执行区，导致模型提前演出后段剧情。

同时段间一致性破裂：`off-white shirt` vs `cream-white shirt`、`prop` vs `props`、
`Lower clothing` vs `Trousers`；第 2 段 `BRIDGE_FROM` 与第 1 段 `END_HOOK` 文本相似度仅 **0.04**
（设计要求"原样复用"）。

## 根因

1. **锁定区来源是全片文档，且被要求"逐条抄入，不得漏项"**
   `build_segment_v2_request` 把 `character_design`(6510 字) / `background_design`(5522) /
   `prop_design`(3532) / `art_design`(8199) / `creative_lock`(353) 整体塞进 GLOBAL_LOCK。
   这些文档本身就是按"全片六场"写的，天然包含后段剧情。
2. **每段独立调 LLM 复写锁定区** → 同一事实在不同段被翻译成不同措辞，跨段漂移。
3. **`BRIDGE_FROM` 只靠提示词约束 LLM"复用"**，没有确定性强制，实际被重写。
4. **`shot_text_{n}` 键根本不存在**：`build_segment_v2_request` 回退到
   `state.get("shot_table")`，即**整张分镜表（全部 6 个 Shot）**，把后段剧情直接送进本段写作上下文。
5. **分段产物不合官方规范**：缺 I2VA 首行对齐指令、`<Picture 1>` 出现 0 次、
   第 2 段保留 `[Shot 2]` 导致 `SHOT_GAP` / `SHOT_NO_TIMESTAMP` 错误
   （项目自带 `h3_validator` 已复现）。首行 `This is a N-second continuous shot.` 非官方规范格式。

## 决策

### 1. 锁定区结构化：一次生成、按窗口裁剪、逐字复用

新增 `SegmentLock` 结构，由**一次** LLM 调用产出结构化 JSON（不是每段一次）：

- `identity` / `wardrobe` / `scene` / `style` / `forbidden`：全片适用，但强制精简
  （合计 ≤ 180 词，identity ≤ 6 条、每条 ≤ 25 词）。
- `props`：每个道具带**阶段（phase）**，每个 phase 有 `start_s` / `end_s` / `text`。
  例如 `glowing_page` 在 0-6s 是"仍夹在禁书里、未撕"；6-11s 是"正沿纤维撕下、未折"。
- `beats`：全片剧情节拍及其时间窗，供越窗检查使用。

渲染时按本段 `[start_s, end_s)` 取子集：全片条目 + 与窗口相交的 prop phase。
LLM 只负责**生成一次**，之后每段从同一字符串渲染 → 跨段措辞逐字一致。

### 2. 强制"本段窗口"语义

- 段写作提示词删除"GLOBAL_LOCK 逐条抄入"措辞，改为"锁定区由系统注入，你只写本段窗口内的剧情"。
- 明确禁止写入窗口外的节拍（折纸/纸鸟/碎窗/跃出/星海）。
- 锁定区字符占比 ≤ 35%、整段 ≤ 2500 词，超限视为缺陷。

### 3. 确定性规范化（不依赖 LLM）

新增 `normalize_segment_prompt(text, plan, variant, duration)`，按顺序：
1. 删除非官方首行 `This is a N-second continuous shot.`
2. 把段内所有 `[Shot N]` 折叠为 `[Shot 1]`（每个执行段都是独立单镜头视频）
3. 首行补 I2VA 对齐指令（`ALIGN_TEMPLATES["I2VA"]`），后接一个空行
4. `BRIDGE_FROM` 正文强制覆写为上一段 `END_HOOK` 原文（第 1 段用首帧描述）
5. 确保镜头块以 `EDGE_STABILITY_SENTENCE` 结尾

### 4. 越窗检查进入陪跑回路

新增 `tools/segment_scope.py`：按 `beats` 表扫描段正文，命中窗口外节拍关键词时给出告警；
先剥离否定子句（`no ... yet` / `without ...`）以容忍合法的负向约束。
另做锁定区预算检查（占比、词数）。检查在**展示给用户之前**执行并打印，不阻断流程。

## 数据流

```
state(全片设计文档) ──► build_segment_lock (LLM×1) ──► SegmentLock
                                                        │
plans[{start_s,end_s,shots}] ───────────────────────────┤
                                                        ▼
                            每段: render_lock_for_segment(窗口) + 本段 Shot 抽取
                                                        │
                                                        ▼
                                  write_segment_v2 (LLM×N，只写窗口内剧情)
                                                        │
                                                        ▼
                          normalize_segment_prompt（对齐指令/Shot 1/BRIDGE 原文）
                                                        │
                                                        ▼
                          check_segment_scope（越窗 + 预算）──► 落盘 shot-NN.md
```

## 失败与降级

- `build_segment_lock` 失败 → 回退到旧 GLOBAL_LOCK 全量注入路径，并打印显式告警。
- `normalize_segment_prompt` 幂等，可对已有文件重复执行。
- 越窗检查为 warning 级，不阻断人工陪跑。

## 范围之外（YAGNI）

- 不重写阶段 1（编剧/设计/美术/QA）。
- 不改 `segment_planner` 的边界规划算法。
- v1 机械拆分路径（`split_shots_from_prompt`）的同类问题本次一并记录但不改，
  因该路径仅在 v2 planning 失败时兜底。
- 不引入官方 Context-IR，不新增第三方依赖。

## 测试

- `SegmentLock` 序列化/反序列化往返、按窗口取子集（含 prop phase 边界：`end_s` 开区间）。
- 渲染出的两段共享条目字符串**逐字节相同**。
- 0-6s 窗口渲染结果不含折纸/纸鸟/碎窗/跃出/星海阶段文本。
- `normalize_segment_prompt`：首行为 I2VA 对齐指令、`[Shot 2]`→`[Shot 1]`、
  `BRIDGE_FROM` 等于上段 `END_HOOK` 原文、幂等。
- `extract_segment_shots` 只返回请求的 Shot 号。
- `check_segment_scope`：越窗节拍报 warning、否定句不误报、预算超限报 warning。
- 用真实 `session-state.json` 做端到端回归，断言第 1 段锁定区不含后段节拍。
