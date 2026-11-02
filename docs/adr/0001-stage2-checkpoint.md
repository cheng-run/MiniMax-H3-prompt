# 阶段 2 组装后落盘断点（stage2-checkpoint.json）

阶段 2（最终提示词组装+质检）中，组装是最贵的一步（LLM 大上下文一次调用，实测可超 10 分钟）。崩溃后重跑要全部重来成本太高。决定在 `run_stage2` 组装完成后立刻把 `prompt_draft` 落盘为 `stage2-checkpoint.json`，质检循环每轮也更新；完整跑完即删除。崩溃后的重跑检测到该断点，直接跳过组装进入质检精修。

**考虑过并放弃**：

- **ETag 每 5 秒存一次 input/output**：测不出边际（崩溃点在 LLM 网络等待，不在进度计算），且写盘频率高但价值低。
- **把 prompt_engineer 拆成 shot_planner + 纯 Python 装配**：设计评审中被采纳过，但实现复杂度>收益——后续若拆分会单独出 ADR。
- **用数据库/SQLite 存进度**：杀鸡用牛刀，JSON 文件足够。

**_status 字段值**：`assemble_done`（组装完）→ `qa_round_N`（质检第 N 轮精修完）→ 文件删除（完整结束）。
