# 导演台 fl2v 陪跑改「只认成片视频」现状勘察（spec #111 / 票 #112 #113 #114）

日期：2026-10-31
范围：只读勘察；为三张票定位现有接线、判据、产物落点与测试先例。
代码基线：分支 `fix/fl2v-frame-prompts`（HEAD `411eafa`）。

> 本文的每条结论都带 `文件:行号`。行号是本轮读树时的实测位置；改动落地后需按符号重新定位，不要照抄行号。
> 术语：**段账**＝`GENxxx/segments/segment-ledger.jsonl`（与多参共用同一本，导演台只写 `output` / `invalidate` 两类行）。

---

## 1. 陪跑循环：入口、主循环、单段/多段分叉、两处提示语

### 1.1 入口

| 路径 | 位置 | 说明 |
|---|---|---|
| 全新流程 | `src/minimax_h3_prompt/ui/wizard.py:567-568` | `brief.is_director` → `_run_director_flow(session)` |
| 规划/写段/打包 | `ui/wizard.py:781-908` | `_run_director_flow`；打包后 `:907-908` 直接调 `_run_director_segment_handoff(..., rotate=True, llm=llm)` |
| 中断续跑 | `ui/wizard.py:551-552` | `status == STATUS_DIRECTOR_SEGMENTS_RUNNING` → `_resume_director_handoff` |
| 续跑函数 | `ui/wizard.py:911-944` | 读 `plan.json`（`:927`）与段账（`:933-940`），再调 `_run_director_segment_handoff(..., passed=passed, llm=llm)`（`:943-944`） |
| 主循环 | `ui/wizard.py:1114-1334` | `_run_director_segment_handoff`：外层 `while True`（`:1203`）＋ `for segment in plan.segments`（`:1204`） |

状态常量：`session_store.py:51`（`STATUS_DIRECTOR_SEGMENTS_RUNNING`）；可续接元组由 `tests/test_director_companion.py:639-655` 的 `test_director_running_status_is_resumable_and_distinct` 钉住。

### 1.2 单段（i2v）与多段（fl2v 等）在哪里分叉

分叉只有**一处语义**，但扩散到四处：

1. `single_shot = len(plan.segments) == 1` —— `ui/wizard.py:1157`。
2. `video_evidence = single_shot and not has_cache_before` —— `ui/wizard.py:1228-1231`。
   - `has_cache_before` 来自 `_segment_has_cache`（`:1101-1111`）：终稿 `.frames.mkv` 或 raw `.pt` 任一存在即算「有缓存」。
   - 因此**只有「单段计划且这一段的缓存不存在」才走贴视频**；单段如果缓存存在，也走数缓存；多段永远走数缓存。
3. 段简报换行：`_render_director_segment_briefing`（`:947-984`），`single_shot` 时印「证据来源：单段计划插件不写段缓存，跑完请把成片视频路径贴回来」（`:971-975`），否则印「段缓存目录 … runSelection 只勾第 N 段」（`:976-982`）。
4. 开跑横幅：`ui/wizard.py:1176-1183`，同样按 `single_shot and not _segment_has_cache(...)` 选措辞。

**“跑完后按回车，我来数该段缓存的帧数”这句在哪**：
`ui/wizard.py:1259-1261`（`else` 分支的 `_prompt`），原文：

> `f"第 {segment.index}/{total} 段在导演台里跑完后按回车，我来数该段缓存的帧数（输入 q 停下）："`

**贴视频路径的问答只在哪条分支**：
`ui/wizard.py:1237-1248` 的 `if video_evidence:` 分支，调用 `_prompt_existing_file(...)`（`:1241-1243`）。问题文本是「第 N/total 段跑完后贴成片视频路径（我数这一段的帧数；输入 q 停下）」。`_prompt_existing_file` 定义在 `ui/wizard.py:1337-1357`：只销引号、判空、判 `is_file()`，`q` 返回 `None`；**不判是不是视频、不判能不能读帧**（后者在 `cache.actual_frames` 才炸，见 §2）。

### 1.3 每段的完整交互次序（现状）

1. 需要终点画面的段先写尾帧变换词（`needs_tailframe` → `_write_director_tailframe`，`:1215-1222`）。
2. 打印段简报一次（`:1223-1236`，`announced` 只印一次）。
3. 等待证据（视频路径或回车），判定（`:1237-1298`）。
4. 通过 → `ledger.log_director_output`（`:1299-1308`）→ 下一段。
5. 全部通过 → 尾问「有要改哪一段重来吗」（`:1316-1329`）→ 交付 `_finish_director_delivery`（`:1334`，定义在 `:1360-1395`）。

**开跑前的重跑一问是无条件的**：`:1185-1190`，不管账里有没有通过行都会问「有要重跑的段吗」。这与 #112「全新开跑不问」冲突，见 §7。

---

## 2. 现有「按成片视频数帧」判定：函数、判据、报错形态

### 2.1 数帧（读盘）

| 函数 | 位置 | 用途 |
|---|---|---|
| `cache.actual_frames(path)` | `director/cache.py:268-290` | **通用**：先 `video_meta` 读容器头帧数，≤0 再 `read_frames_gray` 逐帧解码；仍 ≤0 抛 `ValueError`。单段贴视频走的就是它（`ui/wizard.py:1249-1254`）。 |
| `cache.actual_frames_for_segment(dir, seg)` | `director/cache.py:293-314` | 多段数缓存专用：`.frames.mkv` 不在但 `.pt` 在 → 抛「缓存是 raw 编码，没有 torch」并给处置（`:303-310`）；两者都不在 → 抛「缓存帧文件不存在」（`:311-313`）；否则 `actual_frames`。 |
| `cache.cached_segments(dir)` | `director/cache.py:249-265` | 只认 `seg_NNNN.frames.mkv`，返回 1 基段号升序；目录不在返回空元组。 |

`video_meta` / `read_frames_gray` 在 `tools/frame_match.py:53-67` / `:70-83`：打不开或读不出都**返回空/0**，不抛；所以「不是视频」最终由 `actual_frames` 归到「读不出帧数」那一句。

### 2.2 判定（纯函数）

| 函数 | 位置 | 判据 |
|---|---|---|
| `judge_segment(segment, *, actual_frames, cached_segments, tolerance)` | `director/companion.py:61-136` | ① 段号不在 `cached_segments` → `SEGMENT_NOT_RUN`（空清单）/ `SEGMENT_MISMATCH`（勾错段）；② 有引导段：`actual > export_frames` → `FRAME_OVERFLOW`；`gap=0` → pass；`0<gap<=tolerance` → warn 仍放行；超容差 → fail `FRAME_SHORT`；③ 无引导段：必须**严格等于** `declared_frames`，否则 `FRAME_MISMATCH`。 |
| `judge_single_shot(segment, *, actual_frames, tolerance)` | `director/companion.py:139-160` | 单段专用：`actual_frames is None` 直接 `ValueError`（`:155-158`）；然后**复用** `judge_segment`，但把 `cached_segments=(segment.index,)` 传进去（`:159-160`）——绕开「段在不在缓存里」这一层，因为证据是视频。 |

容差：`DIRECTOR_FRAME_TOLERANCE = 5`（`director/companion.py:32`），配置覆盖口 `Config.director_frame_tolerance()`（`config.py:219-238`）。`tolerance=None` 时**调用时**取常量（`companion.py:73-76`），避免默认参数在定义时求值（注释点名本仓教训）。

结论码与出站文案：`companion.py:38-43`；`DirectorVerdict.passed` 只认 `pass` / `warn`（`:56-58`）。

### 2.3 单段 fl2v/i2v 现在怎么走

- i2v 段表上限 1 段（`director/frames.py:54-56`，`_MODE_MAX_SEGMENTS`），且只允许 `opening`（`:47-49`）——所以「单段」在当前规划器下等价于 i2v。
- 若该段缓存不存在 → `video_evidence=True`，贴视频 → `cache.actual_frames(video)` → `judge_single_shot`（`ui/wizard.py:1237-1257`）。
- 若该段缓存存在（用户在节点上开了 refine / 一采确认）→ 走数缓存分支（`:1258-1289`）。
- 通过行的 `cache` 字段记的是**交回来的视频路径**（单段用例显式断言：`tests/test_director_companion.py:999`）。
- 判定失败菜单：单段 `:1291-1294`「在导演台重跑本段后再贴视频路径；输入 q 停下」；多段 `:1295-1297`「回导演台重跑本段后再回车」。**现状只有一个出口，没有「改词」**。

### 2.4 与 #112 的直接相关点

- 多段要改成贴视频，最省事的复用是 `judge_single_shot`（它已经把「缓存存在性」这一层绕开了）；但 `FRAME_OVERFLOW` 文案仍写「这份缓存不是这一段的产物」（`companion.py:99-103`），`actual_frames` 的报错文案写「段缓存帧文件」「读不出段缓存的帧数」（`cache.py:281`、`:287-289`）——证据换成视频后这些措辞会指错东西。
- `cache.actual_frames` 对「不是视频」的报错是「读不出段缓存的帧数 … 文件可能被截断，或不是 FFV1 无损 mkv」（`cache.py:287-289`），不是「不是视频」。票面要求「当场看到确切错误」，措辞需要按证据来源区分或改成中性。

---

## 3. 尾帧变换提示词链路（#114 的主战场）

### 3.1 触发与产出

| 环节 | 位置 | 说明 |
|---|---|---|
| 触发判据 | `director/tailframe.py:94-117` | `tailframe_segment_types(mode)` 从 `frames.give_images_for` 现算（含 `IMAGE_END` 的段类型）；`needs_tailframe(type, mode)` 在陪跑里调用（`ui/wizard.py:1215`）。`camera_same` / `camera_cut` / `ending` 触发，`opening`（fl2v 下尾帧可选）与 `continue` 不触发。 |
| 装配函数 | `ui/wizard.py:1000-1055` | `_write_director_tailframe`：先看盘上是否已有（`:1019-1027`）；否则取上一段缓存帧文件（`:1029-1039`）→ 导出末帧 png → 用 `replace(plan, segments=(segment,))` 只给这一段写词（`:1043-1049`）。 |
| 起点画面读取 | `director/cache.py:317-355` | `cache.export_last_frame(frames_file, out_path)`：`cv2.VideoCapture` 打开，头可信就 `CAP_PROP_POS_FRAMES=total-1` 读一帧，不可信就整段遍历取最后一帧（`:335-350`）；写 png（`:351-355`）。**恒取末帧，没有最锐利启发式**（最锐利启发式在别处：`tools/frame_auditor.py:267-295` 的 `extract_last_frame(sharpest=...)`，导演台不用）。 |
| 末帧图落点 | `director/tailframe.py:135-141` | `last_frame_png_path(session_dir, segment)` → `GENxxx/director/frame-KK-last.png`，KK 是**被取帧的那一段**（下一段的起点）。 |
| 尾帧文档落点 | `director/tailframe.py:130-132` | `tailframe_path(session_dir, position)` → `GENxxx/director/tailframe-NN.md`，编号跟**段号**。 |
| 写词 | `director/tailframe.py:323-356` | `write_tailframe_prompt`：直调看门狗 `_direct_write`，`MAX_SEGMENT_ATTEMPTS` 次有界重写；调用失败返回 `None`。 |
| 落盘 | `director/tailframe.py:359-418` | `write_tailframe_prompts`：逐段读图（`describe_image`，唯一读图口，`:385-388`）、写词、`path.write_text` 落盘（`:397-399`）；读图/调用失败抛 `RuntimeError` 且**不写半份**（`:387-396`）。 |

### 3.2 为什么读失败会中止整轮陪跑

- `_write_director_tailframe` 在取不到上一段末帧时打印错误并 **`return False`**（`ui/wizard.py:1040-1042`）；
- 主循环看到 False 直接 **`return 1`**（`ui/wizard.py:1220-1222`）——退出整个 `_run_director_segment_handoff`，不是「停在本段再问」。
- 触发条件就是 `cache.frames_path(cache_dir, previous)` 不存在或打不开（`.pt` raw 编码时终稿文件不存在），以及 `export_last_frame` 解不出帧。

### 3.3 已存在时沿用

`ui/wizard.py:1019-1027`：`tailframe_path(...).is_file()` → 读盘、打印「已在盘上，沿用（不重付）」并跑一遍 `validate_tailframe_prompt`，**不读图、不调模型、不覆盖**。对应用例 `tests/test_director_companion.py:880-919`。

### 3.4 与 #114 的直接相关点

- 起点画面只有一条取法（`cache.frames_path` → `export_last_frame`），没有「从账内视频取帧」的分支；要把 `ui/wizard.py:1029-1039` 换成「读上一段通过行的 `video` → `export_last_frame(video, last_frame_png_path(...))`」。
- `export_last_frame` 本身对 mp4/任意 cv2 可解容器都能用（参数名是 `frames_file`，实现不依赖 FFV1）；只需改调用点与文案。
- #114 的「缓存不存在也能出词」在当前实现里恰好不可达：`frames_path` 不在就 `export_last_frame` 抛错 → `return False` → 整轮退出。
- 「上一段视频缺失/读不出时停在当前段」需要决定停法：现状 `return 1` 是**整轮退出（非零）**；票面写「停在当前段」。若沿用「停下不落账、已通过段保留、重跑向导续接」的口径，语义上更接近 `return 0`（参考 `:1244-1248` 的 q 停法）。这是 #114 实现前要裁的一处，见 §7。

---

## 4. 段产出账（导演台段账）

### 4.1 落点与行结构

- 账文件：`ref_segments.segment_ledger_path(directory)` → `GENxxx/segments/segment-ledger.jsonl`（`ref_segments.py:708`、`:725-726`）。导演台**不新开账**（`director/ledger.py:1-19`）。
- 事件常量：`output` / `encounter` / `rewrite`（`ref_segments.py:716-718`）；导演台白名单 `DIRECTOR_EVENTS = (output, invalidate)`（`director/ledger.py:35-38`）。

**通过行（`output`）现有字段** —— `director/ledger.py:79-90`：

```
event="output", segment, start_frame, end_frame,
declared_frames, export_frames, actual_frames,
cache=<绝对路径>, sha256=<写入时现算>, at
```

- 必填字段清单：`_REQUIRED_OUTPUT_FIELDS`（`director/ledger.py:42-45`）＝上面除 `event`/`at` 外的九个。
- `cache` 存绝对路径（`:76-77`），`sha256` 是**那个 cache 文件**的摘要（`:87-88`）。
- 单段视频证据时 `cache` 字段被塞进视频路径（`ui/wizard.py:1299-1308` 传的是 `evidence_path`；用例 `tests/test_director_companion.py:997-999` 断言）。

**目前没有** `video` / `video_sha256` / `source` 字段。

### 4.2 读写函数

| 函数 | 位置 | 说明 |
|---|---|---|
| `log_director_output(...)` | `director/ledger.py:59-92` | 追加通过行；`cache_path` 必填且会现算 SHA。 |
| `log_director_invalidate(...)` | `director/ledger.py:95-109` | 追加作废行（`event="invalidate"`，`segment`/`reason`/`at`）。 |
| `read_director_rows(directory)` | `director/ledger.py:112-117` | 返回所有 `output`/`invalidate` 行，按写入顺序。 |
| `passed_director_rows(directory)` | `director/ledger.py:120-130` | `{段号: 最后一条 output/invalidate 行}`，只保留最后一条是 `output` 的段。**导演台「这段算不算过」的唯一判据**。 |
| `read_director_progress(directory, total_segments)` | `director/ledger.py:133-154` | 续跑读口：① 段号必须在 `1..total_segments`；② 通过行必须写全 `_REQUIRED_OUTPUT_FIELDS`，否则 `ValueError`。 |
| `rotate_director_ledger(directory)` | `director/ledger.py:157-165` | 新交付开头把上一轮账挪成 `.bak`（复用 `ref_segments.rotate_segment_ledger`）。 |

续跑读取点：`ui/wizard.py:933-940`（`_resume_director_handoff`）与 `ui/wizard.py:1164-1170`（主循环内 `passed is None` 时）。跳过已通过段的打印在 `:1208-1212`（会把 `row.get('cache','')` 打出来）。

### 4.3 级联作废与删缓存

| 步骤 | 位置 |
|---|---|
| 波及区间（纯函数） | `director/cascade.py:53-59`（`from_segment..total`） |
| 派生件清单（尾帧文档 + 末帧图） | `director/cascade.py:62-87` |
| 级联总入口 | `director/cascade.py:90-132`（先校验 → 删缓存 → 删派生件 → 对**现在算通过**的段写 `invalidate`） |
| 删单段全部缓存副产物 | `director/cache.py:231-246`（清单来自 `segment_cache_suffixes()` `:171-180`；固定副产物 `:44-50`；一采 `:48-50`） |
| 级联出站文案 | `director/cascade.py:135-168` |
| 主循环调用 | `ui/wizard.py:1080-1098`（`_apply_director_cascade`，返回刷新后的通过表） |

派生件清单细节：尾帧文档 = 第 N..末段（`cascade.py:81-82`）；末帧图 = 第 `max(1,N-1)`..末段-1（`cascade.py:83-86`）。与 #112「级联维持现状」一致。

### 4.4 与 #112 的直接相关点

- **不能把 `video` 直接加进 `_REQUIRED_OUTPUT_FIELDS`**：旧账行会被 `read_director_progress` 判成「账本读不回来」（`director/ledger.py:148-153`），正是 #112 要治的形态。要区分「行不完整 → 报错」与「行完整但没有 video → 无证据、重贴」。
- `passed_director_rows` 会把缺 `video` 的旧行算成通过；主循环 `if segment.index in passed: skip`（`ui/wizard.py:1208-1212`）会静默沿用。需要一个「行里有没有可核的证据」谓词，且只在陪跑层使用（别改 `passed_director_rows` 的通用语义，它还被级联 `cascade.py:124` 用）。
- `log_director_output` 现在**强制** `sha256_file(cache_path)`（`ledger.py:88`），缓存不存在时会抛 `FileNotFoundError`。新签名要允许 `cache_path=None`（缓存路径存在才照记），视频摘要单独算。
- 字段命名冲突：多参的 output 行已经用 `video` + `sha256` 表示视频摘要（`ref_segments.py:759-767`）；导演台行若加 `video` + `video_sha256`，同一本账里 `sha256` 在两种行上分别指「缓存文件」与「视频文件」——报票时要在注释/文档里说清，或统一口径（这是「同一语义两处口径」的候选点）。
- 旧账缺 `video` 的段：票面只说「视为无证据、要求重贴」，没说要写 `invalidate`。若只在前程过滤，账上仍显示通过；若写 `invalidate` 则证据链更干净但要额外一行。这是实现前要定的一处（建议按票面最小口径：不改旧行、只在前程当无证据）。

---

## 5. 「改词」可复用的既有机制（#113）

### 5.1 现成的导演台写段件

| 机制 | 位置 | 说明 |
|---|---|---|
| 写段指令 | `director/prompts.py:111-125` | `load_instruction(mode)` 读 `prompts/segment_writer_director.md` 的 `common` + 本模式小节；缺小节报错。 |
| 请求组装 | `director/prompts.py:372-411` | `build_segment_request(plan, segment, *, shot_table="", sections=None)`：剧情概述、end_hook、时间窗外禁区、分镜窗口、词表切片。**没有 feedback / 修订入参**。 |
| 归一 | `director/prompts.py:414-447` | `normalize_segment_output`：注记按帧注入、时间戳绝对化、模型自写注记剥离。 |
| 校验器 | `director/prompts.py:226-311` | `validate_director_segment`：空段、r2v 六节、非 r2v 不许段头、**禁起止状态句**（`:277-283`）、**禁对齐行**（`:285-290`）、**时间轴注记必须与段表一致**（`:292-297`）、`At` 时间戳必须落在本段窗口（`:299-310`）。 |
| 重写预算 | `segment_prompts.py:817` | `MAX_SEGMENT_ATTEMPTS = 2`（首次 + 1 次重写）；导演台写段在 `prompts.py:463-480` 使用，尾帧在 `tailframe.py:338-355` 使用。 |
| 有界重写循环 | `director/prompts.py:450-481` | `write_director_segment`：调用失败返回 `None`；内容不合格带 issue 清单重写；预算用尽交回原文（不静默写坏词）。 |
| 落盘 | `director/prompts.py:484-515` | `write_director_segments` → `segment_path(session_dir, i)`（`:102-108`）→ `GENxxx/director/segment-NN.md`；先写文件再收集 issues。 |

**锁定块对应物**（#113 说的三样，在导演台的实际形态）：

- **时间轴注记**：模板只住 `_DIRECTOR_TIMELINE_NOTE`（`director/prompts.py:74-77`），`render_timeline_note`（`:148-161`）按帧现算；校验器要求正文本字含它（`:292-297`）。写段时由 `normalize_segment_output` 注入（`:446-447`），模型自写的会被 `_strip_self_written_notes` 剥掉（`:178-183`）。
- **起止状态声明**：导演台**禁止**（`_REF_START_STATE_PREFIX` / `_REF_END_STATE_PREFIX`，校验器 `:277-283`）；判据刻意从多参模块 import（`prompts.py:37-44` 的注释），不在导演台重抄。
- **锁死清单**：导演台**没有**段级锁死清单；唯一同名产物是尾帧文档的 `### 锁死清单`（`director/tailframe.py:63-70`、`:227-234`、`:277-294`）。多参的「锁死声明」在 `ref_segments.py:1193`（`REF_SEGMENT_LOCKED_HEAD`）。**#113 若要求「锁死清单按既有口径锁死」，对导演台段提示词而言应落成「注记 + 禁状态句 + 时间戳窗口」这三条校验，而不是新造一个清单产物。**这是票面措辞与现状的一处需要对齐的地方。

### 5.2 有没有现成的 director 段改写入口？

**没有。** 全仓的段改写入口只有两条，各自绑定路线：

- 多参：`ref_segments.revise_ref_segment_body`（`ref_segments.py:1233-1283`）＋ 请求组装 `build_ref_segment_revision_request`（`:1201-1230`）＋ 向导包装 `_revise_ref_segment_round`（`ui/wizard.py:3654-3714`）。纪律：`body` 与 `locked` 从段文件自己取、只换正文、`assert_locked_annotations_intact`、失败不采用、写回后重过校验、`log_segment_rewrite_round` 留痕。
- 资产图：`asset_loop.rewrite_asset_prompt`（`asset_loop.py:283+`）＋ `ui/wizard.py:2377+` 的 `_rewrite_asset_prompt_round`。

导演台可复用的只有**写段器本身**（`write_director_segment`）与**共享改写账**（`ref_segments.log_segment_rewrite_round`，`ref_segments.py:808-837`），没有「按意见只改正文」的请求组装。若 #113 要「同一份写段指令与同一个校验器」，最直接的实现是：

1. 读盘上的 `segment-NN.md`（当前文本）；
2. 用 `load_instruction(plan.mode)` + 现有 `build_segment_request` 的上下文（`shot_table` 从 `session.stage_state["shot_table"]`，r2v 的 `subject_defs` 从 `state["subject_defs"]`）拼一轮带「用户意见」的请求；
3. 产出走 `normalize_segment_output` + `validate_director_segment`，重写预算 `MAX_SEGMENT_ATTEMPTS`；
4. 失败不覆盖原文件；成功写回并 `log_segment_rewrite_round(... feedback="陪跑里改词" ...)`。

注意 `_run_director_segment_handoff` 目前**没有**读 `shot_table` / `subject_defs`（只有 `_run_director_flow` 在 `ui/wizard.py:807-808`、`:875` 读）。这两个键在持久化白名单里（`session_store.py:74-98` 的 `shot_table`、`subject_defs`），续跑读得回来，所以改词可以在陪跑内重建请求。这是 #113 最容易被漏的接线点。

### 5.3 导演包重建入口

`director/pack.prepare_director_pack(session_dir, plan, *, settings, continuity_mode)` —— `director/pack.py:1033-1088`：

- `settings` 从 `app_config.director_settings()` 取（`config.py:160-196`）；
- `continuity_mode` 从 `session.brief.director_continuity_mode` 取（`ui/wizard.py:895` 的现有调用）；
- 它会**重读全部段提示词**（`:1054` → `_read_segment_prompts` `:485-500`），重建 `pack/` 与 `initial.mmxpack.zip`（`:1082-1084`）。
- 「只重打导演包」＝改完段文件后直接再调它；**不要**再调 `plan_director_segments` / `write_director_segments`。代价：它会读齐所有段词并重写整个包，但不会重付 LLM。

`prepare_director_pack` 的现有调用点：`ui/wizard.py:892-896`（仅此一处生产路径），测试见 `tests/test_director_pack.py:100` 等。

### 5.4 改写账写口

`ref_segments.log_segment_rewrite_round(directory, *, segment, feedback, prompt_before, prompt_after, round_index=None)`（`ref_segments.py:808-837`）：

- 行结构：`event="rewrite"`, `segment`, `round`（缺省按该段已有 rewrite 行数 +1）, `feedback`, `prompt_before`, `prompt_after`, `at`；
- 交付时导演台用它补记「最终包 vs 初始包」的改动（`director/delivery.py:365-367`，`feedback=REWRITE_FEEDBACK="导演台 UI 内修改"`，`director/delivery.py:45-48`）；
- #113 的「陪跑里改词」应复用同一写口，只换 `feedback`（例如「陪跑里改词」），保证两本口径同形。

---

## 6. 测试夹具与先例

### 6.1 `tests/test_director_companion.py`（陪跑逐段驱动，1002 行）

| 夹具/工具 | 位置 | 用途 |
|---|---|---|
| `_write_ffv1_mkv(path, frames)` | `:35-52` | 用 `imageio_ffmpeg` 现造**真 FFV1 mkv**（逐帧变化像素，避免解码器优化成单帧）。 |
| `_config_with(monkeypatch, director_raw, *, comfy_env)` | `:449-460` | 猴补 `config._load_yaml` 只返回 `{"director": ...}`；不读仓里 YAML、不发网络。 |
| `_director_brief(**overrides)` | `:553-561` | 造 `Brief(mode="director", director_mode="t2v", duration=30, ...)`。 |
| `_plan_types(types, *, mode)` | `:564-585` | 按段类型造真段表（帧数走 `frames.export_frames_for_declared`）。 |
| `_director_session(tmp_path, types)` | `:588-604` | 写 `plan.json` + 每段 `segment-NN.md`，返回 `STATUS_DIRECTOR_SEGMENTS_RUNNING` 的 `SessionState`。 |
| `_cache_dir_for` / `_plant_cache` / `_run_config` | `:607-622` | 缓存目录放 `tmp_path` 下；`comfy_output_root` 指到 `tmp_path`。 |
| `_is_rerun_question` / `_is_segment_question` | `:625-636` | **按问题文本匹配**作答，不按调用次序数（#57 教训）。 |
| `fake_prompt` 模式 | `:672-679` 等 | `monkeypatch.setattr(wizard, "_prompt", fake_prompt)`；`_drain_stdin` 桩成 no-op（`:669`）。 |

驱动方式：直接调 `wizard._resume_director_handoff(config, session)` 或传 `llm=` 桩；断言 `capsys` 出站 + `ledger.passed_director_rows` 行内容。代表用例：

- `:658-694` 全段通过落账（种缓存、空回车）。
- `:697-730` 勾错段报红后补跑。
- `:733-766` 续跑跳过已通过段。
- `:769-785` `q` 停下不落账。
- `:788-830` raw `.pt` 处置（**#112 后这条生产路径会消失**，见 §7）。
- `:833-877` 尾帧词接线（桩 `tailframe.describe_image`、`_TailLLM` 有 `invoke`）。
- `:880-919` 已有尾帧词沿用。
- `:926-944` `judge_single_shot` 纯函数。
- `:947-1001` 单段贴视频（桩 `wizard._prompt_existing_file`；断言账上 `cache` 是视频路径）。

### 6.2 `tests/test_director_pack.py`（包与媒体落点，1177 行）

- `_plan(mode)`（`:48-51`）用 `parse_director_plan(JSON, total_s, mode=...)` 从字面量 JSON 建表；
- `_write_prompts(session_dir, plan, texts)`（`:54-60`）直接写 `segment-NN.md`（打包器只读不生）；
- `_session`（`:63-66`）、`_settings`（`:69-74`，`DirectorPackSettings(width=736, height=1280, ...)`）；
- 调用 `prepare_director_pack(session_dir, plan, settings=...)`（如 `:100`），断言 zip 名单（`_zip_names` `:77-79`）与 `timeline.json`（`_pack_timeline` `:82-83`）。
- 适合 #113「只重打导演包」：先写段词 → 打一次 → 改段文件 → 再打一次 → 断言 `timeline.json` 里该段 `prompt` 变了、其它段没变。

### 6.3 `tests/test_wizard_last_frame.py`（收帧问答，338 行；base/FL2VA 路线，不是导演台）

| 工具 | 位置 | 用途 |
|---|---|---|
| `_answers(monkeypatch, sequence)` | `:65-68` | 用 `iter(sequence)` 顺序喂 `wizard._prompt`；`_confirm` 固定。 |
| `_patch_strip` | `:55-62` | 桩 `frame_auditor.extract_last_frame` / `describe_bridge_frame`，并桩 `wizard._gate_segment_handoff`。 |
| `_video(tmp_path, name)` | `:71-75` | 写一个**真文件**（内容假），因为账要算 SHA256。 |
| `_log(session)` | `:78-82` | 读 `segments/gate-log.jsonl`。 |

用途：先例价值在「顺次答问 + 桩剥帧 + 真文件当视频证据」；**不是**导演台的贴视频用例（导演台的贴视频先例在 §6.1 的 `:947`）。

### 6.4 `tests/test_director_cascade.py`（802 行）

- `_plant_segment(cache_dir, seg)`（`:32-45`）：造该段全套副产物。
- `_record_pass(directory, cache_dir, seg)`（`:136-152`）：写通过行；缓存文件已存在时不覆盖（注释说明：覆盖会把能真取末帧的 FFV1 盖掉）。
- `_passed_five(tmp_path)`（`:155-162`）：五段全通过。
- `_write_ffv1_mkv` 在本文件**另抄了一份**（`:443`）——与 `test_director_companion.py:35` 重复。若要加视频 fixture，注意别造第三份。
- 适合 #113/#114 的级联断言：改词/重跑后作废范围只从第 N 段起、派生件（`tailframe-NN.md`、`frame-KK-last.png`）按清单删。

### 6.5 其他相关

- `tests/test_director_i2v_contract.py:94-123`：i2v 单段打包契约，直接调 `prepare_director_pack`。
- `tests/test_director_tailframe.py`：尾帧词纯函数/写盘（`:308` 逐段写、`:374` 起点图缺失报错、`:397` 读图失败报错）。#114 若改起点来源，这一组仍是下游行为的守卫。
- `tests/conftest.py`：`isolated_asset_library` 自动把素材库指到临时目录（`:102-117`）；`write_test_video`（`:39-71`）写 mp4（H.264），导演台数帧用例**不能**用它代替 FFV1（原因见 `test_director_companion.py:35-40` 的注释）。

---

## 7. 与 #111 描述冲突、或需要先裁的接线点

按「会改变行为/会让用例红」的次序排：

1. **开跑前的重跑一问是无条件的**（`ui/wizard.py:1185-1190`），#112 要求「只在账里已有通过行时出现」。注意这一问排在轮转（`:1159-1162`）与读账（`:1164-1170`）之后，判据应当用**这一轮读到的 `passed`**，不要用轮转前的账。
2. **旧账缺 `video` 不能读失败**。`read_director_progress` 会把缺 `_REQUIRED_OUTPUT_FIELDS` 的行判成账本损坏（`director/ledger.py:148-153`），现有用例 `tests/test_director_companion.py:402-415` 就钉着这个行为。若把 `video` 加进必填清单，旧账整本读不回来——与 #112 的「不再因缺少新的必填字段把整本账读失败」直接冲突。要实现「缺 video = 无证据」，但保留「缺 cache/actual_frames = 行不完整」的报错。
3. **主循环的跳过判据只看段号**（`ui/wizard.py:1208-1212`），会把缺 `video` 的旧行当通过静默跳过。需要在前程加「这一行有没有视频证据」的判断；不要改 `passed_director_rows` 的通用语义（级联 `cascade.py:124` 也在用）。
4. **`_segment_has_cache` 与 `video_evidence` 的分叉会整体废弃**（`ui/wizard.py:1101-1111`、`:1228-1231`）。#112 要求多段与单段走同一套贴视频问答；`single_shot` 只剩下的用途是简报措辞（`:971-975`）。现有用例 `test_handoff_surfaces_the_raw_codec_disposal_instead_of_not_run`（`tests/test_director_companion.py:788-830`）断言的正是「多段数缓存遇 `.pt` 打处置」——改成贴视频后，这条生产路径不再存在，用例要重写或删除；`test_handoff_passes_every_segment_and_records_the_ledger`（`:658`）等所有「空回车＝通过」的用例都要改成喂视频路径。
5. **`log_director_output` 强制对 cache 算 SHA**（`director/ledger.py:87-88`），缓存不存在会抛 `FileNotFoundError`。新字段设计要允许「缓存存在才照记」，视频摘要另算；不能把 cache 设为必填。
6. **字段口径冲突**：多参 output 行已有 `video` + `sha256`（`ref_segments.py:759-767`）；导演台新增 `video` + `video_sha256` + 保留 `cache` + `sha256`，同一本账里 `sha256` 在两种行上指不同文件。要么在行里显式写 `source`（票面要求）并在注释里说明，要么统一摘要键名——否则「同一语义两处口径」会在这里再长一次。
7. **尾帧失败现在的停法是整轮 `return 1`**（`ui/wizard.py:1220-1222`），票面 #114 写「停在当前段」。两种解释都成立（「不再往下跑」vs「停下且可续接」），但返回值与用户看到的提示不同；实现前要裁。参照：q 停下用 `return 0` 且保留进度（`:1244-1248`）。
8. **尾帧起点只有缓存一条路**（`ui/wizard.py:1029-1039`），必须换成读账内上一段 `video`；但要注意「已存在的尾帧词优先」（`:1019-1027`）——如果先查视频再查词，会让「词已在盘上、视频被删」的续跑场景从「沿用」变成「报错」，与 #114 的「已存在则沿用」冲突。**顺序必须保持先查词、后取帧。**
9. **级联文案与 #112/#113 冲突**：`render_cascade_notice` 仍说「跑完在陪跑里按回车继续」（`director/cascade.py:162-163`，改后应为贴视频）、「改词重跑：在**导演台 UI 里**改」（`:164-166`，改后应指向陪跑内的 `e`）。这两句是用户唯一的操作指路，必须跟着改。
10. **判定文案指错证据**：`FRAME_OVERFLOW` 说「这份缓存不是这一段的产物」（`director/companion.py:99-103`）；`actual_frames` 说「段缓存帧文件」「读不出段缓存的帧数」（`cache.py:281`、`:287-289`）。证据换成视频后，这些句子会在真实报错里指错东西；#112 的「确切错误」要求它们至少不把视频叫成缓存。
11. **改词上下文在陪跑里没有读**：`_run_director_segment_handoff` 不读 `shot_table` / `subject_defs`（`_run_director_flow` 才读，`ui/wizard.py:807-808`、`:875`）。#113 的改词要重建写段请求，必须从 `session.stage_state` 取这两个键（白名单里有：`session_store.py:74-98`）。
12. **「锁死清单」在导演台段提示词里不存在**（§5.1）：票面若按多参/尾帧的「锁死清单」字面实现，会造出一个没有消费者的新产物。导演台的锁定实际是「注记由代码注入 + 禁起止状态句 + 禁对齐行 + 时间戳窗口」，校验器 `validate_director_segment` 已经全有（`director/prompts.py:226-311`）。
13. **交付时的改写账与陪跑改词共用同一本账**（`director/delivery.py:365-367`）：陪跑改词若在重打导演包之后写回段文件，最终包 vs 初始包的比对会以**重打后的包**为「初始」，不会把这些改动再记一遍；`feedback` 要能区分两种来源（`REWRITE_FEEDBACK` 现为「导演台 UI 内修改」）。这符合 #113「同口径」，但注释要写明：陪跑改词不是靠交付比对发现的。
14. **尾帧词的起点来源与级联派生件同号**：`last_frame_png_path(KK)`（`tailframe.py:135-141`）与级联删除区间（`cascade.py:83-86`）已经对齐；改起点来源不应改文件名／区间（票面也说不改清理清单）。
15. **`judge_single_shot` 的名字/文档与「多段也用视频」冲突**：函数本身可复用（它只是把缓存存在性绕开），但名字与 docstring 会让后来人以为只有单段能用；#112 落地时要么改名/改文档、要么在陪跑层包一层，别留「名字说单段、产线拿它判多段」的口径分裂。
16. **测试基线与耗时**：改陪跑问答会带走一批既有用例（§7 第 4 条），历史教训是「全绿但耗时暴增／真发请求」；跑全量时同时看耗时，不要只看红绿（`wizard-new-step-breaks-old-tests` 记忆）。本机已知环境性失败按用户口径一句话带过。

---

## 附：最关键的 5 个锚点

1. `src/minimax_h3_prompt/ui/wizard.py:1114-1334` —— 陪跑主循环；单段/多段分叉在 `:1157` 与 `:1228-1231`；「按回车数缓存」在 `:1259-1261`；贴视频只在 `:1237-1257`。
2. `src/minimax_h3_prompt/ui/wizard.py:1000-1055` —— 尾帧词接线：起点取上一段缓存帧（`:1029-1039`），失败 `return False` → 主循环 `return 1`（`:1220-1222`）；已有则沿用（`:1019-1027`）。
3. `src/minimax_h3_prompt/director/ledger.py:59-92` / `:133-154` —— 通过行字段与续跑读口；`_REQUIRED_OUTPUT_FIELDS`（`:42-45`）是旧账兼容的关键闸门。
4. `src/minimax_h3_prompt/director/companion.py:61-160` —— `judge_segment` / `judge_single_shot`；`judge_single_shot` 用 `cached_segments=(index,)` 绕开缓存存在性（`:159-160`），是多段贴视频可直接复用的判定口。
5. `src/minimax_h3_prompt/director/pack.py:1033-1088` —— 「只重打导演包」的唯一入口 `prepare_director_pack`；改词后的重建就调它，不要回到规划/写段全流程。
