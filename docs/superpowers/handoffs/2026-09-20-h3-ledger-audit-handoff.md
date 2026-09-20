# 交接：H3 生成台账与审计（2026-09-20）

> **读者**：接手本项目的下一个 Claude 会话（零上下文）。
> **读法建议**：先读本文件 → 再读 `docs/superpowers/plans/2026-09-20-h3-ledger-and-audit.md`（计划，含完成记录）→ 需要设计理由时读 `docs/superpowers/specs/2026-09-20-h3-ledger-and-audit-design.md`。

---

## 0. 一句话状态

计划 14 个任务**全部落地并提交**；后续 6 项修补也全部完成；真实素材验收全部命中手工基线。**工作区全绿：`234 passed`，约 12s。**

---

## 1. 工作区精确状态（先看这个）

```
分支 master，最近提交：
  d1abec1  fix(tools): threshold 贯通到建边；mad 越同源阈值的输入帧来源改判 low 并进 review_needed
  fa801e8  test: COMFY_OUTPUT 默认值集中到 conftest，并补 H3_COMFY_OUTPUT 解析测试
  c757217  fix(tools): merge_videos 总是清理中间产物 concat_list.txt
  f2a2e16  test: 去掉 suggest_defrost 的空断言（or True）…
  0ed8ca9  test: 真实素材回归改 module 级 fixture…（45s→~11s）
  58992e0  fix(tools): 桥接帧未匹配时按尺寸判 kind 并进 review_needed
  2c60967  test: 钉死 resolve_chain 的链延续性优先（含突变验证）
  6702582  docs: 计划执行完成
  2ed9484  feat(cli): 新增 preflight 子命令
  …（更早见 git log）

未提交：
  M config/agent.yaml   ← ⚠️ 用户自己的改动，全程 20+ 个提交里从未被 stage 过，不要动、不要提交
```

当前 `pytest` 应为 **239 passed / 0 failed**。若不是，说明有人改坏了东西，先别往下做。
（本交接文档初版写的是 234；其后新增 5 条测试，见 §9。）

---

## 2. 这次交付了什么

一个数据源 + 三个消费者，**零侵入**（未改 `graph/`、`ui/`、`pipeline.py`、`session_store.py`、`agents/`，只在 `main.py` 加了三个子命令组）。

| 模块 | 职责 |
|---|---|
| `src/minimax_h3_prompt/tools/frame_match.py` | 帧读取/比对原语：中文路径安全读图、首尾窗口、灰度降采样、MAD、阈值分级（`MAD_SAME=2.0` / `MAD_MAYBE=5.0`） |
| `src/minimax_h3_prompt/tools/ledger.py` | **建边**推断「镜头 → 视频 → 输入帧」链路 → `ledger.json`；override 合并 |
| `src/minimax_h3_prompt/tools/seam_audit.py` | 静止检测、ffmpeg 合并、报告、基线对比 |
| `src/minimax_h3_prompt/tools/preflight.py` | 提交前三档校验（可证 error / 启发式 warning / 资源 refrain） |

测试：`test_frame_match.py`、`test_ledger.py`、`test_ledger_cli.py`、`test_ledger_real_assets.py`、`test_seam_audit.py`、`test_preflight.py`、`test_preflight_cli.py`、`test_audit_cli.py`、`conftest.py`（合成视频 fixture）。

---

## 3. 怎么验证（可直接复制运行）

```bash
# 全量（预期：234 passed，约 12s）
./.venv/Scripts/python.exe -m pytest -q

S="output/sessions/古老图书馆里-少年撕下会发光的书页折成纸-9ac0bf14/GEN001"
O="D:/Comfyui/ComfyUI/output/视频/MiniMax-H3/2026-09-18"

./.venv/Scripts/python.exe launch.py ledger rebuild "$S" --output-dir "$O"
./.venv/Scripts/python.exe launch.py audit run    "$S" --output-dir "$O" --no-merge
./.venv/Scripts/python.exe launch.py preflight --input-frame "$S/bridge_frames/shot-02-start.png" \
    --prev-video "$O/MiniMax-H3视频_00001-audio.mp4"
```

**期望输出（2026-09-20 实测，改动推断算法后应逐字不变）**：

```
镜头 5 个：…00001-audio.mp4 → 00002 → 00004 → 00005 → 00006
[废片] MiniMax-H3视频_00003-audio.mp4（high）— 未被任何桥接帧引用
[中断] 第 6 段未产出（medium）— …

镜头 5 个 ｜ 每缝均值 24.2 帧 ｜ 占全片 14.1%
  接缝静止 · 每缝均值（帧）: 基线 24.2 → 本次 24.2（持平）
  接缝静止 · 占全片: 基线 14.1% → 本次 14.1%（持平）

预检通过。            ← 正确配对；绑错段应输出 mad≈3x 的 [阻断]
```

> 手工基线（2026-09-18 spike）：接缝静止 **4.04s / 14.1% / 全片 28.67s**。
> `每缝均值` 的真值恰为 `97/4 = 24.25`，`round(…, 1)` 走银行家舍入显示 **24.2**（手工脚本当时显示 24.3）。不是测量差异，别去"修"。

---

## 4. ⚠️ 已核查的结论 vs 上一会话说错的话

**上一会话（2026-09-20）把几件性质完全不同的事都笼统叫成了「计划的 bug」，用户对此存疑——这个怀疑是合理的。** 下面逐条给出核查后的真实性质。

| 上一会话的说法 | 核查结果 | 真实性质 |
|---|---|---|
| 计划缺陷①：`kind` 判定自相矛盾（spec 说按尺寸、计划测试要求 128×72 判 `extracted`） | **成立** | 计划自相矛盾；用户已拍板改为「以匹配到 src 视频为准」，spec ① 已同步 |
| 计划缺陷②：Task 6 的 `_make_session` 造不出链 | **成立**（探针实测：`dst_candidates` 全空，链路只还原 1 个镜头） | **真缺陷**（fixture 没兑现它自己 docstring 写的「尾帧接力」） |
| 计划缺陷③：`find_ffmpeg` 用了 `except Exception: pass` | **成立**（计划原文如此） | 违反计划**自身**铁律，非功能 bug |
| 计划缺陷④：`classify_leftovers` 取「遍历到的第一个」是 bug | **❌ 不成立——上一会话说错了** | 真实素材上原版与现版**答案相同**（00003 只与 00004 同源，mad=0.88，其余 21.6–49.1，**根本不存在平局**）。那个平局只存在于 fixture（三个**逐字节相同**的视频）。上游 subagent 把它报成"计划实现有 bug"，上一会话未经核实就转述了。改动本身是合理的健壮性改进，但**不是 bug 修复** |
| 计划里那句 `warnings.append("…"是别人的来源"…")` 是 `SyntaxError` | **成立**（该行含 8 个 ASCII 双引号、0 个中文引号，编译实测报 `invalid syntax`） | 真缺陷 |
| 遗留①：`test_resolve_chain_prefers_continuation_over_time` 是无区分力的弱测试 | **成立**（内存 exec 突变实测：删掉链延续性优先后旧测试**照样通过**） | 真测试缺陷，**已修**（新增 2 条测试 + 突变验证） |
| 遗留②：桥接帧的尺寸后备是死代码 | **成立**（读代码即可见：`src_video is None` 提前 return） | 代码与 spec 不一致，**已修** |
| （subagent 提出）`classify_match` 会返回 `no_match`，而 `confidence` 值域只有 high/medium/low | **不可达** | `build_edges` 用 `threshold=MAD_MAYBE(5.0)` 过滤，`src_mad` 恒 < 5.0；真实 `ledger.json` 里 6 个 confidence 全是 `high`。只有手工构造 `BridgeRef` 直接调 `_shot_input_frame` 才可能漏出。属潜在值域不一致，非现网问题 |

**教训（写给下一个会话）**：subagent 报告里的「计划有 bug」必须自己复现一遍再转述；「测试失败」和「实现有 bug」是两件事，很可能是 fixture 不真实造成的。

---

## 5. 剩余待办

### ① 本轮 6 项修补已全部完成（记录在案）

| # | 问题 | 状态 |
|---|---|---|
| 1 | `test_resolve_chain_prefers_continuation_over_time` 无区分力（核心消歧零护栏） | ✅ `2c60967` 新增 2 条测试 + 内存突变验证 |
| 2 | 桥接帧的尺寸后备是死代码（代码与 spec 不一致） | ✅ `58992e0` 后备可达 + 进 `review_needed` |
| 3 | 真实素材测试把全量拖到 45s | ✅ `0ed8ca9` module fixture → 12s |
| 4 | `SESSION` 相对路径，换 cwd 静默 SKIP | ✅ `0ed8ca9` 锚定仓库根 + skip 理由带解析路径 |
| 5 | `suggest_defrost` 的 `or True` 空断言 | ✅ `f2a2e16` 改为验证真的替换成活词 |
| 6 | `merge_videos` 残留 `concat_list.txt` | ✅ `c757217` `try/finally` 总是清理 |

### ② 已知的其它小项

- ✅ **已修**（`fa801e8`）：`COMFY_OUTPUT` 的硬编码默认路径已集中到 `tests/conftest.py`
  的 `DEFAULT_COMFY_OUTPUT` + `comfy_output_dir()`，并补了 env 解析测试
- 真实素材测试依赖本机素材，缺素材时 SKIP（reason 里已带解析后路径 + 要设哪个环境变量，不会静默）
- `audit/` 目录里 `merged_naive.mp4`、`latest.json`、`<时间戳>.md`、`baseline.json` 是**有意保留**的产物（`output/` 已在 `.gitignore` 里）
- ✅ **已修**（`d1abec1`）：`InputFrame.confidence` 与 `classify_match` 的值域不一致。
  ⚠️ 本文件初版断言它「经 `build_ledger` 不可达（`src_mad` 恒 < 5.0）」——**那个前提是错的**：
  `build_ledger` 的 `threshold` 当时根本没传给 `build_edges`，这才是"恒 < 5.0"的原因；
  一旦把参数贯通，`--threshold 20` 就能让 `no_match` 从公开路径漏出来。详见 §9

### ②b 仍然存在的小项（未修）

- `_make_session` 把 float32 图直接 `cv2.imencode`，触发既有的
  `cv::imencodeWithMetadata Unsupported depth image ... fallbacked to CV_8U` 警告。
  测试输出因此不"干净"（非本轮引入，改动前的基线跑就有）

### ③ 计划里「明确不在本计划内」的部分（别顺手做）

- 驱动 ComfyUI 自动出片
- 改提示词层的 `end_hook` 冻结词（本工具只提供检测手段）
- CI 接入（合成 fixture 已 CI-ready，配 GitHub Actions 是独立一步）

---

## 6. 关键设计决策（已拍板，不要推翻）

1. **建边而非猜身份**：每张桥接帧对全部视频的**尾窗口**和**首窗口**各做一次 argmin，得到 `src → dst` 边；起点用 `pick_start`（是别人的 src、但从不是任何边的 dst），**不按尺寸猜**。
2. **`dst_candidates` 必须保留全部候选**（不能只留最优）——真实歧义（00003/00004 首帧 mad=0.88 同源）靠这个才能解。消歧优先级：**链延续性 > 生成时间**。
3. **`kind` 以「匹配到 src 视频」为准**（用户 2026-09-20 拍板），尺寸规则 `1280×736 → extracted` 只在匹配不上时作后备。
4. **`confidence` 必须配 `evidence`**：推断不出就进 `review_needed`，**不猜**。
5. **override 独立成文件** `ledger.override.json`，工具永不覆盖。
6. **预检三档**：可证规则 → `error` 阻断；启发式（冻结词）→ `warning`（实测反证：镜头 5 的 `end_hook` 含「定格」却末尾静止 0，是相关不是因果）；资源规则 → `refrain`。
7. **只读铁律**：预检/台账/审计绝不修改任何输入文件，ComfyUI output 目录一行不写。
8. **绝不静默失败**：新代码禁止 `except Exception: pass`；读不了的文件必须反映到 `warnings` / `review_needed`。

---

## 7. 环境与坑

- **测试命令必须用 `./.venv/Scripts/python.exe -m pytest`，不要用裸 `pytest`**。`python -m` 会把项目根加进 `sys.path`，`from tests.test_ledger import _make_session` 这类跨测试文件 import 依赖这一点。
- Windows 控制台是 cp936，直接跑 CLI 中文会乱码；看中文加 `PYTHONIOENCODING=utf-8`（乱码只影响显示，不影响文件与断言）。
- `cv2.imread` / `cv2.imwrite` **打不开中文路径**，必须走 `np.fromfile` + `cv2.imdecode`（已封装在 `frame_match.imread_unicode`）。`cv2.VideoCapture` 反而正常。
- 读视频尾帧不能用 `CAP_PROP_POS_FRAMES` 精确 seek（H.264 关键帧对齐会偏移），要往回多退 12 帧再顺序读到尾（见 `frame_match._TAIL_REWIND`）。
- ffmpeg 用 `imageio-ffmpeg` 自带的（`find_ffmpeg()`），已加进 `pyproject.toml` 依赖。
- Bash 工具下**进程替换 `<(...)` 不可用**（Windows），要用真实临时文件。
- 真实素材路径：会话 `output/sessions/古老图书馆里-少年撕下会发光的书页折成纸-9ac0bf14/GEN001`，输出 `D:/Comfyui/ComfyUI/output/视频/MiniMax-H3/2026-09-18`。

---

## 8. 项目规矩（CLAUDE.md 已声明，这里提醒）

- **改代码前先出 spec 并等用户明确批准**，未批准不动一行；实现中发现方案要调整，先停下告知。
- 任何任务动手前先查 skills 列表，命中就调用。
- 长期记忆写到 `D:\笔记\ClaudeBrain\`（项目记录 `projects/minimax-h3-prompt.md`，索引 `INDEX.md`）。本次交付已登记；若本次交接后的改动改变了结论，记得同步更新。

---

## 9. 本轮增补（2026-09-20 第二个会话）

**做了什么**：修 §5② 的那两处小项；过程中发现并修掉一个更实质的缺陷。

### 9.1 新发现：`build_ledger` 的 `threshold` 没传给 `build_edges`

`build_ledger(threshold=...)` 把参数传给了 `pick_start` / `resolve_chain`、还写进了
`scan.threshold_mad` 报告，唯独漏了 `build_edges`（用默认 `MAD_MAYBE=5.0`）。
CLI 上 `--threshold`（`main.py:50`）因此对**建边**静默无效，而报告里写着用户给的值。
实测 `build_ledger(threshold=999)` 与 `threshold=5.0` 输出逐字一致。

这条是**用户可达**的，形态正是设计决定 #8「绝不静默失败」禁止的。
修复：`build_edges(..., threshold=threshold)`（`d1abec1`）。

**它同时解释了 §5② 那句"潜在值域不一致"的成因**——edges 恒在 5.0 过滤，
`src_mad` 才恒 < 5.0，`no_match` 才不可达。原判断把「当前的巧合」当成了「结构上的不可能」。

### 9.2 `no_match` 的处置：降为 `low` 并入 `review_needed`

阈值贯通后 `no_match` 就可达了：`--threshold 20` 时，mad 落在 `[5.0, 20)` 的镜头
旧代码会产出 `status="final"` + `confidence="no_match"`，**且没有任何 review 标记**。
现按设计决定 ④ 降为 `"low"` 并记入 `review_needed`；`source` / `match_mad` 照实保留。

副作用（有意接受）：`classify_match` 的第三个返回值不再有任何消费方，
`InputFrame.confidence` 的真实值域恰好就是注释原写的 `{high, medium, low}`。

### 9.3 本轮的判别实验（改动前做不到的）

```bash
S="output/sessions/古老图书馆里-少年撕下会发光的书页折成纸-9ac0bf14/GEN001"
O="D:/Comfyui/ComfyUI/output/视频/MiniMax-H3/2026-09-18"
./.venv/Scripts/python.exe launch.py ledger rebuild "$S" --output-dir "$O" --threshold 0.0   # → 1 个镜头
./.venv/Scripts/python.exe launch.py ledger rebuild "$S" --output-dir "$O" --threshold 5.0   # → 5 个镜头
```

**默认路径行为零变化**：§3 的三条验收命令输出与改动前逐字一致；
`ledger.json` 的 `confidence` 仍全 `high`、`review_needed` / `warnings` 仍为空。

### 9.4 新增测试（234 → 239）

- `tests/test_ledger.py`：`test_build_ledger_forwards_threshold_to_edge_building`、
  `test_build_ledger_flags_weak_bridge_source_for_review`（`bridge_shift` 参数是新增的，
  默认 0.0 与历史行为一致）
- `tests/test_real_assets_env.py`（新文件，3 条）：钉住 `H3_COMFY_OUTPUT` 解析契约，
  **不依赖真实素材**，缺素材的机器上照跑

### 9.5 教训（延续 §4 那条）

§4 的教训是「subagent 报的 bug 要自己复现再转述」。本轮补一条：
**「不可达」这种结论必须连「为什么不可达」一起验**。只验"当前跑不出来"就下结论，
会把一个参数传递 bug 当成设计上的先天保证。判据：若断言某分支不可达，
要能指出**是哪个不变量**保证它不可达，并单独验证那个不变量成立。
