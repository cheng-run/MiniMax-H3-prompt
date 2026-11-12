# 工作台会话与 Headless API（D2–D4）

> 入口：`python -m minimax_h3_prompt.api`（只绑 `127.0.0.1:8000`）。spec #214 / #227，ADR 0032、0033、0034；
> 本地鉴权见 #222 §4 与 ADR 0033。
> 这张表是导航与不变量，不是教程；实现细节以代码注释为准。

## 模块地图

| 模块 | 职责 |
|---|---|
| `workbench_store.py` | 会话仓库：`workbench.v1` 快照、原子写、每会话线程锁、`mutate`、事件日志 `EventLog`、幂等登记表、产物登记 |
| `api/jobs.py` | `JobRunner`：每会话 asyncio 锁、`to_thread` 跑 LangGraph、节点边界取消、`active-job.json`、启动对账、阶段 1/2 执行 |
| `api/routes/sessions.py` | `/api/v1` 全部会话端点；**路由顺序即契约**：`{session_id:path}` 会吞斜杠，`/jobs`、`/events`、`/action-items` 必须注册在详情路由之前 |
| `api/routes/assets.py` | 上传素材的收与列（`POST/GET /sessions/{id}/assets`）；整只路由器在 `create_app` 里排在 `sessions` 之前，顺序不靠人记 |
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
| `step_machine.py` | 状态/触发/闸门的单一声明；续跑起点；`resolve` 的 approve/revise 去向与 approve 按变体分叉 |
| `session_store.py` | 快照文件读写、`atomic_write_text`、向导续接状态与 `RESUMABLE_STATUSES` |
| `api/fake_pipeline.py` | 确定性桩管线：固定阶段 1/2 与假读图，**只认环境变量 `MINIMAX_FAKE_PIPELINE`**（默认关、无 HTTP 入口），真实/桩互斥 |
| `frontend/src/features/sessions/` | 工作台四分区：`SessionDetailView`（壳/SSE/运行控制）、`ReviewTab`（审查与修订）、`AssetsTab` + `FrameSubmissionCard`（素材与帧提交）、`ArtifactsTab`（产物查看/复制/下载/通用媒体播放） |
| `frontend/src/api/client.ts` | 生成契约的类型出口与唯一 API 门面：令牌注入、媒体/产物 URL、错误归一成 `ApiError` |
| `scripts/accept_d2.py` | 真实 HTTP 验收驱动器（手动，不进 CI；启动时先取令牌） |
| `scripts/accept_d3.py` | 媒体与素材的真实验收驱动器（手动；真实照片上传 + 真实 `final.mp4` 的 Range 与字节核对，报告落 `output/accept-d3/`） |
| `scripts/accept_d4.py` | D4 真实浏览器验收驱动器（手动；Playwright + 真实 LLM 走审查/修订/帧提交/阶段 2/复制，报告落 `output/accept-d4/`） |

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
- 产物 content **按登记表的形态分支**（#225）：文本产物（登记表现有 7 件，名字由
  登记表派生）共用纯文本 + 1 MiB 上限（超出 413）；成片 `final.mp4` 给字节 + 单段
  Range；两者都答 `HEAD`（同头、无体）。哪几个名字能读由 `workbench_store` 的登记表
  派生，端点里不抄第二份白名单。
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

## 媒体与鉴权（D3，ADR 0033）

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
