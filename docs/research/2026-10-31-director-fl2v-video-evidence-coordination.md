# 协调笔记：导演台 fl2v 陪跑改造（#111 / #112–#114）

日期：2026-10-31。本文件是协调会话写给实现子代理的**上下文指针**，不是交付物。
权威来源依次是：GitHub issue #111（spec）→ #112/#113/#114（票）→ 本仓代码与测试。
票面与本笔记冲突时，**票面优先**；票面本身有歧义时回来问协调会话，不许自行选边。

## 基线

- 集成分支：`feat/director-fl2v-video-evidence`，从 `fix/fl2v-frame-prompts` 切出，
  含未合并提交 `411eafa`（关键帧提示词按配置帧模型出词等）。**不要再回到 main 取旧实现**。
- 全量基线（改动前，2026-10-31）：`1771 passed / 3 failed / 5 skipped`（108s）。
  三个失败是既有环境项，**不许在本 spec 里顺手修**：
  - `test_loadout_manifest_real_assets.py::test_real_artifacts_carry_a_sampling_config_and_an_unet`
    —— 读本机 ComfyUI 真实产物，现场有一份 24 步的样本；
  - `test_network_guard.py::test_default_refuses_real_connect`
  - `test_network_guard.py::test_default_refuses_socket_connect`
    —— `tests/conftest.py` 里已经没有 `no_real_network` 守卫，但测试文件的注释仍按
    「守卫存在」写（tests 自 2026-10-30 起不入库，这是本机工作区状态）。

## 仓库结构约束（2026-10-30 起）

- git **只跟踪** `src/`、`config/`、`CONTEXT.md`、`CODING_STANDARDS.md`、`README.md`、
  `pyproject.toml`、`examples/`、`launch.py`、`uv.lock`。
- `tests/`、`docs/`、`scripts/`、`AGENTS.md` 物理上在盘上，但**不入库**。
- 因此：工作树里没有 `tests/`。协调会话在创建工作树时已把主树的 `tests/` 复制进去；
  实现子代理在工作树内改 `src/` 与副本 `tests/`，两样都跑。测试改动由协调会话在合并后
  同步回主树（`git` 看不到它们，不会自己合）。
- 工作树内跑测试：
  ```bash
  cd D:/项目/python/MiniMax-H3-prompt/worktree/ticket-NN
  PYTHONPATH="$(pwd -W)/src" D:/项目/python/MiniMax-H3-prompt/.venv/Scripts/python.exe -m pytest -q tests/...
  ```
  判据：`python -c "import minimax_h3_prompt; print(minimax_h3_prompt.__file__)"` 必须指向
  该工作树的 `src`（已验证 ticket-112 可指向本树，49 passed）。
- 源码修改用 git 提交到 `ticket/NN-*` 分支；**永不 push**。测试副本不提交。

## 票面切分与依赖

- **#112 先做**（证据模型 + 账本 + 入口一问）。#113/#114 都以「账内有 video」为前提。
- **#114 次之**（尾帧起点改为上一段账内视频末帧）。
- **#113 最后**（等待视频路径时接受 `e`，改词后只重打导演包；失败菜单三出口）。
- 三张票都顺序做，不开并行工作树：它们都改 `ui/wizard.py` 的同一个陪跑循环与同一批
  测试文件，并行收益小、语义冲突成本高。

## 已定实现口径（协调会话据票面细化，实现时不要改方向）

### #112：每段贴成片视频作唯一证据

1. **判定来源只有视频**。删掉「单段且无缓存才用视频 / 有缓存就数缓存」的分支；
   多段与单段统一：出简报 → 用户跑完 → 贴本段成片视频路径 → 数这条视频的帧 → 判定。
   - 数帧用 `director.cache.actual_frames(video)`（容器头不可信时逐帧解码，读不出抛错）。
   - 判定沿用 `director.companion.judge_single_shot(segment, actual_frames=...)`；
     它的行为就是 `judge_segment` 的「证据已在手」分支（有引导段按导出帧＋容差，
     无引导段必须等于声明帧）。票面要求「沿用单段现用的那条成片口径」，不要另造判据。
   - 路径不存在 / 不是视频 / 读不出帧数：当场报错并**重问**，不落账（`q` 除外）。
2. **账本行**（`director/ledger.py`）：
   - `event=output` 新增 `video`（绝对路径）、`video_sha256`（写入时现算）、
     `source="video"`；
   - `cache` 与 `sha256` **保留**：段缓存目录里确实有该段产物时照记，没有就写 `null`；
     它们**不参与判定**（同一件事不许两个口径）。
   - **旧账兼容是硬约束**：`video` 不要加进 `_REQUIRED_OUTPUT_FIELDS`（加了会让旧账在读口
     抛「行不完整」，与「缺 video＝无证据」冲突）。旧必填集继续校验「行本身坏没坏」；
     「有没有视频证据」在陪跑层单独过滤。`cache`/`sha256` 允许 null，且不得强制对不存在的
     cache 算 SHA。
3. **续跑**：旧账里 `event=output` 但**缺 `video`** 的行视为**无证据**。
   - 不得静默沿用：这些段必须重新贴视频。
   - 必须让用户看见：续跑读取时打印一行点名「第 X 段旧账没有视频记录，需要重新贴视频」。
   - 不许因为旧行缺字段就整本账读失败（那会把用户卡在「连续跑都进不去」）。
4. **入口一问**（spec「入口与级联」）：开跑前「有要重跑的段吗」**只在账里已有通过行时
   出现**；全新开跑直接进第 1 段。续跑且 passed 非空才问。
5. **段简报**：不再出现「段缓存目录 / 数缓存帧数」这类把缓存当判定依据的话。
   多段保留「runSelection 只勾本段」；单段保留「时间线只有一段，直接运行」；
   证据行统一为「跑完把本段成片视频路径贴回来」。
6. **成功后**：账行记视频路径；出站提示可提视频路径，不再说「已记入缓存路径」。
7. **不动**：导演包结构、段规划、尾帧变换、其他两条路线、插件、`exportMode`。

### #114：尾帧变换起点 = 上一段账内视频末帧

1. `ui.wizard._write_director_tailframe` 的起点画面不再取 `cache.frames_path(...)`，
   改为**上一段账内通过行的 `video`** 剥末帧。
   - 「账内」＝ `ledger.passed_director_rows(directory)` 的最后一条有效 output 行；
     本轮刚通过的第 N-1 段也必须能被读到（不要只依赖进入循环时的旧 `passed` 快照）。
   - 末帧导出继续用 `cache.export_last_frame(video, last_frame_png_path(...))`；
     **恒取末帧**，不要引入「最锐利」启发式。
   - 段缓存不存在也能出词（这正是 #111 的现场）。
2. 上一段视频缺失 / 读不出：明确报错并**停在当前段**，不写半份提示词，不拿缓存顶替。
   「停在当前段」的落法＝打印错误后 `return 1` 退出本轮（段未落账，重跑向导会回到这一段），
   **不是** `return 0` 假装正常结束；错误文案要写明「修好后重跑向导会从这一段接着走」。
   顺序必须是**先查已有 `tailframe-NN.md`（沿用）、后取视频末帧**——反过来会让
   「词在盘上、视频被删」的续跑从沿用变成报错。
3. 已存在 `tailframe-NN.md` 仍然沿用（不重付、不覆盖）。
4. 错误文案里的「缓存」改成「视频」（`cache.export_last_frame` 的异常文本也一并统一，
   但别改函数职责）。
5. 级联的派生件清单不变（仍删 `tailframe-NN.md` 与 `frame-KK-last.png`）。

### #113：每段「不满意就改词」（e）

1. 等视频路径的问答接受三种输入：视频路径 / `e` / `q`。
   - 路径：进判定；`q`：停下，不落账，已通过段留账；`e`：进改词。
2. `e` 的一轮改词：
   - **只动画面正文**；时间轴注记、起止状态声明、锁死清单沿用既有组装器与校验器
     （`director.prompts.build_segment_request` → `normalize_segment_output` →
     `validate_director_segment`），实现上建议在 `director/prompts.py` 加一个
     `rewrite_director_segment`（与 `write_director_segment` 同形：直调看门狗 +
     `MAX_SEGMENT_ATTEMPTS` 有界重写）。
   - **导演台段提示词没有「锁死清单」这个产物**：票面的意思是沿用既有锁定口径
     ——注记由代码注入、禁起止状态句、禁对齐行、`At` 时间戳必须落在本段窗口。
     不要新造一份清单文件（会没有消费者）。
   - 改词请求要重建写段上下文：`shot_table` 与 r2v 的 `subject_defs` 从
     `session.stage_state` 取（持久化白名单里有），陪跑目前没读它们，这是最易漏的接线点。
   - 产出不合格按既有重写预算处置；预算用尽交回原文并说明，不静默写坏词。
   - 写回 `director/segment-NN.md`；记一行改写账（`ref_segments.log_segment_rewrite_round`，
     `feedback` 点名「导演台陪跑改词」，与 #108 的「导演台 UI 内修改」区分）。
3. 改完**只重打导演包**：`director.pack.prepare_director_pack(session.directory, plan,
   settings=config.director_settings(), continuity_mode=brief.director_continuity_mode)`；
   不重新规划、不重写其它段。成功后提示「重新导入导演包，并用 runSelection 只勾这一段」，
   回等待。打包失败大声报错并停住（不许带回一个坏包）。
4. 数帧不合格的出口扩成：**重跑本段（再贴路径）/ 改词（e）/ 停下（q）**。
5. 首尾两问（#106）语义不变；`e` 只出现在「轮到某段」的等待里。

## 关键代码锚点（改动前）

- `src/minimax_h3_prompt/ui/wizard.py`
  - `_run_director_segment_handoff`：`1114-1334`（循环、证据分叉、账行写入、首尾两问）
  - `_write_director_tailframe`：`1000-1055`（当前从缓存取上一段末帧）
  - `_render_director_segment_briefing`：`947-984`
  - `_segment_has_cache`：`1101-1112`（#112 后只用于「照记 cache」，不参与判定）
  - `_prompt_existing_file`：`1337-1357`
  - `_resume_director_handoff`：`911-944`
- `src/minimax_h3_prompt/director/ledger.py`
  - `_REQUIRED_OUTPUT_FIELDS`：`42-45`；`log_director_output`：`59-92`；
    `passed_director_rows`：`120-130`；`read_director_progress`：`133-154`
- `src/minimax_h3_prompt/director/companion.py`
  - `judge_single_shot`：`139-160`（#112 复用它，保持行为不变）
- `src/minimax_h3_prompt/director/cache.py`
  - `actual_frames`：`268-290`；`export_last_frame`：`317-355`；`frames_path`/`raw_frames_path`
- `src/minimax_h3_prompt/director/prompts.py`
  - `build_segment_request`：`372-411`；`normalize_segment_output`：`414-447`；
    `validate_director_segment`：`226-311`；`write_director_segment`：`450-481`
- `src/minimax_h3_prompt/director/pack.py`
  - `prepare_director_pack`：`1033-1088`（重打导演包的唯一入口）
- 账本写口：`src/minimax_h3_prompt/ref_segments.py:808-837`（`log_segment_rewrite_round`）

## 测试先例与坑

- 陪跑最高缝：直接调 `ui.wizard._resume_director_handoff`/`_run_director_segment_handoff`，
  用 `monkeypatch.setattr(wizard, "_prompt", fake_prompt)`、
  `monkeypatch.setattr(wizard, "_prompt_existing_file", fake_existing)`、
  `monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)`。
- **作答必须按问题文本匹配，不许往输入表末尾追加**（#57 实测：追加会被上游菜单吃掉，
  流程整体挪一步，甚至会真发 LLM 请求）。空串会被重问，必须给合法答案或 `q`。
- 假 LLM 只需要 `invoke(request, config=None)`；尾帧链路还要桩 `tailframe.describe_image`。
  发网络请求会炸；`tests/conftest.py` 目前**没有**出网守卫，所以更要主动桩。
- `test_director_cascade.py` 与 `test_director_companion.py` 里所有驱动陪跑的用例都会
  受 #112 影响（原来答回车/数缓存，现在必须贴视频路径）。逐条改，别只改新用例。
- 造视频用测试里现成的 `_write_ffv1_mkv`（真 FFV1，能真数帧），或 conftest 的
  `write_test_video`（mp4）。断言用字面量（帧数、路径、菜单文案），不从实现现算。
- 守卫要做变异检验：把「必须有 video 才入账」「e 必须先落盘再回等待」「缓存缺失不再
  拦路」各扰动一次，确认会红。

## 汇报格式

每个实现子代理完成时只回：
1. 改了哪些文件（路径）；
2. 跑了哪些命令、结果（passed/failed/skipped）；
3. 与票面/本笔记的偏差或未决问题（没有就写「无」）；
4. 是否已把集成分支 tip 合进自己的分支。
不要把代码或长日志粘回来。
