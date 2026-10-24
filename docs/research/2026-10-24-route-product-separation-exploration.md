# 首尾帧与多参的产物集分离：代码勘察笔记（issue #77/#78~#83）

行号基准＝`2714503`；后续提交会使下文行号漂移（引用前先按符号/原文重定位）。

勘察时间：2026-10-24。只读勘察，未改代码。主仓 HEAD＝`2714503`（主分支 `archive/ref2va-full`），
代码与测试均无改动。基线：`--collect-only` 收集 **1258** 条，与本票口径一致。
（并行工作树 `worktree/integration` 已另有 #79 文档提交 `b4dde8a`，见 §7。）

## 1. 路线判据：`Brief.mode` 与既有「是不是多参」写法

- 唯一字段：`src/minimax_h3_prompt/brief_parser.py:69`（`mode: str = "base"  # ref | base`），
  便捷属性 `Brief.is_ref` 在 `brief_parser.py:93-95`；解析入口 `brief_parser.py:291-312`
  （`多参/ref2va` → ref；`首尾帧/fl2va` → base+FL2VA；优先级注释 `:255`）。
- **不可拿来当路线判据的两条支路**：
  - `brief_uses_refs()` `brief_parser.py:102-104`：`mode == "ref" or variant in FRAME_VARIANTS`
    （`FRAME_VARIANTS` `:99` 含 FL2VA）——这是「要不要参考图」，两条路线都真；
  - `min_refs()` `brief_parser.py:107-115`：按 variant 数张数，仍是帧路线语义。
- 既有的「真·路线守卫」三处（写法先例，各自只守一段）：
  - `make_identity_rt_node` `graph/nodes.py:944-946`（base → `{"identity_lock": ""}`）；
  - `_make_reference_consistency_node` `graph/nodes.py:724-728`（base → `{"subject_defs": ""}`）；
  - `ref_asset_registry` `generation.py:1018-1022`——但 `brief.refs` 分支在守卫**之前**返回
    （`:1019-1020`），正是票面点名的隐患。
- 此前为 FL2VA 打的「结果置空」特例：`generation.py:850-872`，判据 `brief.variant.upper() == "FL2VA"`，
  只挡 FL2VA；I2VA/L2VA/T2VA 的 base 仍走 else 支路（`:873-898`）。**这是要删的特例**。
- 历史坑：`brief_parser.py:361-368` 注释记录「变体写 FL2VA、输入写多参」会让帧链照跑、
  产物按 FL2VA 走——variant 与 mode 不同义。

## 2. 阶段 1 管线与三个资产图节点

- 链常量：`graph/pipeline.py:61-74`（`_STAGE1_CHAIN`，`parallel_image_prompts` 在 `:68`）；
  折叠表唯一真源 `_PARALLEL_GROUPS` `pipeline.py:89-94`，`_FOLDED_NODES` 现推 `:97-99`。
- 节点实现：`_make_image_prompt_node` `graph/nodes.py:156-217`；注册名与 state 键
  `graph/nodes.py:912-914`（`image_prompt_character/prop/scene` → `character/prop/scene_image_prompts`）。
  签名 `def node(state: PipelineState) -> dict`，正常返回 `{output_field: entries}`。
- **「空更新」先例**：关键帧节点在非帧变体 `return {}`（`nodes.py:495-499`）；并行组合
  `make_parallel`（`nodes.py:958-971`）合并子节点 dict，三者全空则整组 `{}`。
  `run_stage1` 只在 `if update:` 时落盘/回调/报进度（`pipeline.py:277-285`）——节点内路线
  早退会**自动**让进度行消失、续跑空转一次不发请求（与票面「已知副作用」一致）。
  守卫须写在节点内（读 `state["brief"].mode`）；在链上跳过节点会动拓扑。
- 模型调用清单的断言先例：`tests/test_asset_image_prompts.py:76-116` 的 `_run_stage1()`
  打桩 `nodes.run_agent` 与 `roundtable._invoke`，返回「全部出站请求」；同款
  `tests/test_ref_route_prompts.py:98-149`。断言落在请求正文（如 `:134` 按 `_ASSET_HEAD` 过滤）。
- **本次实测调用数**（打桩脚本，非测试）：
  - base T2VA：总 22＝圆桌 8＋`run_agent` 14（含资产 3；T2VA 不跑帧节点）；
  - ref T2VA：总 26＝圆桌 11＋`run_agent` 15（含资产 3）；
  - 推出 base 帧变体（I2VA/FL2VA）`run_agent` 现为 **15**，去掉 3 个资产节点＝**12**，
    与票面吻合。帧变体桩未跑通（假 bundle 过不了校验），12 属推导值、**未证实**到实测级；
    建议 #78 用 `_run_stage1` 的请求列表直接钉成用例。

## 3. 产物出口四处

- **结果构造**：`generation.py:840-916` `result_from_state`（FL2VA 特例 `:850-872`，
  else 支路 `:873-898`，资产在 `:898`）。
- **Markdown 渲染**：`generation.py:1192-1212` `render_generation`——分叉依据是
  `result.fl2va_prompt_bundle is not None`（**:1198**），**不是** `result.mode`；base 的
  T2VA/I2VA 旧数据无 bundle 时落到资产支路（`:1206-1212`），正是要加路线判定处。
  三份 md 落盘两条路：`topic_generation.save_generation` `topic_generation.py:40-56`
  （文件名表 `:20-28`）与向导 `_sync_released_asset_files` `ui/wizard.py:1249-1278`。
- **参考素材登记**：`ref_asset_registry` `generation.py:998-1037`（`brief.refs` 早返回
  `:1019-1020`，mode 守卫 `:1021-1022`）。
- **描述合并**：`_with_merged_descriptions` `generation.py:1040-1054`（内部调
  `image_prompt_variants` `:1048`）。
- `image_prompt_variants` 定义：`generation.py:929-995`。调用点：`generation.py:898/1032/1048`；
  `ui/wizard.py:1269`（md 同步）、`:1286`（逐张出词）、`:1329`（锁死清单）、`:1615`（读图校对）、
  `:1998`（素材队列）、`:2088`（无词素材补交）。前四处是本票四个出口；向导几处是多参内部消费者
  （保留纯函数直调，统一入口须透传画布）。

## 4. 会话层

- 读回/保存：`session_store.load_session` `session_store.py:234-258`（唯一读入口）、
  `SessionState.save` `:200-224`、`save_session` `:227-231`。
- 三键 schema 声明：`graph/state.py:29-31`；持久化白名单 `session_store.py:60-73`（`:63`）。
- 「字段损坏必须报错」纪律：`_canvases_from_dict` `session_store.py:112-135`、
  `_video_output_from_dict` `:138-157`、`load_session` docstring `:236-239`、
  `find_awaiting_sessions` 跳过告警 `:274-281`。路线过滤写在 `load_session`，
  docstring 明说「路线过滤，不是容错洗数据」。
- 真实旧样本：`output/sessions/` 10 个会话**全为 base/I2VA**，其中 3 个带全三键
  （`一个抖音女网红的旅游vlog-b6e2ab21`、`位于云上的天宫中…-af993547`、
  `极慢动作…-5ab5c019`），其余 7 个无三键。**无现成 ref 会话样本**——#80 的
  「多参三键原样保留」需自造夹具（**未证实**到真实样本级）。

## 5. 展示层

- 重跑确认门：`ui/wizard.py:897-909` `_confirm_setting_rerun`（念 `len(plan.nodes)` 步
  ＋ `plan.artifacts`）；调用点 `:1058`（传 `mode=brief.mode`）。
- 计划/产物映射：`graph/pipeline.py:160-221`；`_NODE_ARTIFACTS` `:182-194`
  （三项在 `:188`）；「仅多参」集合先例 `_REF_ONLY_ARTIFACTS` `:199`；
  `stage1_rerun_plan` `:202-221`。**#81 要求计划对象不改**：`plan.nodes` 保持全切片，
  由确认门按路线扣掉 `parallel_image_prompts` 的标签与一步；把过滤做进 `stage1_rerun_plan`
  会连带改结构测试口径。
- 进度环节名来源：`ui/wizard.py:359-374` `_NODE_LABELS`（`:366` →「资产图生图提示词」）；
  打印 `_report_node` `:377-379`；挂载 `:524/668/934/2629/2875`。源头早退后组合节点 update
  为空，行自然不出现（见 §2），展示层不必另改。
- **两条结构测试依赖节点清单与产物映射表**（拓扑零差异，应原样绿）：
  `tests/test_wizard_setting_revisions.py:279-288`（计划节点 ↔ `_NODE_ARTIFACTS` 一一对应）、
  `tests/test_asset_image_prompts.py:418-446`（`_PARALLEL_GROUPS` ↔ 真折叠＋链序）；
  另有第三道标签闸 `tests/test_wizard_setting_revisions.py:546-556`。

## 6. 测试地图

预计翻面（复用 base brief 的资产用例）：
- `tests/test_asset_image_prompts.py`：`_run_stage1`（`:76`，base I2VA）及其消费者
  `:123/:149/:186/:228/:278/:295/:448/:471/:602`；节点级 `:488-599` 直接传 base brief，
  守卫若在节点内必须换 ref brief。
- `tests/test_image_prompt_discipline.py:218-260`（节点级，base brief）。
- `tests/test_wizard_clarification.py:303-310`（节点级，base I2VA）。
- `tests/test_fl2va_prompts.py:772-800`（I2VA base 断言 `result.image_prompts` 非空，新口径应为空；
  「画布透传」的真实意图应挪到 ref brief 保留）。
- `tests/test_wizard_setting_revisions.py:395-404`（确认门 N 步的 base 断点，应改 N-1）；`:249-255` 不受影响。
- `tests/test_generation.py:17` 的 `make_result` 默认 base，但只断言 artifacts；`:54` 只断言
  script/video——需复核 `render_generation` 键集合变化后再判。

必须保留的多参正向断言：
- `tests/test_ref_route_prompts.py`（整文件，ref brief；登记/请求/阶段 2）；
- `tests/test_wizard_ref_route.py:142/278/303`、`test_wizard_ref_handoff.py`、
  `test_wizard_ref_resume.py`、`test_wizard_ref_semantics.py`、
  `test_wizard_asset_loop.py:856`、`test_wizard_asset_intake.py`、
  `test_wizard_asset_proofread.py`、`test_wizard_injection_card.py`；
- `tests/test_generation.py:109-197`（登记编号/描述/不泄漏 base）。

断言「三键/三份 md/确认门文案」的用例：`test_asset_image_prompts.py:320,354,386`、
`test_wizard_asset_loop.py:856`、`test_wizard_ref_route.py:303`、
`test_wizard_setting_revisions.py:395-404`、`test_fl2va_prompts.py:106`。

## 7. 术语守卫与新增词条

- 守卫：`tests/test_context_glossary.py`，四条独立不变式（`:76/88/98/118`），标题判据
  `^\*\*.+\*\*:$`（ASCII 冒号，`:39`）；已知边界见文件头 `:26-28`。
- 本树（HEAD `2714503`）**尚未落地 #79**；并行工作树 `worktree/integration` 的 `b4dde8a`
  已把三条互斥词条写进 `CONTEXT.md:56-68`（设计正文 / 资产图提示词 / 素材库图），ADR
  `docs/adr/0020-route-decides-artifacts-not-graph-copies.md` 已存在。若在那边实现，以那边为准；
  在本树新增则插在 `## 多参生成路线（ref2va）` 现有词条之后（同节出现词条后不许再出现散文块，
  第 4 条不变式）。当前词条标题数 69（`grep -cE '^\*\*.+\*\*:$' CONTEXT.md`）。

## 8. 基线

- 收集：`./.venv/Scripts/python.exe -m pytest --collect-only -q` → `1258 tests collected in 1.36s`（主仓）。
- 子集：`-k asset` → `188 passed, 5 skipped, 1065 deselected in 5.24s`（主仓实测）。
- 工作树跑测试须显式 `PYTHONPATH=<worktree>/src`，否则测的是主树（AGENTS.md）；本次未跑全量。
