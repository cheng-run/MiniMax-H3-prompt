# matt skills 工作流地图（本机 41 个 skill 的分工与实测调用画像）

日期：2026-10-30
状态：**只读勘察**——读了 27 份 SKILL.md 正文、比对了上游清单、统计了本项目会话日志；**未改任何 skill、未改本仓代码**。
依据：`~/.cc-switch/skills/`（cc-switch 管理，symlink 进 `~/.claude/skills/`）；上游 `github.com/mattpocock/skills`（`gh api` 取目录清单）。

标注约定：

- **【正文确证】**＝逐字读到的 SKILL.md 内容，附 skill 名。
- **【实测】**＝本机跑命令得到的结果。
- **【未验证】**＝推断或无法在本机证实的部分。

> 本文地图**以 skill 正文为准**。本仓 `AGENTS.md` 对其中三处做过改写（见第六节），所以正文与仓内实际做法不一致的地方是**故意的**。

---

## 一、结论表

| # | 结论 | 依据 |
|---|---|---|
| 1 | 本机 `~/.cc-switch/skills/` 共 **41** 个 skill 目录，其中 **27** 个来自上游 `mattpocock/skills`，**14** 个不是 | 【实测】`comm` 比对五个上游分类 |
| 2 | 27 个里真正的主干只有 6 个，其余是原语、岔路、元层 | 【正文确证】`ask-matt` |
| 3 | `grill-with-docs` 正文只有一行：调 `grilling` + `domain-modeling` 两个原语；`grill-me` 同理但只调 `grilling`（无文档沉淀） | 【正文确证】`grill-with-docs`、`grill-me` |
| 4 | 主干有两条硬纪律：**第 1→3 步不换上下文窗口**；`/implement` **每票之间清上下文** | 【正文确证】`ask-matt`「Context hygiene」 |
| 5 | 实现有两条路线：逐票 `/implement` 与任务图并行 `/implement-spec`，后者只落一条集成分支 | 【正文确证】`implement`、`implement-spec` |
| 6 | `code-review` 的两轴结果**禁止合并排序**——分离就是为了不让一轴遮住另一轴 | 【正文确证】`code-review`「Why two axes」 |
| 7 | 上游还有 **11** 个 skill 本机未安装（多为写作类与脚手架类） | 【实测】`comm -23` |
| 8 | 本项目 51 个历史会话（2026-09-26 → 10-30）里，**`/retro` 调用 0 次**；从未进过场的共 **14** 个 | 【实测】会话日志统计，已排除当前会话文件 |
| 9 | `/retro` 的职能在本仓被 `/evolve`（自写 skill）与记忆体系分流，这是它一次没跑的结构性原因 | 【未验证】推断，无直接证据 |

---

## 二、主干：idea → ship

```
        /grill-with-docs   ← 想清楚（可岔到 /prototype 用一次性代码回答）
              ↓
           /to-spec        ← 综合成 spec（不访谈，只归纳）
              ↓
          /to-tickets      ← 拆成纵向切片票，声明 blocking edges
              ↓
   /implement（逐票）或 /implement-spec（任务图并行）
              ↓  内部都靠 /tdd 红绿推进
        /code-review       ← 双轴复审（Standards + Spec）
              ↓
            /retro         ← 只改「代理的环境」，不改代码
```

| Skill | 干什么 | 关键约束 |
|---|---|---|
| `grill-with-docs` | 访谈式问清楚，同时沉淀 `GLOSSARY.md` 与 ADR | 正文一行：调 `grilling` + `domain-modeling`；有工作目录就用它，没有目录用 `grill-me` |
| `to-spec` | 把当前对话**综合**成 spec 并发到 issue tracker | **明令不访谈**；先与用户确认测试 seam（越靠上层越好，理想 1 个）；模板 7 节；打 `ready-for-agent` |
| `to-tickets` | 拆成 **tracer bullet 纵向切片**票，每张声明 `Blocked by` | 切片竖切穿所有层、单窗口可完成；**宽重构例外**走 expand → migrate 分批 → contract；第 4 步必须让用户过粒度与依赖边 |
| `implement` | 按票实现 | 每票调 `tdd`、常跑单测、末尾跑全量、调 `code-review`、提交到当前分支；**每票之间 `/clear`** |
| `implement-spec` | 整个 spec 一次跑完 | 票当**任务图**只取 frontier；每票独立 worktree + 后台实现子代理，合并走 merger 子代理；全部落**一条集成分支**；最后跑一次 `code-review`；收尾关票或开 PR |
| `code-review` | 两轴并行复审 diff | **Standards**（仓内文档化规范 + Fowler 12 种坏味道基线）与 **Spec**（对不对得上原票）各自独立子代理，**结果禁止合并排序** |

**上下文卫生（`ask-matt` 明写）**：第 1→3 步待在一个不断裂的窗口里（grilling → spec → tickets 建立在同一份思考上），到 `/implement` 才起新会话。天花板是「smart zone」（约 150k token），逼近时在**阶段边界** `/compact`，不要硬顶着降级推理。

---

## 三、原语层（被主干调用，也能单独用）

| Skill | 干什么 |
|---|---|
| `grilling` | 访谈原语：把决定画成**设计树**，按**轮次**问，每轮只问当前 **frontier**（前置已定的问题），每题附**推荐答案**、措辞让"是"＝采纳推荐。**事实归代理**（派子代理查，不许问用户），**决定归用户**。frontier 空＝谈完 |
| `domain-modeling` | 主动维护领域模型：术语冲突当场质疑、模糊词收敛为规范词、决定落地**立刻**写 `GLOSSARY.md`（只放词汇不放实现）；ADR 要同时满足**难回退 / 无上下文会困惑 / 真权衡**三条才提 |
| `codebase-design` | 深模块词汇：module / interface / depth / seam / adapter / leverage / locality；删除测试、"接口即测试面"、"一个 adapter 是假缝、两个才是真缝" |
| `tdd` | 红绿色循环：测试只写在**事先与用户确认的 seam** 上；三类反模式＝实现耦合 / 同义反复 / 横向切片；**重构不属于这个循环**，归 review |
| `writing-for-agents` | 写"给代理读的文档"的规范：context pointer、context load vs cognitive load、信息层级三级阶梯、leading word、**禁止用否定句下指令**（会反向强化）、pruning（单一口径 / 别缓存环境、no-op 就删整句） |
| `wait-what` | 一句话纠正：把上一条消息用 `GLOSSARY.md` 的词汇重讲一遍（正文 7 行） |

---

## 四、岔路与入口

**三个起点入口**（外来工作从这里进来）：

| Skill | 何时用 | 汇入点 |
|---|---|---|
| `triage` | 一堆**不是自建**的 issue / 外部 PR 要过筛（`to-tickets` 产的票已 agent-ready，**不要**再 triage） | 产出带 agent brief 的 `ready-for-agent` 票，交 `/implement` |
| `diagnosing-bugs` | 难 bug / 间歇 flake / 两个已知良好状态之间的回归 | 六阶段，**Phase 1 建一条会变红的紧反馈回路就是全部**；没有回路不许猜假设 |
| `wayfinder` | 大到一个会话装不下、路还看不清的活儿 | 在 tracker 上画**决策票地图**（不是交付物），一张张解；雾散后**交接**给 `/to-spec`，不自己动手 |

**中途绕道**：

| Skill | 干什么 |
|---|---|
| `prototype` | 一次性代码回答一个设计问题，两支：逻辑/状态 → 单个可分享 HTML；长什么样 → 同路由多套激进变体。收敛后答案进真代码，原型留 `prototype/<name>` 分支当一手资料 |
| `research` | 派后台子代理查**一手源**，产出一份带引用的 md 继续干活 |
| `to-questionnaire` | 卡点在**别人脑子里**时写问卷；只访谈"寄给谁、要拿回什么"，不访谈主题 |
| `wizard` | 只有人能做的步骤，生成交互式 bash 脚本逐步引导并写回 `.env`/CI secrets |
| `handoff` | 把对话压成 md 给另一个代理接手；含 "suggested skills" 段；**默认落系统临时目录** |
| `improve-codebase-architecture` | 代码库保健：按近期改动热点找深化机会，出 HTML 报告，挑一个进 `grilling` 变回 idea |

---

## 五、元层

| Skill | 干什么 |
|---|---|
| `ask-matt` | 路由器：描述处境 → 告诉你该用哪个（`disable-model-invocation`，需手打） |
| `setup-matt-pocock-skills` | 首次使用前跑一次：配 issue tracker、五档 triage 标签、domain docs 布局，写进 `docs/agents/*.md` 与 `AGENTS.md` 的 `## Agent skills` 块 |
| `pr` | PR 正文模板：Summary 用**最小的可视化** + Evidence 前后对照 + Merge Danger（**单向门还是双向门** + 影响半径） |
| `retro` | 复盘**代理的环境**而非代码：导航指针、自动检查、编码规范、AGENTS.md 瘦身、工具经济性、no-op 指令、信息可达性。**机械错误→确定性检查；判断题→写规范** |
| `teach` | 多会话教学工作区（`MISSION.md` / `lessons/*.html` / `reference/` / `learning-records/`），与工程流程无关 |

---

## 六、本机实际情况

### 6.1 不是 matt 的 14 个【实测】

`evolve`（本仓自写的中文自进化提案）、`resolving-merge-conflicts`、`frontend-design`（带 LICENSE.txt），以及 11 个工具类插件：`pdf`、`docx`、`xlsx`、`pptx`、`OfficeCLI`、`obsidian-markdown`、`agent-reach`、`archify`、`runcomfy-cli`、`skill-creator`、`find-skills`。

> 判定口径：`gh api repos/mattpocock/skills/contents/skills/{engineering,productivity,misc,in-progress,deprecated}` 取并集，与本机目录名做 `comm`。**名字出现在某 skill 正文里 ≠ 本机装了**，需以此比对为准。

### 6.2 上游有、本机未装的 11 个【实测】

`chief-of-staff`、`claude-handoff`、`git-guardrails-claude-code`、`loop-me`、`migrate-to-shoehorn`、`scaffold-exercises`、`setup-pre-commit`、`setup-ts-deep-modules`、`writing-beats`、`writing-fragments`、`writing-shape`。

### 6.3 本仓 `AGENTS.md` 已改写的三处接缝（正文与本仓不一致是故意的）

- `/to-spec` 的 spec **先整篇贴对话给用户审查**，通过后才发 issue（覆盖 skill 默认的 `gh issue create`）；
- `/implement-spec` 收尾**停在集成分支**，不落 main、不 push；集成分支按票拆回主分支提交；
- `/implement-spec` 第 2 步的探索笔记**必须写进 `docs/research/`**（覆盖默认的"仓库外目录"）。

---

## 七、实测调用画像（本项目 51 个历史会话）

**口径**【实测】：`.jsonl` 里 `<command-name>/xxx` ＝用户手打的斜杠命令；`Skill` 工具的 `input.skill` ＝模型自动调用。**统计前排除当前会话文件**——会话日志实时写盘，grep 会命中自己刚产出的输出（本轮实测：`/retro` 首次统计得 1，实为 0）。

**时间跨度**：2026-09-26 → 2026-10-30。

| 手打斜杠命令 | 次数 | | 模型自动调用 | 次数 |
|---|---:|---|---|---:|
| `/clear` | 40 | | `domain-modeling` | 28 |
| `/implement` | 32 | | `tdd` | 23 |
| `/grill-with-docs` | 29 | | `grilling` | 23 |
| `/to-tickets` | 17 | | `code-review` | 18 |
| `/to-spec` | 15 | | `diagnosing-bugs` | 16 |
| `/implement-spec` | 12 | | `writing-for-agents` | 4 |
| `/handoff` | 12 | | | |
| `/model` | 11 | | | |
| `/ask-matt` | 7 | | | |
| `/evolve` | 2 | | | |
| `/compact` | 2 | | | |
| `/retro` | **0** | | | |

**从未进场的 14 个**：`retro`、`triage`、`wayfinder`、`prototype`、`research`、`to-questionnaire`、`wizard`、`teach`、`pr`、`setup-matt-pocock-skills`、`wait-what`、`grill-me`、`codebase-design`、`improve-codebase-architecture`。

**读法**：本项目实际只用了主干（grill → spec → tickets → implement → code-review/tdd），外加 `handoff`、`diagnosing-bugs`、`ask-matt`；所有「岔路」与「保健类」skill 一次没进过场。

---

## 八、边界与未验证

1. 调用统计只覆盖**本项目**日志目录（`~/.claude/projects/d-----python-MiniMax-H3-prompt/`），其他项目分开存放；且以现存 51 个文件为准，更早会话若已清理则不在统计内。【实测】
2. `retro` 与 `evolve` 的职能重叠是**按正文对照的推断**，本轮未做"同一现场两套流程各自产出什么"的对照实验。【未验证】
3. skill 正文随上游更新会变；本笔记的引用均未附行号（正文短、整篇可重读），重读时以 `~/.cc-switch/skills/<name>/SKILL.md` 为准。
4. 本笔记不含任何对 skill 行为的运行结果——所有"会怎样"都来自正文陈述，不是实测。
