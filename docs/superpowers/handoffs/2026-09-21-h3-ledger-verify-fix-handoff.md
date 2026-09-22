# 交接：H3 台账复核 + threshold 贯通修复（2026-09-20 第二个会话）

> **读者**：接手本项目的下一个 Claude 会话（零上下文）。
> **读法建议**：先读本文件；需要更早的历史与设计理由时，再读
> `docs/superpowers/handoffs/2026-09-20-h3-ledger-audit-handoff.md`（上一份交接，含 §4 结论核查、§7 环境与坑、§9 本轮增补——本文件与其内容一致，历史细节以它为准）。

---

## 0. 一句话状态

本会话**逐字复核了上一份交接的全部可验证主张（全部命中）**，随后在修小项时
发现并修掉一个更实质的用户可达缺陷（`--threshold` 对建边静默无效）。
**工作区全绿：`239 passed`，约 12s。**

---

## 1. 工作区精确状态（先看这个）

```
分支 master，最近提交（本会话新增 3 个）：
  c578530  docs: 交接文档更新至 239 passed，记录 threshold 未贯通的新发现
  fa801e8  test: COMFY_OUTPUT 默认值集中到 conftest，并补 H3_COMFY_OUTPUT 解析测试
  d1abec1  fix(tools): threshold 贯通到建边；mad 越同源阈值的输入帧来源改判 low 并进 review_needed
  （更早：c757217 / f2a2e16 / 0ed8ca9 / … 见 git log，均为上一会话的交付）

未提交：
  M config/agent.yaml   ← ⚠️ 用户自己的改动，全程从未被 stage 过，不要动、不要提交
```

当前 `pytest` 应为 **239 passed / 0 failed**。若不是，说明有人改坏了东西，先别往下做。

---

## 2. 本会话做了什么

### 2.1 独立复核上一份交接（教训落地：结论必须自己复现再转述）

| 复核项 | 结果 |
|---|---|
| §1 工作区状态 | **逐字命中**（234 passed、提交列表、agent.yaml 未 stage） |
| §3 三条真实验收 | **逐字命中**：链路 `00001→00002→00004→00005→00006`；废片 `00003` 判 high；接缝 4.04s / 14.1% / 每缝均值 24.2 帧；preflight 正确配对通过、绑错段 mad=37.17 阻断 |
| §4 争议行④（`classify_leftovers` 平局说） | 用**仓库外一次性脚本**独立复算：`00003` vs `00004` mad=**0.88**、其余 21.60/35.56/35.83/49.10，低于 `MAD_SAME` 的候选**只有 1 个 → 真实素材上无平局** → 上一会话的自我纠正成立 |

### 2.2 修复：`build_ledger` 的 `threshold` 没传给 `build_edges`（用户可达缺陷）

- `threshold` 传给了 `pick_start`/`resolve_chain`、写进了 `scan.threshold_mad` 报告，
  **唯独漏了 `build_edges`**（用默认 `MAD_MAYBE=5.0`）→ CLI `--threshold` 对「建边」
  **静默无效**，而报告里写着用户给的值。实测 `threshold=999` 与 `5.0` 输出逐字一致。
- 形态正是设计决定 #8「绝不静默失败」禁止的。修复：`build_edges(..., threshold=threshold)`（`d1abec1`）。
- **这条同时推翻了上一交接「`no_match` 经 `build_ledger` 不可达」的前提**——
  不可达是那个 bug 造成的巧合，不是结构保证。参数贯通后 `--threshold 20`
  就能让 `no_match` 从公开路径漏出（旧代码会产出 `status="final"` + 无任何 review 标记）。

### 2.3 `no_match` 的处置：降为 `low` 并入 `review_needed`

按设计决定 ④「confidence 必须配 evidence，不猜」：mad 落在 `[5.0, threshold)` 的输入帧
来源改判 `confidence="low"` 并记入 `review_needed`，`source` / `match_mad` 照实保留
（`d1abec1`）。副作用（有意接受）：`classify_match` 的第三个返回值不再有消费方，
`InputFrame.confidence` 的真实值域恰好就是注释原写的 `{high, medium, low}`。

### 2.4 `COMFY_OUTPUT` 硬编码集中（`fa801e8`）

默认路径集中到 `tests/conftest.py` 的 `DEFAULT_COMFY_OUTPUT` + `comfy_output_dir()`
（`H3_COMFY_OUTPUT` 优先，空字符串视为未设）；新增 `tests/test_real_assets_env.py`
（3 条，**不依赖真实素材**）钉住解析契约；`test_ledger_real_assets.py` 改从 conftest 取值，
skip 理由带「换机器请设 H3_COMFY_OUTPUT」。

### 2.5 新增测试（234 → 239）

- `tests/test_ledger.py`：`test_build_ledger_forwards_threshold_to_edge_building`、
  `test_build_ledger_flags_weak_bridge_source_for_review`（`_make_session` 新增
  `bridge_shift` 参数，默认 0.0 与历史行为一致）
- `tests/test_real_assets_env.py`（新文件，3 条）

---

## 3. 怎么验证（可直接复制运行）

```bash
# 全量（预期：239 passed，约 12s）
./.venv/Scripts/python.exe -m pytest -q

S="output/sessions/古老图书馆里-少年撕下会发光的书页折成纸-9ac0bf14/GEN001"
O="D:/Comfyui/ComfyUI/output/视频/MiniMax-H3/2026-09-18"

# 默认路径行为零变化的三条验收（输出应与上一交接 §3 期望逐字一致）
./.venv/Scripts/python.exe launch.py ledger rebuild "$S" --output-dir "$O"
./.venv/Scripts/python.exe launch.py audit run    "$S" --output-dir "$O" --no-merge
./.venv/Scripts/python.exe launch.py preflight --input-frame "$S/bridge_frames/shot-02-start.png" \
    --prev-video "$O/MiniMax-H3视频_00001-audio.mp4"

# 判别实验（修复前做不到的）：threshold 现在对建边真的生效
./.venv/Scripts/python.exe launch.py ledger rebuild "$S" --output-dir "$O" --threshold 0.0   # → 1 个镜头（链被打断）
./.venv/Scripts/python.exe launch.py ledger rebuild "$S" --output-dir "$O" --threshold 5.0   # → 5 个镜头（默认）
```

**默认路径零变化的判据**：`ledger.json` 的 `confidence` 仍全 `high`，
`review_needed` / `warnings` 仍为空；三条验收输出与改动前逐字一致（本会话实测如此）。

---

## 4. 教训（写给下一个会话）

1. **「不可达」这种结论必须连「为什么不可达」一起验。** 只验「当前跑不出来」就下结论，
   会把一个参数传递 bug 当成设计上的先天保证。判据：若断言某分支不可达，要能指出
   **是哪个不变量**保证它不可达，并单独验证那个不变量成立。（本轮 `no_match`「不可达」
   的真实原因是 threshold 没贯通，不是结构不可能。）
2. （延续上一交接 §4）subagent / 前一会话报的「bug」必须自己复现一遍再转述；
   「测试失败」和「实现有 bug」是两件事。

---

## 5. 剩余待办 / 不要顺手做的事

- **未修的小项**：`_make_session` 把 float32 图直接 `cv2.imencode`，触发
  `cv::imencodeWithMetadata Unsupported depth image ... fallbacked to CV_8U` 警告
  （测试输出有噪音，非本轮引入，改动前基线跑就有）。
- **明确别做**（无用户指示不碰）：驱动 ComfyUI 自动出片、改提示词层 `end_hook` 冻结词、CI 接入。
- `config/agent.yaml` 的未提交改动是用户自己的，**不要动、不要提交**。

---

## 6. 环境与坑

见上一交接 §7（venv 的 `python -m pytest` 调用方式、cp936 控制台、中文路径要用
`imread_unicode`、尾帧 rewind 12 帧等，均仍然有效）。真实素材路径同 §3 所列。
