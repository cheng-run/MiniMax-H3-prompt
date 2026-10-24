# 编码与协作规范（Coding Standards）

`/code-review` 的 Standards 轴会扫全仓找「代码该怎么写」的文档，`CODING_STANDARDS.md` 存在就必须在列——本文件就是喂给那条轴的。

**本文件不发明判据。** 每条都标了真源：真源改了改真源，这里只做同步；仓内已经写下的规矩（ADR、守卫测试、`AGENTS.md`）不在这里重抄，只列指针。

## 一、仓内已有的规范载体（先读这些）

- **`CONTEXT.md`** — 术语与 `_Avoid_`。产出里命名域概念时用它定义的词，别漂到它明确避免的同义词；概念不在表里，是要么你在自造语言、要么真缺一条。
- **`docs/adr/`** — 架构决策。产出与既有 ADR 冲突要**显式提出**（「与 ADR-NNNN 冲突，但值得重开，因为…」），不静默覆盖。
- **`AGENTS.md`** — 票据工作流（何时开工作树 / 在哪提交 / 何时推送 / `PYTHONPATH` / 逐票全量）与产物落点。
- **`pyproject.toml`** — pytest `testpaths`，注释写了为什么。
- **`tests/test_context_glossary.py`** — `CONTEXT.md` 的结构守卫。

## 二、语言与产出

| 判据 | 真源 |
| --- | --- |
| 注释与 docstring 一律简体中文，写**为什么**（引 issue 号 / ADR / 实测现场 / 反例），不复述代码在做什么；标识符仍用英文 | 全局 `~/.codex/AGENTS.md`（2026-09-25 用户确认） |
| 英文产出的例外：H3 提示词正文、官方 edge-stability 句必须逐字符英文 | 同上；另见 ADR 0004 |
| 代码标识不裸跑当主语——中文描述词在前、标识括注在后 | 全局 `~/.claude/CLAUDE.md` |

## 三、证据与断言

| 判据 | 真源 |
| --- | --- |
| 报 bug、下结论必须附可复现证据（命令、输出、错误原文）；给不出证据的断言标**「未经证实」**，不得当已确认事实陈述 | 全局 `~/.claude/CLAUDE.md`「Bug 报告证据规则」 |
| 证据只写实测能证的增量：先跑「改前」臂再写证据；票面的问题陈述是**假设**，实测推翻就地更正票面 | auto memory `evidence-scope-claims`（#31/#33） |
| 物证必须与出货代码对应：物证落盘后再改出站文本或文档措辞，等式会**静默变假** → 重跑那一臂，不加豁免 | auto memory `evidence-must-match-shipped-code`（#33） |
| 标「官方原话」就给全，不截后半句；计数写清单位；度量类数字**先落脚本再抄** | auto memory `evidence-quotes-and-counts-precision`（#30/#33） |
| 「路径＝证据」的路径必须带轮次，否则被同流程再写一次后旧行静默指向新内容 | auto memory `evidence-paths-need-round-suffix`（#56） |

## 四、口径与接线

| 判据 | 真源 |
| --- | --- |
| **同一契约不许两处口径**：凡按时长/数量核对，先问「同一个来源吗、允不允许不等」 | auto memory `one-validator-two-totals`（#56 假 `SEGMENT_TIMELINE_MISSING`、#57 末段余量） |
| 新增规则要回答「**谁调用它**」；守卫形态**从常量派生**，不许字面重抄一份（常量一改就静默失效） | auto memory `rule-exists-plumbing-missing`、`guard-shape-must-derive-from-constants` |
| 经 LangGraph 图传的 state 键必须在 `StateGraph` schema 里声明，否则到节点手上静默没有 | auto memory `langgraph-drops-undeclared-state-keys`（#28） |
| 删参数要追传递闭包 | auto memory `rule-exists-plumbing-missing` |
| 官方文本只有一个可写源 | ADR 0008 |

## 五、测试

| 判据 | 真源 |
| --- | --- |
| 守卫/钉合测试要做**变异检验**：扰动来源必须变红（永不为红＝没有守卫） | auto memory `guard-tests-need-mutation-check`（#56 复审硬条） |
| 会真建 LLM 客户端的用例一律打桩（暗读 `.env` 真 key 的用例只在本机能过） | auto memory `h3-ticket-workflow` |
| 加新环节后要跑**受影响的旧用例文件**并看耗时：接线变更会让旧用例开始走新路真发网络请求（信号＝CPU 时间远低于墙钟） | auto memory `wiring-change-makes-old-tests-hit-network`（#56：80s→145s） |
| 给向导加新问答别动用例输入表**末尾**（会被上游队列菜单吃掉、把流程挪一步）；在 `fake_prompt` 里只对那道新题作答 | auto memory `wizard-test-inputs-new-question`（#57：2.25s→249s） |
| 工作树内跑测试先设 `PYTHONPATH=<工作树>\src` 再用主仓 `.venv`，否则测的是主树 | `AGENTS.md`「票据工作流」 |
| 已知的环境性失败一句话带过并点名失败项，不反复归因；已知项不再当新发现陈述 | 全局 `~/.codex/AGENTS.md`「测试基线的报告纪律」 |

## 六、文档

| 判据 | 真源 |
| --- | --- |
| 改 `prompts/*.md` 优先**追加到列表末尾**（行号引用会集体漂且一处都不报错）；必须插中间就逐处挪指针 | auto memory `line-number-citations-drift`（#36 实测 15 处） |
| 给模型读的 `.md` 里**不写 HTML 注释**（`load_role_prompt` 逐字读全文，注释会进系统提示词） | 同上 |
| `CONTEXT.md` 词条＝`**术语**:` + 正文 + `_Avoid_:`，标题行与正文不许错配 | `tests/test_context_glossary.py` |

## 七、提交

| 判据 | 真源 |
| --- | --- |
| 提交信息用中文，形如 `type(scope): 摘要（issue #N）`；复审采纳项单独一条并写明是哪条轴 | 仓内 git log 惯例 |
| 关票＝先发证据评论（`--body-file`）再 `close`；复审意见分列**采纳／不采纳＋理由** | 仓内票面惯例（#47/#54 关票评论） |

---

**auto memory 的位置**：`~/.claude/projects/d-----python-MiniMax-H3-prompt/memory/`（每条一个文件，索引在 `MEMORY.md`）。引用它们是因为这些判据目前只存在于那里——哪天某条进了 ADR 或守卫测试，就把这里的行删掉，让真源说话。
