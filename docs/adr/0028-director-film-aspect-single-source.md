# 导演台成片画幅只有一处真源：向导问一次，包与节点都照它

> **状态（2026-11-04 复核）**：仍生效。证据：`director/film_canvas.py`（判定与派生）、`ui/wizard.py` 的 `_choose_film_canvases`（唯一问答）、`ui/wizard_director.py` 的 `node_setup_lines` 调用点（打包后交代节点）。

日期：2026-11-04。关系：落实 #161（#157 的范围外跟进项）；修正
`docs/adr/0024-director-route-native-semantics.md` 之后仍然存在的「画幅三处声明」；
不动导演包结构、段规划与插件本身。

## 背景：一次真实短片里的三份画幅

2026-11-04 的 20 秒 fl2v 实跑（`docs/research/2026-11-04-质量验收-快过年返乡.md` §0）：

| 来源 | 声明 | 实际作用 |
|---|---|---|
| 向导画布问答 | `z_image: 1920×1080` | 只进关键帧生图（帧模型是 Qwen 时落到 1920×1088） |
| `config/agent.yaml` 的 `director.width/height` | 736×1280 | 写进导演包 `pack.json` 的 `output` |
| 导演台节点 width/height | 864×480（首版 480×864） | **实际执行值** |

同一个包、同一批段词，只改节点参数就出两版画幅；首版 9:16 把 16:9 的关键帧中心裁掉，
段 2 起点画面里的父母被裁出画。验收记录把这条列为 T2，并注明「与 2026-11-02 A/B
记录的现象相同」。

## 候选路线

1. **插件应用 `output`**：依赖导入时把 `pack.json` 的 `output` 应用到节点。
   源码里确实有这条路径（`director/pack.py` 的 `_assemble_timeline` 与
   `web/js/minimax_timeline.js` 的 `applyImportedTimeline` 都会读 `output.width/height`），
   但 2026-11-04 的实跑里**没有生效**，丢失点在浏览器层、未证实。把唯一真源押在一条
   未证实的行为上，等于把失败形态再留一次。
2. **向导交代节点**：包 `output` 由向导的画幅答案派生，同时在打包后打印节点该设的
   `width / height / ref_max_size`。不依赖插件的导入行为。

2026-11-04 用户裁定：走**路线 2**；改造范围限于**导演台路线**（base/ref 的注入卡
问答不动）。

## 决定

1. 导演台路线**只问一次成片画幅**（`ui/wizard.py` 的 `_choose_film_canvases`，复用比例→
   尺寸菜单）。问完按插件网格对齐到 32 的倍数（`film_canvas.snap_film_size`），并把
   对齐后的实际尺寸当场说破。
2. **关键帧画布由它派生**：`film_frame_canvases` 把成片画幅按**当前关键帧模型家族**
   写进 `Brief.canvases`（同尺寸、比例差为 0），帧请求的【画布】块与画幅预检都从这一份取。
3. **导演包 `output.width/height` 同取这一份答案**（`film_canvas.film_settings`）；
   `ref_max_size` / `ref_image_size` 仍取 `config/agent.yaml` 的 `director:` 节——它们
   管参考图清晰度，与画幅无关，搬过来就是又开一处真源。
4. **打包后交代节点**：`film_canvas.node_setup_lines` 打印节点的三项应设值与「包声明
   ≠ 实际执行值」的提醒，用户照抄；这是路线 2 的用户侧落点。
5. 旧会话没有成片画幅答案时，`film_settings` 回落配置声明（不猜、不抛错）；这类会话
   的 `config/agent.yaml` 兜底仍然有效。

## 后果

- 新会话的画幅预检（`director/aspect_flow.precheck_message`）在「关键帧画布 vs 包声明」
  这一对上恒等——它不是失效，而是把守卫焦点让给了贴回核对（锚图 vs 期望画幅）；旧会话
  与配置兜底仍走原判据。
- `--brief` 路线没有 `_phase1_new` 的问答，包参数继续取配置声明；要单次覆写成片画幅，
  用 `画布 <帧模型家族>: WxH` 仍在 `Brief.canvases` 生效（`film_size_for` 读同一格）。
- 插件将来若证实会应用 `output`，删掉 `node_setup_lines` 的打印即可；包与关键帧的
  单一真源不受影响。
- 真实短片验收（#161 AC4）在用户下一次导演台实跑时补：判据是「向导答的画幅 ＝
  `/history` 里的 width/height ＝ 成片宽高」。
