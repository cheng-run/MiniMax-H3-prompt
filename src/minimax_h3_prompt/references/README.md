# 官方规范 references（给人读的对照物）

这两份 vendor 进来的 H3 提示词规范，供人查阅与核对：

- `base-en.txt` —— base 模式（T2VA / I2VA / FL2VA / L2VA）的字段、结构与句法；
- `ref-en.txt` —— ref 模式（参考图 / 视频 / 音频）的六段式结构与 retention 标记。

**`ref-en.txt` 与上游逐字节一致；`base-en.txt` 不是**——它有 3 处本仓增补，逐条登记在下面的账本里。
读到 `base-en.txt` 的任何一句话，先在这里查它是不是本地加进去的，**再决定能不能叫「官方」**。

## 账本：三处本地增补（2026-11-01 核定）

| | 上游 | 仓内 |
|---|---|---|
| 来源 | HF `MiniMaxAI/MiniMax-H3` › `docs/VIDEO_PROMPT_WRITING_GUIDE_base_en.md`（GitHub `MiniMax-AI/MiniMax-H3` › `skills/h3-prompt-writing/references/base-en.txt` 与之逐字节相同） | `src/minimax_h3_prompt/references/base-en.txt` |
| 体量 | 15,773 B / 222 行 | 16,352 B / 228 行（多 6 行 / 579 B） |
| sha256 | `2cfebc096a6e08370f288d468d90b60f7f9bcb938f94bf090816e910e48e75fc` | `cb274aa8508344c16a0d19f3fb2c866c5a66d5c0166cee29150fb1841996b905` |

上游文本自 2026-08-03 初始化后内容未变；本仓 3 处增补由 `486a45d`（2026-09-15）一次加入。

| # | 副本行号 | 内容 | 定性 | 依据 |
|---|---|---|---|---|
| 1 | `:86` 后半句 | `Cut times must be whole seconds (the milliseconds component must be \`.000\`), because the ComfyUI H3 duration option only offers integer-second choices in the 4-10s range; each shot's implied duration must also be an integer within 4-10 seconds:` | **本仓执行约束** | ComfyUI 的 H3 节点确实只给 4–10 整秒档。官方同段示例写的是 `At 00:03.500`（半秒），直接反证「毫秒位必须 `.000`」不是官方格式规范 |
| 2 | `:89` | `[Shot 2] At 00:04.000, the camera cuts to...`（上游为 `At 00:03.500`） | **本仓改动** | 配合第 1 条改的示例。它已不是官方句法样本，**不要拿它当「官方这么写」的证据** |
| 3 | `:92-96` | `Additionally, end every shot block with this exact edge-stability sentence …` 及其下 ```text 块中的 `Keep every character's silhouette …` | **来源不明 → 按「本仓经验句」用** | 2026-11-01 在 HF、GitHub、MiniMax 平台文档三处均未见该段（`grep -i "edge-stability\|silhouette\|whole second"` 零命中），Exa 全网检索该句原文零命中。既无法证实为官方，也无法证伪；**保留它的理由是本项目自己的桥接帧链**（抽出的尾帧要保持轮廓锐利，留给下一段当首帧参考），不是「官方要求」 |

行号差 6 全部落在 §4.2 这一段，所以全仓 `base-en.txt:92-96` 这类引用指的是**副本行号**，
上游没有对应行。前 85 行与上游完全一致。

### 口径纪律

- 第 1、2 条可以叫「本仓执行约束」，**不可以叫「官方规范」**；
- 第 3 条可以叫「我们的 edge-stability 句」「本仓经验句」，**不可以叫「官方明令要求」**——
  除非哪天真在上游找到原文，那时把它降级进账本并把上游链接贴在这里。

## 定位（ADR 0008）

**它们是给人读的参考与对照物，不是运行时的输入。** 流水线一条请求都不会读这两份文件——
加载链上没有模板引擎、也没有解析链（官方原文是散文，靠位置去解析既脆又会让 `h3_validator`
的 import 期崩给全程序看）。**格式问题一律以这里为唯一依据**，但那是**对人**说的：
读它、照着它核对产出。

要逐字符照抄的原文，在**代码里**的唯一可写来源是 `tools/h3_validator.py` 的常量
（「唯一」指的是**出站文本的来源**：正样本 fixture 与各条用例里内联的同句是**样本**、
CONTEXT / ADR / playbook 里的是引文，它们不是来源）：

| 常量 | 内容 | 谁用 |
|---|---|---|
| `EDGE_STABILITY_SENTENCE` | 镜头块收尾句（账本第 3 条） | 校验器、两条写段模板（插值）、角色提示词（由加载处注入） |
| `ALIGN_TEMPLATES` | 帧变体首行对齐指令（I2VA / FL2VA / L2VA），上游原文 | 同上 |
| `BASE_SECTIONS` / `REF_SECTIONS` / `RETENTION_MARKERS` | 字段名与 retention 标记，上游原文 | 校验器 |

`tests/test_official_references.py` 读这两份文件，把每个常量**逐字钉回**它们，并反向钉住
「被判自创结构的标记在原文里确实找不到」。**这里要说清它钉的是谁**：它钉的是**仓内副本**，
不是上游——

- 它证明「常量 ↔ 副本一致」，**不证明「副本 ↔ 上游一致」**。上面 3 处增补就是在它眼下发生的，
  一条都没报红，因为它读的就是改过的那份。
- 于是：改常量而不同步副本 → 报红；**重新 vendor（整份替换）而不同步常量 → 报红**
  （这是有意的：措辞一变，进模型请求的文本就跟着变，值得一次人工确认，而不是悄悄跟着漂）。
- 「副本 ↔ 上游一致」只能靠人按上面的 sha256 核对，机器不管。

## 维护

**不要顺手改这两份文件的正文**——它们是核对物，改了就失去核对价值。三条规矩：

1. 上游更新时整份替换，再按守卫测试的报红逐条核对常量。
2. **整份替换会静默丢掉账本里的 3 处增补**。替换前先决定：在新副本上重新增补并更新本账本，
   还是把增补搬进代码常量 / 知识真源（`knowledge/film-language-video.md`）后不再回写副本。
   两种都行，但**必须是一次有记录的取舍**，不能换完就忘。
3. 任何新的本地增补都要登记进账本，并在此处注明「本仓执行约束」还是「本仓经验」——
   不登记就等于伪装成官方原文。
