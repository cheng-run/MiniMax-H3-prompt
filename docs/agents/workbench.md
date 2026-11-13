# 工作台会话与 Headless API（D2–D7）

> 入口：`python -m minimax_h3_prompt.api`（开发形态只绑 `127.0.0.1:8000`）；冻结形态见下文「冻结形态（EXE）」。
> spec #214 / #227 / #235 / #243 / #252，ADR 0032、0033、0034、0035、0036、0037；
> 本地鉴权见 #222 §4 与 ADR 0033。
> 这张表是导航与不变量，不是教程；实现细节以代码注释为准。

## 模块地图

| 模块 | 职责 |
|---|---|
| `workbench_store.py` | 会话仓库：`workbench.v1` 快照、原子写、每会话线程锁、`mutate`、事件日志 `EventLog`、幂等登记表、产物登记 |
| `api/jobs.py` | `JobRunner`：每会话 asyncio 锁、`to_thread` 跑 LangGraph、节点边界取消、`active-job.json`、启动对账、阶段 1/2 执行 |
| `api/routes/sessions.py` | `/api/v1` 全部会话端点；**路由顺序即契约**：`{session_id:path}` 会吞斜杠，`/jobs`、`/events`、`/action-items` 必须注册在详情路由之前 |
| `api/routes/assets.py` | 上传素材的收、列与删除（`POST/GET/DELETE /sessions/{id}/assets`）；整只路由器在 `create_app` 里排在 `sessions` 之前，顺序不靠人记 |
| `api/routes/media.py` | 素材字节读取（`GET/HEAD /media/{asset_id:path}`）；同样排在 `sessions` 之前 |
| `api/media.py` | 单段 Range 解析、416 与字节流响应；素材与成片的 content 端点**共用这一份** |
| `diagnostics.py` | 诊断包组装（manifest/环境/脱敏配置/日志窗口/会话/文本产物）；纯文件与字典操作，不依赖 FastAPI |
| `api/routes/diagnostics.py` | `GET /api/v1/diagnostics/bundle`：唯一新增的 D5 端点，`application/zip` + `attachment`，不在鉴权豁免面上 |
| `logging_setup.py` | 服务进程日志：`logs/app-YYYYMMDD.log` 按日滚动、保留 N 天（`logging.retention_days`）、写入前按值脱敏 |
| `api/auth.py` | 本地访问控制：Host/Origin 白名单、每进程启动令牌、`LocalAccessMiddleware`（纯 ASGI，最外层） |
| `api/routes/auth.py` | `GET /api/v1/auth/token`：返回令牌并下发 `wb_session` Cookie |
| `api/app.py` | 工厂、lifespan（启动对账）、注入 `sessions_root` / `runtime_config` / `local_access` |
| `bootstrap.py` | 首次启动把内置配置模板复制进 `<数据根>/config/`（#238）；只由启动入口在 import `api.app` **之前**调用，已存在不覆盖，开发形态是空操作 |
| `paths.py` | 唯一路径解析口（#237）：只读资源（`_MEIPASS` / 仓库根）与可写数据（`data/` / 仓库根）分行；密钥文件按形态分流（冻结态 `%APPDATA%\MiniMax-H3-prompt\.env`，开发态仓库 `.env`，#238） |
| `api/problems.py` | RFC 9457 problem+json；所有错误都走这里（中间件用 `build_problem_response`） |
| `step_machine.py` | 状态/触发/闸门的单一声明；续跑起点；`resolve` 的 approve/revise 去向与 approve 按变体分叉（ref 走 `asset_loop`） |
| `session_store.py` | 快照文件读写、`atomic_write_text`、向导续接状态与 `RESUMABLE_STATUSES` |
| `api/fake_pipeline.py` | 确定性桩管线：固定阶段 1/2（含 ref 的资产图词与校对结论）、假读图与假段提示词/段判据/段视频造件，**只认环境变量 `MINIMAX_FAKE_PIPELINE`**（默认关、无 HTTP 入口），真实/桩互斥 |
| `segment_validation.py` | 段交接的两道判据：同步提交自检（段号/技术档，`segment_submission_problems`）与语义判据（尾帧描述 vs `end_hook`，`judge_end_hook`）；读图与模型调用在 `api/jobs.py` |
| `ref_segments.py` | 多参分段：`prepare_ref_segmentation`（切点/闭包/重编号/段表/顺序卡/四列分镜表）、`RefSegmentJob.write_segment`（单段落盘 + 段级注入卡）、`is_single_segment_route`（≤ `MAX_SEG_S` 即「1 段」） |
| `segment_refs.py` | 段内参考素材子集：`compute_segment_scope` → `compute_asset_closure` → `demote_consistency_anchors`，结果随段只读回显 |
| `injection_card.py` | 注入卡渲染（整片 / 段级）：给人照抄的接线清单，只显示用量、不做容量拒绝 |
| `ref_capacity.py` | 多参容量外部契约 9/3/3/12；`asset_ledger.py` / `asset_loop.py` / `asset_proofread.py` 是 CLI 侧台账、循环与校对实现——工作台复用校对判据，不读 CLI 的 `asset-ledger.json` |
| `frontend/src/features/sessions/` | 工作台五分区：`SessionDetailView`（壳/SSE/运行控制）、`ReviewTab`（审查与修订）、`AssetsTab` + `FrameSubmissionCard`（素材与帧提交）、`SegmentsTab` + `SegmentCard`（长片分段只读展示、`segment_video` 回交、ref 段内素材只读回显与「换一段」）、`ArtifactsTab`（产物查看/复制/下载/通用媒体播放） |
| `frontend/src/api/client.ts` | 生成契约的类型出口与唯一 API 门面：令牌注入、媒体/产物 URL、错误归一成 `ApiError` |
| `scripts/accept_d2.py` | 真实 HTTP 验收驱动器（手动，不进 CI；启动时先取令牌） |
| `scripts/accept_d3.py` | 媒体与素材的真实验收驱动器（手动；真实照片上传 + 真实 `final.mp4` 的 Range 与字节核对，报告落 `output/accept-d3/`） |
| `scripts/accept_d4.py` | D4 真实浏览器验收驱动器（手动；Playwright + 真实 LLM 走审查/修订/帧提交/阶段 2/复制，报告落 `output/accept-d4/`） |
| `scripts/accept_d6.py` | D6 真实长片验收驱动器（手动；真实 LLM + 用户 ComfyUI 生成的真段视频走分段规划/回交/读图/拼接，报告落 `output/accept-d6/`） |

## 不变量（改这些之前先读）

- 事件类型是**十种闭合集合**；加字段可以，改语义不行。`seq` 会话内从 1 严格单调，只在会话锁内分配。
  **D4 之后仍然闭合**：帧提交复用 `action_item.created`，approve 按变体分叉也不新增类型。
- **`stage_state` 不出 API**（D4）：阶段 1 的中间产物落成登记表里的产物，前端经 content 端点读正文；
  快照、待办载荷与错误体都不带 state 内部键。revise 重跑原地更新文件，产物清单是「显示哪些文件」的唯一事实。
- **产物分区不按变体判断**：登记表里有哪几件就显示哪几件（T2VA 两件、帧链多两帧词）；
  复制/下载都走 content 端点（下载是 `<a download>` + Cookie），媒体按 `kind` 通用渲染，
  不按文件名特判成片。阶段 2 成功即 `completed`，**没有交付确认闸门**（ADR 0034）。
- **先写事件、后写快照**；快照 `last_event_seq` 落后时按 `seq > last_event_seq` 重放，重放幂等。
- 每会话**单写入者**：任何「读 → 改 → 写」都走 `WorkbenchSessionStore.mutate`（锁在存储层，别在调用方自己拼）。
- `session_id` = `sessions_root` 下的相对路径；`_resolve` 拒绝绝对路径、`..` 与根外路径。
- legacy（`wizard_session.v1` 或无 `session_id`）只读：可列/可查/可列产物，起 job 回 409 `session_legacy`。
- 取消是协作式：只在节点边界生效，job 记 `cancelled_at_node`；**不要承诺立即停止**。
- 启动对账只把 `running` 收成 `interrupted`，**不自动续跑**。
- 阶段 2 中断后再 `advance`：按 `stage1_review` 的决议事实续跑阶段 2（`_resume_step`），不回人工闸门。
- **长片的段状态住快照**（`session.segments`，D6 §10）：段号、状态、提示词产物 id 与已交
  视频素材 id 都是快照字段，**每次段落定（通过/被拒/重建待办）立刻落盘**；事件重放不补它。
  重启后从第一条未通过的段继续——已通过段不重跑、不重收视频，`validating` 重跑校验
  （素材不重交），`pending` 重起写词 job，`waiting_video` 的待办本就在快照里、
  重启后重新呈现（不新建）。
- 工作台**不读也不写** CLI 的段产出账 `segments/shot-ledger.jsonl`（D6-AC12）：同一会话
  目录曾被 CLI 跑过时，进度只以工作台自己的段状态为准。
- **全部段通过后服务端自动拼接**（D6 §9，`segmenting` 阶段里的一步）：复用
  `final_film.assemble_film`——`plans` 取本会话 `segments/plan.json`，`rows` 由段状态里的
  已通过段与视频素材构造。成功产出 `final.mp4` 并转 `completed`（**没有交付确认闸门**）；
  缺段则 `failed`，`assemble_film` 的 reason（已点名第几段）原样进 `job.failed` 的
  `detail`，补齐后 `advance` 续拼，已通过段不重收。拼接口径是**路线属性**：base 裁
  第 2..N 段首帧（帧链段间共享首帧），ref 走 `STREAM_COPY_FIRST`（多参段彼此独立，
  裁首帧只会吃掉用户交的帧）。
- 产物 content **按登记表的形态分支**（#225）：文本产物（登记表＝10 件文本固定名
  `video-prompt.md` / `script.md` / `shot-table.md` / 两个帧提示词 / 两份摘要 /
  `injection-card.md` / `segments/order-card.md` / `segments/ref-shot-table.md`，
  D4 §7 / D7 §9 / §11，加四条
  模式 `segments/shot-NN.md` 与 `segments/segment-NN.md` → kind `segment_prompt`、
  `assets/picture-NN.md` → kind `asset_prompt`、`segments/injection-card-NN.md` → kind
  `segment_injection_card`，D6 §7 / D7 §7 / D7 §9 / D7 §10）共用纯文本 + 1 MiB 上限
  （超出 413）；成片 `final.mp4`（第 11 件固定名，媒体形态）给字节 + 单段
  Range；两者都答 `HEAD`（同头、无体）。「算不算产物」「按什么形态读」「清单从哪来」
  三个判断只有一个入口 `workbench_store.artifact_spec`，端点与清单都不抄第二份白名单。
- **注入卡是只读产物、绝不进模型载荷**（D7 §9 / #257）：整片卡 `injection-card.md`
  （kind `injection_card`）在素材循环全部定稿之后、阶段 2 开始之前由
  `JobRunner._write_film_injection_card` 自动产出（入口只有 `_execute_stage2` 一处，
  不单独占 step；写失败即 job 失败）。段级卡走唯一入口
  `JobRunner._write_segment_injection_card`（接线在 #258），落 `segments/injection-card-NN.md`
  → kind `segment_injection_card`，在分段分区与段词同屏；没有该文件就没有卡块。
  卡面只显示用量、不做容量拒绝（闸门仍只有 §8 的两处）；页面没有编辑入口，重跑直接重算。
- **媒体读取**（#225 / D3-3）：`GET` 与 `HEAD` 分开注册——HEAD 的**成功**响应在契约里
  不声明响应体（错误响应两条路径同形，都是 problem+json）；
  固定带 `Accept-Ranges: bytes`、`ETag`、`Last-Modified`、`Content-Length`、`Content-Type`
  与 `Content-Disposition: inline; filename*=UTF-8''…`。Range 只支持**单段**
  （`a-b` / `a-` / `-n`）：越界、语法非法、多段一律 416 problem+json，并带
  `Content-Range: bytes */<len>`；`Range` 的单位不是 `bytes` 时按 RFC 忽略、回整份。
  素材标识只认 `<会话标识>/<16 位内容哈希>`（无扩展名，见 §5.4），扩展名是落盘形态。
  成片的 `ETag` 取 `mtime_ns-大小`：为一次请求去哈希几百 MiB 不值（素材侧才是内容哈希）。
- **上传素材**（D3-2）是 `<会话目录>/assets/<sha256 前 16 位>.<嗅探扩展名>` 加一份
  `.json` 边车（显示名、类型、字节、首次创建时间），清单靠目录现扫 + 命名规则——
  `.tmp-*` 不是素材，列表看不见，只在启动时删；同内容不写第二份。上传**不改会话
  状态、不发事件**，残片由启动清理收拾（`store.cleanup_assets`）。
- **参考素材台账**（D7-2 / #255）住 `SessionState.asset_ledger`（会话文件是唯一真源，
  ADR 0032），不另写 `asset-ledger.json`：`name / kind / number / asset_id / sha256 /
  round / status`。同一个上传端点的两种用法只差一个判据：**带语义名 = 登记参考素材**
  ——会话内唯一、类型由嗅探定、同类编号从 1 起、容量按台账同步判 9 图 / 3 视频 /
  3 音频 / 12 文件（422 `ref_capacity_exceeded`）、**起跑（离开 `created`）即锁登记**
  （409 `asset_locked`）；**不带语义名 = 普通素材**——ref 与 base 一致，不记账、不编号、
  不占容量、不受起跑锁，它就是素材图与段视频回交的通道（#256/#258）。base 会话带语义名
  → 422 `asset_name_invalid`（没有台账可登记）。删除是
  `DELETE /sessions/{id}/assets/{asset_id}`，只撤销台账登记（台账外 → 404
  `asset_not_found`，base 调用 → 409 `route_mismatch`）；详情里的 `brief.refs` 就是
  台账摘要。

## 素材循环（D7-3，ADR 见 D7 的「ref 多参进工作台」）

`ref` 在阶段 1 与阶段 2 之间多一档 `asset_loop`（阶段标签，不是会话状态）。

| 步骤 | 类型 | 阶段标签 | 产物/状态 |
|---|---|---|---|
| 阶段 1 | job | `stage1` | `script.md` / `shot-table.md`；审查待办 `stage1_review` |
| 第 N 件出词 | job `asset_loop` | `asset_loop` | `assets/picture-NN.md`（kind `asset_prompt`）；挂 `asset_image` 待办 |
| 第 N 件交图 | 待办 `asset_image` | — | 载荷 `asset_id`；会话 `awaiting_human` |
| 第 N 件校对 | job `asset_loop` | `asset_loop` | 读图（`describe_image`）对照这一件的词；通过转 `passed` |
| 阶段 2 | job | `stage2` | 全部图片定稿后自动接上（注入卡是 #257） |

- **ref 通过审查不直接进阶段 2**：`resolve` 的 approve 分支按 `brief.is_ref` 分叉，
  `_resume_step` 在「阶段 1 已 approve」与「阶段 2」之间插入 `asset_loop`；base 一字不变。
- **严格串行、一次一件**：当前件号＝台账里**最大连续通过编号 + 1**（台账是唯一真源，
  重启后照样重建）；每件同时最多一个未决 `asset_image`。
- **没有放行键**：`asset_image` 的唯一决议是 `asset_id`；校对不通过时用
  `rewrite_asset_prompt` 按差异清单改词、**覆盖同一份** `assets/picture-NN.md`，
  同一件号重开待办（`reason` 带差异清单）。台账只记轮次与状态，历史不靠多份文件。
- **交图标识住台账条目**（`proofread_asset_id`）：崩在「已交图、未校对」之间时，
  恢复跑校对而不是要求重交；判过即清。
- **资产图词按语义名取**（`generation.asset_prompt_for` → 阶段 1 的
  `asset_prompts_for`）：名字对不上是硬失败（`AssetPromptMissing`），不拿相邻件的词顶替。
- **台账 → 管线的 brief.refs 只有一处**（`JobRunner._ref_brief`）：工作台的参考素材不在
  brief 文件里，阶段 1/2 与修订重跑交给管线的 `brief.refs` 都由台账现造（只收 Picture，
  `path` 留空）；漏接一处，分镜表 Subject 标签与资产图提示词命名就与上传的语义名脱钩。
  派生出来的 refs **不写回会话文件**（台账是唯一真源）；base 会话原样返回。
- 桩管线**覆盖到 ref 素材循环**（#259）：`MINIMAX_FAKE_PIPELINE=1` 下阶段 1 按台账
  语义名产出资产图词，校对 job 伪造结论（不调 `describe_image`、不建模型）——第 1 件
  的第一轮固定不通过、其余通过（判据是台账轮次，重跑不换结论）。桩只换「谁写/谁判」，
  产物落盘、台账转移与待办重开都走真实那一份代码。开关关掉时没有任何一条路径会伪造
  结论：出词走 `asset_prompt_for`，名字对不上就硬失败。

## 长片分段陪跑（D6，ADR 0036）

`duration > 10` 秒即长片（`step_machine.is_long_form`）。长片**不替代阶段 2**：阶段 2 照旧产出
`video-prompt.md` 与中文摘要，成功收尾时接上分段循环，会话保持 `running`。

| 步骤 | 类型 | 阶段标签 | 产物/状态 |
|---|---|---|---|
| 阶段 1 | job | `stage1` | `script.md` / `shot-table.md`；审查待办 `stage1_review` |
| 阶段 2 | job | `stage2` | `video-prompt.md` + 摘要；长片收尾接分段规划 |
| 分段规划 | job | `segmenting` | 段列表进快照，`segments/plan.json` 落盘；只读展示、**不要求确认** |
| 第 N 段写词 | job | `segmenting` | `segments/shot-NN.md`（kind `segment_prompt`），段转 `waiting_video` |
| 第 N 段回交 | 待办 `segment_video` | — | 载荷 `video_asset_id`；会话 `awaiting_human` |
| 第 N 段校验 | job | `segmenting` | 同步技术档（resolve 时 422）+ 异步剥尾帧读图；通过转 `passed` |
| 拼接 | job | `segmenting` | 全部 `passed` 后自动 `assemble_film` → `final.mp4` → `completed` |

- **`segmenting` 是阶段标签，不是会话状态**：会话仍用七态；「等用户交视频」是 `awaiting_human`。
- **`segment_video` 是第三个人工待办**：每段一个，`segment_index` 带段号；resolve 只收
  `video_asset_id`（本会话已上传的 mp4 素材），与 `decision`/`frames` 错配一律 422
  `segment_video_invalid`。语义闸门拒绝 → 不落 `passed`、重建同一段待办（不是 `failed`）；
  技术异常 → `failed`，重试重跑同一次校验、素材不重交。
- **段状态住快照**：`session.segments[]` 的 `status`（`pending` / `waiting_video` /
  `validating` / `passed`）、`prompt_artifact_id`、`video_asset_id` 每段落定立刻落盘；
  `_segment_step` 用「第一条非 `passed` 的段」决定下一次 job 做规划/写词/校验/拼接，
  不靠 job 记录记账（15 段会挤爆 `JOB_HISTORY_LIMIT`）。
- **写词逐段增量**：第 N+1 段的请求带第 N 段尾帧锚（`stage_state.bridge_frame_descriptions`），
  不能一次写完所有段。
- **两道闸门没有 opt-out**：技术档在 resolve 时同步判（帧数档/时长/尺寸），语义在 job 内
  读尾帧与 `end_hook` 对照。7–10 秒档不在 `H3_TIER_FRAMES` 实测表内，技术档只报
  `segment_video_duration_unknown_tier` **warning**，不阻断；服务端只把 `error` 回给客户端，
  warning 由验收驱动器用同一个函数本地复核后记录。
- **不读也不写 CLI 的段产出账**（`segments/shot-ledger.jsonl`，D6-AC12）：同一会话目录
  曾被 CLI 跑过时，进度只以工作台自己的段状态为准。

## ref 段循环（D7-5，#258）

分段门槛是**路线属性**（spec §5.4）：`ref` 一律进段循环，`duration` 超过单次生成上限
（`ref_segments.is_single_segment_route`，从 `MAX_SEG_S` 派生、不写字面 15）才真拆段；
≤ 上限的会话就是「1 段」直出——页面显示 1 段，走同一套段循环（待办、双闸门、拼接），
不是长片。`JobRunner._needs_segment_loop` 的判据：`brief.is_ref or is_long_form(duration)`。

| 步骤 | 与 base 的差别 |
|---|---|
| 分段规划 | `_plan_ref_segments` 走 `prepare_ref_segmentation`（切点/闭包/重编号），`segments/plan.json` 落盘但**不是产物**（已由 `SegmentOut` 表达）；`segments/order-card.md`（kind `concat_order_card`，段序唯一依据）与 `segments/ref-shot-table.md`（kind `ref_shot_table`，四列分镜表）登记进产物表 |
| 写段 | 从盘上 `plan.json` + `video-prompt.md` 重建（`load_ref_segments_for_resume`），**不重新规划**——段边界一变，已通过段就对不上了；段词落 `segments/segment-NN.md`（kind `segment_prompt`，与 base 的 `shot-NN.md` 并列两条模式） |
| 段校验 | **不建桥接帧、不剥尾帧、不调 `describe_image`、不判 `end_hook`**（ADR 0011）：段尾语义在写段时由段内注记判；提交时只确认技术档（resolve 已判）与段词仍在 |
| 换一段 | `POST /sessions/{id}/segments/{index}/reopen`：`passed → waiting_video`、清 `video_asset_id`、重建同段 `segment_video` 待办；**只换视频，段词与段内子集不动**。base → 409 `segment_resubmit_unsupported`；段号不存在 → 404 `segment_not_found`；未通过 → 409 `segment_not_passed` |

- **段内参考素材只读**（spec §10）：`SegmentOut.assets` 回显规划期算好的子集（段内重编号
  Picture 号 + 语义名 + 整片原号）；链条是分镜表标签 → 闭包 → 撤一致性锚 → 从 1 重编号。
  没有任何手改入口——逐段手填等于把闭包算法搬到前端。
- **容量在规划期硬失败**（spec §8）：段内子集超 9 图或单请求超 12 文件时 job 失败并点名
  超限素材（`ref_segments._check_segment_capacity`；工作台把 `SessionState.asset_ledger`
  当另一个台账来源传进去，9/3/3/12 的判据只有一份）。
- **路线错配 409**（spec §13）：ref 会话带 `frames` 或 `frame_submission` →
  `route_mismatch`（ref 不走帧链，ADR 0011）。
- **base 的读图路径一字不变**（#258 AC）：桥接帧、尾帧剥取、`end_hook` 判据都还在
  base 分支里；`_needs_segment_loop` 对 base 仍只认 `>10` 秒。

验收命令（手动，不进 CI；配置来源断言在起被测程序之前）：

```bash
# 阶段 A：真实 LLM 跑完阶段 1/2/规划，写出第 1 段提示词与 handoff.md
PYTHONIOENCODING=utf-8 PYTHONPATH=src .venv/Scripts/python.exe scripts/accept_d6.py --prepare
# 用户按提示词在 ComfyUI 生成 segment-01.mp4… 放进 videos/ 后
PYTHONIOENCODING=utf-8 PYTHONPATH=src .venv/Scripts/python.exe \
    scripts/accept_d6.py --submit-dir output/accept-d6/<戳>/videos
```

CI 侧由桩管线覆盖三段闭环（`tests/test_api_fake_pipeline_segments.py`、
`frontend/e2e/longfilm.spec.ts`）；真实档的证据与「未跑到 60 秒上限」的边界声明落
`output/accept-d6/`。驱动器拒绝桩模式（`MINIMAX_FAKE_PIPELINE=1` / `--fake` 一律 FAIL）。



媒体读取（素材 `GET/HEAD /media/{asset_id:path}` 与成片
`GET/HEAD /artifacts/{artifact_id:path}/content`）和本地鉴权共用一套边界：只服务
`sessions_root`，凭据「请求头或 Cookie」二选一。素材的标识是 `<会话标识>/<16 位
内容哈希>`（无扩展名），成片走登记表里的已知文件名；两者都按单段 Range 语义处理。

| 规则 | 位置 | 不变量 |
|---|---|---|
| Host | `api/auth.py` | 主机名 ∈ `127.0.0.1` / `localhost` / `[::1]`，**端口不限**；否则 403 `host_forbidden` |
| Origin | 同上 | 带 Origin 时主机名同样须回环，否则 403 `origin_forbidden`；无 Origin 放行 |
| 凭据 | 同上 | `X-Workbench-Token` 头**或** `wb_session` Cookie，任一有效即通过；否则 401 `auth_required` |
| 豁免面 | `auth.is_exempt` | 闭合三处：`GET /api/v1/health`、`GET /api/v1/auth/token`（仅 GET/HEAD）、不在 `/api` 下的一切 |
| 媒体根 | `workbench_store` | 只解析 `sessions_root` 之下；`asset_id` 拆出的会话必须已通过 `_resolve`，文件名必须是 `<16 位十六进制>.<已知扩展名>`；不接 `D:\素材库`、不直服 ComfyUI 输出 |
| Range | `api/media.py` | 单段 `a-b` / `a-` / `-n`；越界、语法非法、多段一律 416 + `Content-Range: bytes */<len>`；素材与成片共用这一份 |

- **令牌每进程随机、只存内存**（`create_app` 生成，`app.state.local_access`）。重启即换；
  测试里每个 app 各一份。
- **凭据绝不进证据**：URL、access log、错误体、`output/accept-*/` 报告里都不许出现。
  验收驱动器取令牌时只记状态码，不记响应体。
- **中间件在路由外层**：错误响应必须用 `problems.build_problem_response` 自己构造，
  抛 `HTTPException` 只会变成 500（异常处理器在它内层）。
- **测试入口统一用 `tests/workbench_client.py`**：`TestClient` 默认 base_url 是
  `http://testserver`，会被 Host 校验当成非回环；令牌又随 app 生成，手拼必漏。
- 新增任何「不走 `/api`」的端点等于自动豁免——加之前先想清楚它是不是该在豁免面上。

## 契约链

`FastAPI` → `scripts/export_openapi.py` → `contracts/openapi.json` →
`frontend/scripts/generate-api.mjs` → `frontend/src/api/generated.ts`。
`scripts/check.sh full` 会重导并 `git diff --exit-code`（`fast` 在改动 api/契约时同样跑）；CI 同口径。

## 桩管线（D4）

`MINIMAX_FAKE_PIPELINE=1` 让 `JobRunner` 用固定阶段 1/2 与假读图跑完整个闭环，**不建任何模型**。
它是 Playwright CI 与本地调试的开关：

- 只认这一个环境变量（`api/fake_pipeline.py` 的 `fake_pipeline_enabled`），默认关，没有任何 API 入口；
  启动日志会明确打印「桩管线已开启」。
- 真实与桩是**互斥的执行分支**（`JobRunner.fake_pipeline` 为真时根本不调 `run_stage1` / `run_stage2`），
  不是真实路径里换一段回复。桩产出的状态与产物仍走真实落盘路径，浏览器看到的形状一致。
- `frontend/scripts/serve-e2e.mjs` 在 `playwright test` 的 webServer 里注入它；真实验收
  （`scripts/accept_d4.py`）必须对着**没有这个变量**的服务跑。驱动器的 `--self-check`
  会把 `--fake` 与本进程开着该变量两种误标判成 FAIL，并以非零退出码收场——桩模式
  不允许「自检通过」。

## 冻结形态（EXE）（D5，ADR 0035）

外壳只做三件事：等 `/api/v1/health` 就绪 → 建窗 → 关窗退出（`desktop.py`）；业务一行不改，
窗口地址与系统浏览器完全相同、**不带令牌**。冻结后只有下面这些位置与开关变了：

| 项 | 开发形态 | 冻结形态 |
|---|---|---|
| 启动入口 | `python -m minimax_h3_prompt.api`（或 `-m …desktop` 带窗口） | `MiniMax-H3-prompt.exe`（`scripts/exe_entry.py` 转发到 `desktop.main`；`--self-check` 走 `selfcheck`，不开窗） |
| 端口 | 固定 `8000`（Vite 代理写死，不能随机） | 默认随机空闲端口；`--port N` 显式、`--port 0` = 自动 |
| 数据根 | 仓库根 | EXE 同级 `data/`；`--data-root` 可覆盖（argv 必须先于任何业务 import 解析） |
| 只读资源 | 仓库根 | `sys._MEIPASS`（前端 `dist`、内置配置模板、提示词与知识库） |
| 会话 / 产物 | `output/sessions`、`output/` | `data/sessions`、`data/output`（**并列**，不叫 `data/output/sessions`） |
| 配置 | 仓库 `config/` | `data/config/{agent,image,workflow,library}.yaml`，首启从内置模板只补缺 |
| 密钥 | 仓库 `.env` | `%APPDATA%\MiniMax-H3-prompt\.env`，**不读** `data/` 下的 `.env`，不受 `--data-root` 影响 |
| 日志 | 同数据根 | `data/logs/app-YYYYMMDD.log`，按日滚动、保留 `logging.retention_days`（默认 7），写前脱敏 |
| 诊断包 | 同一端点 | `GET /api/v1/diagnostics/bundle`（`application/zip` + attachment，**不在**鉴权豁免面上） |

- **端口的事实只有一个来源**：`StartupInfo`（启动日志、窗口标题 `…（端口 N）`、概览页都读它）。显式端口被占用时**退出码 2 并点名端口**，不静默换；已有实例退出码 3，其余起不来是 1。
- **启动参数也认环境变量**（spec §5.2）：`--port` / `--data-root` 未传时读 `H3_PORT` / `H3_DATA_ROOT`，优先级 **命令行 > 环境变量 > 形态默认**。非法 `H3_PORT`（非整数、超出 0–65535）以「起不来」（退出码 1）收场并点名变量，不悄悄退回默认——双击启动的冻结形态没有命令行，这两个变量是唯一的改法。
- **单实例按数据根**：命名互斥体名由数据根绝对路径的哈希派生——随机端口让「双击两次」不再像冲突，不拦就会变成两个写入者。
- **`--data-root` 的时机是硬约束**：配置是 import 期单例，迟到的覆盖不生效；启动入口因此在业务 import 之前解析 argv。
- **根路径推导只许一处**：新增「项目根 / 资源根 / 数据根」推导走 `paths.py`，结构守卫会咬第二处。
- **CDP 调试端口**：`--debug-port N` → `webview.settings['REMOTE_DEBUGGING_PORT']`；环境变量 `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS` 那条路**实测不通**（spike 第 6 节）。
- **WebView2 是运行前提**：缺失或主版本过旧时启动前拦下并给安装指引；版本不可判定只记警告、继续启动。
- **关窗即退出**：正在跑的任务按既有 `interrupted` 语义收尾，不常驻、无托盘；重启后会话与产物仍在。
- **构建与自检**命令、`build-exe/` 命名理由见 `docs/agents/frontend.md`，别在这里抄第二份。

## 验收

真实跑：主树、`python -m minimax_h3_prompt.api` 另开终端，再跑 `scripts/accept_d2.py`
（会话生命周期）与 `scripts/accept_d3.py`（真实素材上传 + 真实成片 Range）。浏览器侧
由 `frontend/e2e/media.spec.ts` 覆盖上传与 `<video>` seek 206（CI 用入库的
`tests/fixtures/tiny.webm`；真实照片/成片那条由 `ACCEPT_D3_PHOTO` / `ACCEPT_D3_ARTIFACT`
门控（`ACCEPT_D3_REPORT` 可把浏览器侧事实另存一份 JSON），手动跑）。确定性全链路在
`tests/test_api_*.py` 与 `tests/workbench_deterministic.py`。

D4 的浏览器闭环：CI 由 `frontend/e2e/delivery.spec.ts`（T2VA 直通 + FL2VA 帧提交，桩管线）
与 `sessions.spec.ts`（SSE 重连）覆盖；真实 LLM 不进 CI，由 `scripts/accept_d4.py` 手动跑
（Playwright + 真实审查/修订 + 真实图片帧 + 真实读图，报告落 `output/accept-d4/`）。
完成态还会读会话文件核对帧证据（首/尾读图描述非空、`frame_images[].target` 的文件存在），
不以「页面显示已完成」代替「读图真的发生过」。证据评论里**不出现令牌**。

D6 的分段闭环：CI 由 `frontend/e2e/longfilm.spec.ts`（桩管线的段列表与回交表单）与
`tests/test_api_fake_pipeline_segments.py`（规划→写词→回交→校验→拼接）覆盖；真实档由
`scripts/accept_d6.py` 两阶段手动跑（`--prepare` / `--submit-dir`，报告落
`output/accept-d6/`）。驱动器在**起被测程序之前**断言配置来源（绝对路径、`sha256`、
端点/模型），并拒绝桩模式；`--submit-dir` 用服务端同一个技术档函数本地复核，把
服务端不回的 `warning`（7–10 秒档 `unknown tier`）如实写进证据。
