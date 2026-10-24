# 分段提示词质量重构（细颗粒度 + 桥接锚定）

## 背景

当前长视频（>10s）走「整条提示词 → 按 [Shot N] 机械拆分 → 逐段陪跑」。实测质量问题：
剧情断裂、段间人物/场景漂移、运镜无理、细节丢失。根因：每个分段提示词只有孤立镜头块，
缺全局设定约束；拆分粒度按 Shot（过粗），段内缺乏细颗粒度时间轴。

## 决策

1. **拆分边界整数化**：段边界必须是整数秒，且每段时长 ∈ [4,10]。
   段内时间戳允许 0.1s 精度（`At 00:01.700`），由剧情驱动切分。
2. **两步生成**：
   - `segment_planner`（LLM，轻量）：只规划整数段边界 + 每段覆盖的 Shot + 前后衔接说明，输出 JSON。
   - 每段独立调 `prompt_engineer`，上下文小（本段 Shot 描述 + 全局设定锁定 + 衔接提示），token 省且快。
3. **每段提示词必含 3 个块**（在此前 [Shot] 块基础上增加）：
   - `GLOBAL_LOCK` 块：人物/服装/场景/光线/氛围的不可违背约束（从 art_design/character_design 取）
   - 段首状态：`bridge_from`（上一段末尾画面状态，第 1 段 = 首帧图描述）
   - **段尾钩子** `end_hook`：本段最后 0.5-1s 的具象画面（谁+位置+朝向+动作），作为下一段首帧的锚
4. **回头改写整条 prompt**：两步完成后，把所有段 + soundscape/music 重新装配为完整英文提示词存档。

## 数据流

```
shot_table ──► segment_planner ──► segments[{start_s,end_s,shots[...],bridge_from,end_hook,hint}]
                                      │
                                      ▼
                        每段独立调 prompt_engineer，产出该段英文提示词（细颗粒度）
                                      │
                                      ▼
                              segments/shot-NN.md 落盘
                                      │
                                      ▼
                              组装最终完整 prompt（含所有 Shot + soundscape/music）→ video-prompt.md
```

## 失败与降级

- segment_planner 超时/无法解析 → 回退旧机械拆分路径（不丢功能）
- 某段 prompt_engineer 调用失败 → 该段保留占位并打印告警，其余段继续；用户可在分段时间线配置重试

## 范围之外（YAGNI）

- 不重写现有 stage 1（编剧/设计/美术/QA）
- 不引入官方 Context-IR（本地部署无 API 密钥）
- 不做逐帧（0.1s 是全片最细，不进 30fps 逐帧标注）

## 测试

- segment_planner 的 JSON 解析 + 校验（边界整数、时长区间、覆盖完整）
- 段落提示词包含 GLOBAL_LOCK / bridge_from / end_hook 三个块
- 现有分段陪跑入口（`_run_segmented_flow`）在新生成下兼容
