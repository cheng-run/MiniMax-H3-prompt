# 导演台 i2v 是单段：一张首帧 → 一段视频

日期：2026-10-30（**修订**当天的第一版）。关系：收窄
`docs/adr/0024-director-route-native-semantics.md` 与
`docs/design/2026-10-29-导演台路线实现设计.md` §3.1「给图清单」表；**取代本文件
2026-10-30 的第一版结论**（「i2v 只允许 opening/continue/ending 三型」）。t2v / r2v /
fl2v 的给图清单与段类型规则一个都不动。

> **2026-10-31 追加（原文不改，留作留痕）**：#112 起「单段 **且** 缓存不存在才取成片
> 视频」的过渡特例作废——多段与单段统一为「每段贴成片视频」，账行新增
> `video` / `video_sha256` / `source="video"`；段缓存只作现场线索照记，不参与判定。
> 下方「单段跑完没有段缓存」一节记录的是 2026-10-30 的现场与过渡形态，历史保留。

## 背景：真实跑撞穿插件契约

第一版按「i2v 只给开场一张首帧，其余段靠段间引导衔接」收窄，依据是设计文档 §3.4.1 的
假设。20 秒 5 段 i2v 包在 ComfyUI 里第一次运行就失败（2026-10-30 12:10:45，
`D:\Comfyui\ComfyUI\user\comfyui_8188.log`）：

```
ValueError: Segment #2 has no source image.
  gen_timeline.py:500 → _build_gen_source_clips → gen_timeline.py:307
```

插件 HEAD `4c6245e` 的三处执行分支（逐行读过，不是推测）：

| 位置 | 判据 | 对本路线的影响 |
|---|---|---|
| `director/gen_timeline.py:22` | `GEN_IMAGE_KEYS = frozenset({"i2v"})` | i2v 恒走 `gen_image` 子模式，不走 `gen_blank` |
| `gen_timeline.py:47-54` + `:393-394` | 包写 `timelineMode="prompt_batch"` → `is_prompt_batch_timeline` 恒真 → `edit_mode` 被**强制**成 `"segment"` | `_resolve_gen_image_ref`（`:126-144`）只读段内 `genImage`／段级 `imageFile`，**「全局一张图」分支对我们是死路**；插件报错文案里的 "global or per-segment" 只有后者可达 |
| `gen_timeline.py:304-310` | 任一段缺源图 → `Segment #N has no source image`（建计划阶段，尚未采样） | 就是上面那次失败；`runSelection` 也救不了（`_gen_segment_ranges` 遍历全部段） |
| `director/executor_core.py:851-856` | `i2v_new_anchor = task_key == "i2v" and first_frame is not None` → `use_motion_context=False` | i2v 段必然有首帧图，于是**段间引导对 i2v 永久失效**；`segment_continuity.py:352-366` 的三个生效条件在这里被直接短路 |

**结论（事实层）**：插件里的 i2v ＝「每段一张独立首帧、各段互不衔接」。它的分段续写、
运镜与切镜只能走 fl2v（`fl2v_timeline.py:63-126` 允许 shot 的首尾帧都为空，
`executor_core.py:849-856` 在引导打开时让首帧让位给上一段尾帧）。

第一版的错在于：把「接口形状能对上」（包里有 `genImage` 字段、段类型看着合理）当成
「执行分支能跑」——从没跑过真实 ComfyUI，测试全用 `tmp_path` 模拟，连「非开场段
`genImage` 为空」都是被用例**断言为正确**的。

## 决定

1. **i2v 是单段模式**：`frames._MODE_MAX_SEGMENTS["i2v"] = 1`，
   `segment_types_for_mode("i2v") = ("opening",)`。整片＝那一段。
2. **整片时长上限 15 秒**（`frames.SINGLE_SEGMENT_MAX_S`，与全仓「单段视频 4–15 秒」
   同值，由用例交叉钉合）。超限在**选完模式之后、阶段 1 之前**就收正——阶段 1 的剧本、
   分镜、生图词都按时长出，跑完再拒等于让用户白付一整轮。
3. **判据只有一处**：`frames.validate_segment_types`。规划解析、`plan.json` 读回、
   打包三处都调它，段数上限与段类型清单一并生效。
4. **规划请求里明写「只能 1 段」**：系统提示词是五型共用一份、不知道模式，不写在这里
   模型会照多段叙事的习惯给五段，然后整份回复被判不合格。
5. **不静默改判**：规划器在 i2v 下判出多段或运镜/切镜段一律**当场报错**，不改写成
   `continue`（那会把一段本要换镜的内容写成连续镜头，属于擅改用户内容）。

## 后果

- 导演台 i2v 的产物是「一张首帧 → 一段 ≤15 秒视频」；超过 15 秒的片子走 fl2v。
  这与路线 1（首帧生成视频）在成片形态上高度重叠——**这是插件的口径，不是我们可以
  挑选的**；要长片、要运镜，插件唯一支持的路是 fl2v。
- `plan.json` 里 i2v 的 `give_images` 恒为 `["first"]`（唯一段就是开场）。
- 旧版留下的多段 i2v 段表在续跑读回时会被拒，报错信息指向「删会话另开」——那些表
  在插件里本来就跑不了，读过去只会再撞一次 `Segment #2 has no source image`。
- 尾帧变换提示词（#107）对 i2v 永不触发（`tailframe.needs_tailframe` 在 i2v 下恒假），
  单段制下这条由「只有 opening」自然落地。
- **单段跑完没有段缓存**，陪跑的验收证据改成成片视频（2026-10-30 真实跑现场）：
  插件的磁盘缓存只服务「分段重跑 / refine / 段间引导」
  （`executor_core.py:106-125` 的 `_segment_disk_cache_needed`），而单段既没有引导
  （`segment_continuity.py:192-207` 在 `segment_count < 2` 时关掉）也没有别的触发条件，
  于是一份缓存都不写。当天实测：`MiniMax-H3视频_00001_.mp4` 243 帧与声明帧一致，
  `output/minimax_seg_cache` 却整个目录不存在，旧陪跑据此报「这一段还没跑」。
  现在 `ui/wizard._run_director_segment_handoff` 对「单段 **且** 缓存确实不存在」的情形
  改问成片视频路径，判据不变（无引导段：实际帧数必须等于声明帧），账行 `cache` 记这条
  视频。若用户在节点上开了 refine / 一采确认，插件仍会写缓存，那时照旧数缓存。
- 2026-10-30 用户同为当日裁定、仍然有效的部分：**i2v 里不做 fl2v 的事**（运镜/切镜
  属 fl2v）；阶段 1 收尾必须出站首帧生图提示词并等图就位。

## 被否决的备选

- **多段 i2v、每段由用户供图**（成片是 N 个独立片段硬切）：插件层面可行，但那是
  「分镜图 → 视频」的另一种产品形态，段类型语义要整体重做（`continue`/`ending` 失去
  依据）。用户 2026-10-30 裁定**先跑通单段**，此案留待日后单独设计。
- **逐段串联**（剥上一段尾帧当下一段首帧，自动改包再跑下一段）：连续性最像长片，
  但要把「一次导入一次跑完」改成「一段一包」，且画质仍不如 fl2v 的潜空间引导——
  是在用工程手段模拟 fl2v 已有的能力。
- **静默把 i2v 多段表改判成 fl2v 包**：用户选的是 i2v，产出 fl2v 时间线属于静默
  改判路线，本仓明令禁止。
- **只改文案、保留多段**：改字不改行为等于把崩溃留给他下一次跑。
