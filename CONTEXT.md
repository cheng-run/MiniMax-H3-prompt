# MiniMax-H3 Prompt

生成 MiniMax H3 视频生成提示词并把长视频拆成可逐段执行的工具。用户从简短的创意出发，经分镜与关键帧生图提示词，得到最终**英文** H3 提示词（官方 base-en.txt 格式；字段名仍为英文：integrated_multimodal_description / overall_soundscape / non_diegetic_music；对白、歌词、画面可见文字按原文语言保留），并附**中文摘要**供用户核对视频走向；长视频按镜头拆成多个执行段（Segment），每段在 ComfyUI 手动跑通后剥尾帧作为下一段首帧参考，逐段串成长视频。

## 视频结构与拆段

**Shot（镜头）**:
叙事单元。一个镜头包含一段连续的画面/动作/运镜，有明确的起止时刻（`[Shot N]` + 时间戳）。
_Avoid_: 镜头片段、分镜块

**Segment（执行段）**:
实际运行单元。ComfyUI 里的单次 H3/FL2VA 生成，时长必须是 **4-10 秒的整数**（ComfyUI 时长下拉只有整数档）。一个长于执行窗口的 Shot 会被 `split_shot_into_segments` 拆成多个 Segment；段时长为整数秒，**末段吸收余数**（误差集中在最后一小段）。
_Avoid_: 段、Segment 片段、执行块

**分析表（timeline table）**:
带 `[Shot N]` 与时间戳的镜头清单，是 Shot 拆分与分析的对象。
_Avoid_: 分镜表、镜头表

## 桥接机制

**尾帧桥接（bridge frame）**:
把上一执行段的输出视频剥出尾帧图片，作为下一段 H3 的 `first_frame` 参考输入，保证两段画面连续。参考帧**不进成片时间线**——成片用各段视频自身的帧拼接，剥帧选第几帧不影响成片是否跳接。
_Avoid_: 首帧参考、接帧

**尾帧剥离（extract tail frame)**:
从段视频尾部选 **最锐利的一帧**（最后 5 帧候选按 Laplacian 方差取最大）作为桥接帧；全部低于阈值时回退最后一帧并告警。规避生成末帧的压缩伪影/运动模糊导致下一段人物识别失败。

**关键帧实际画面（frame description）**:
用户提交的首帧/尾帧图片经读图模型读出的画面描述，落在 `state["fl2va_frame_descriptions"]`（`role`: first/last）。它是**唯一画面事实源**：分镜表只是计划，用户可能复用旧图或手改图，两者冲突时一律以画面为准——分段请求与分段规划都要注入它，并声明「图中已有的事物不得写成尚未出现/等它登场」。缺读图结果时回退阶段 1 的生图提示词（计划画面）。
_Avoid_: 首帧描述、图片摘要

## 提示词字段

**分段规划（segment plan）**:
长视频阶段 2 的第一步：把整条视频拆成 4-10 秒的整数执行段，输出每段的 start_s/end_s（整数边界）、包含的 Shot、剧情概述和段尾钩子；不写正文。

**段尾钩子（end_hook）**:
**plan 层概念（分段规划产物），不进提示词**。分段规划时给每段写一句「画面在结束时必须达到的具体状态」（谁+位置+朝向+最后半秒动作），用于段边界规划与桥接帧人工验收的对照。段与段之间的画面衔接由**桥接帧图片 + 提示词首行 Picture 1 锚定句**完成，文字不再复用（官方 base-en.txt 无 BRIDGE_FROM/END_HOOK 字段，2026-09-22 迁移）。
_Avoid_: BRIDGE_FROM、提示词末句钩子

**细粒度时间戳**:
段内描述的时间戳允许 0.1s 精度（`At 00:01.200`），但段边界必须是整秒（4-10s，ComfyUI 传参限制）。

**integrated_multimodal_description**:
主描述字段，含 `[Shot N]` 分镜块与时间戳，画面/动作/运镜/对白/同步声都写在这里。全文英文（官方 Output Rules）；实体外观只写在**首次出场的 Shot** 内，未出场实体只在句中否定（如 `No cat is visible in the frame`）或完全不提（官方无 GLOBAL_LOCK 集中定义区）。

**Picture 1 锚定句（anchor sentence）**:
帧变体（I2VA 等）提示词首行的官方对齐指令（如 `For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.`），配合正文里描述段首状态、让文字与桥接帧图片一致；段间衔接的文字侧机制。

**overall_soundscape**:
本段环境声与动作声的英文摘要句（1-4 句连续段落，无时间戳，官方 §4.6）。分段规划时**按段重写**（整条时间轴裁切已废弃）。

**non_diegetic_music**:
观众能听到、角色听不到的背景配乐的英文摘要句（1-3 句，无时间戳，官方 §4.7；无声写 `N/A`）。分段规划时**按段重写**（整条时间轴裁切已废弃）。

## 摘要与核对

**中文摘要（summary）**:
LLM 把完整中文提示词提炼为简明中文：`overall`（整体走向）+ 每个 Shot 一行画面/声音。**整体走向只在分段陪跑前展示一次**，逐镜头清单由每段的 `[本段中文摘要]` 逐段展示，避免重复。
_Avoid_: 中文解说、视频内容走向

## 断点恢复

**阶段 2 断点（stage2 checkpoint）**:
`<generation_dir>/stage2-checkpoint.json`。在 `run_stage2` 的「组装视频正文」完成后立刻落盘 `prompt_draft`；质检循环每轮也更新（status：`qa_round_N`）。崩溃后重跑，检测到该文件存在 → 跳过组装直接进质检精修；阶段 2 完整结束后删除。目的：组装（最贵一步，可能烧 10+ 分钟）崩溃不重跑。
_Avoid_: 断点续跑、恢复点

## 提示词格式纪律（2026-09-22 官方格式迁移）

自创结构 GLOBAL_LOCK / BRIDGE_FROM / END_HOOK / 防波纹咒语（每个 Shot 块尾附加边缘稳定约束句）/ 首行时长句（`This is a N-second continuous shot.`）**已全部删除**：官方 base-en.txt 无这些字段，自创结构偏离模型训练分布导致服从度下降（validator 对其报 error）。段落一致性改由 Picture 1 锚定句 + 首次出场 Shot 内的实体定义 + 桥接帧图片承担。若未来剥尾帧人物识别率下降，**防波纹咒语的删除是第一嫌疑**（恢复一句的成本极低）。