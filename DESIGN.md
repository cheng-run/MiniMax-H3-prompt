---
name: MiniMax H3 工作台
description: 单人本地视频提示词工作台，视觉世界是「状态是线段」——一种字号，靠字距与墨色分层，靠线型表达状态
colors:
  canvas: "#111314"
  plate: "#191c1e"
  plate-raised: "#212528"
  rule: "#2c3134"
  rule-strong: "#3d4448"
  ink: "#e6e3da"
  ink-soft: "#a8a89f"
  ink-faint: "#6f7476"
  signal: "#e0a93b"
  signal-ink: "#1a1408"
  state-live: "#e6e3da"
  state-done: "#8d9295"
  state-pending: "#4d5356"
  state-dead: "#6f7476"
  error: "#d9573d"
  selection: "#3a3222"
typography:
  display:
    fontFamily: "Archivo, system-ui, -apple-system, 'Segoe UI', 'Microsoft YaHei', 'PingFang SC', sans-serif"
    fontSize: "1.75rem"
    fontWeight: 500
    lineHeight: 1.55
    letterSpacing: "-0.02em"
  lead:
    fontFamily: "Archivo, system-ui, -apple-system, 'Segoe UI', 'Microsoft YaHei', 'PingFang SC', sans-serif"
    fontSize: "1rem"
    fontWeight: 500
    lineHeight: 1.55
  body:
    fontFamily: "Archivo, system-ui, -apple-system, 'Segoe UI', 'Microsoft YaHei', 'PingFang SC', sans-serif"
    fontSize: "0.875rem"
    fontWeight: 400
    lineHeight: 1.55
  micro:
    fontFamily: "Archivo, system-ui, -apple-system, 'Segoe UI', 'Microsoft YaHei', 'PingFang SC', sans-serif"
    fontSize: "0.75rem"
    fontWeight: 400
    lineHeight: 1.55
    letterSpacing: "0.04em"
  data:
    fontFamily: "ui-monospace, 'SF Mono', Consolas, 'Cascadia Mono', monospace"
    fontSize: "0.75rem"
    fontWeight: 400
    lineHeight: 1.55
rounded:
  sm: "2px"
spacing:
  1: "4px"
  2: "8px"
  3: "16px"
  4: "24px"
  5: "32px"
  6: "48px"
components:
  button-primary:
    backgroundColor: "{colors.signal}"
    textColor: "{colors.signal-ink}"
    rounded: "{rounded.sm}"
    height: "32px"
    padding: "0 16px"
  button-secondary:
    backgroundColor: "transparent"
    textColor: "{colors.ink}"
    rounded: "{rounded.sm}"
    height: "32px"
    padding: "0 16px"
  button-ghost:
    backgroundColor: "transparent"
    textColor: "{colors.ink-soft}"
    rounded: "{rounded.sm}"
    height: "32px"
    padding: "0 16px"
---

# Design System: MiniMax H3 工作台

## Overview

**Creative North Star: "状态是线段"**

这套界面借的是暗房里的分光板：一块炭灰连续底，上面只有 1px 的发光细线，每条线代表一个会话、一个步骤、一条待办。它的主张是**状态不该靠颜色传达**——色觉之外还得读得出「这条在跑、这条停了、这条在等你」，而线天生就能同时承载这么多种差异：实线、虚线、加倍、短一截、划掉。

因此全站只有一种无衬线体（Archivo，中文回落系统黑体）、**一种字号层级**。层级不靠字号大小铺排，靠字距（全大写铭牌 0.14em）、墨色深浅（骨白／次要灰／最弱灰）与线的形态。**钠黄是唯一亮色**，只出现在「现在最要紧的那一条」上——等待人工的那一行、当前步骤、焦点环、主按钮。

这套系统在 2026-11-21 替换了此前的「无形的仪表盘」。旧系统的问题是层级全靠大小与圆角白卡，二十行会话摊开来像一张没排完的表；新系统的每一条会话都有自己的线形，扫视时先读线再读字。

**Key Characteristics:**
- 炭灰连续底 + 骨白铭牌 + 单一钠黄
- 一种字号，靠字距与墨色分层
- 1px 发光线取代全部边框与阴影；零圆角（控件留 2px）
- 状态 = 线的形态（实／双／短／划）
- 深色为默认（暗房看片），浅色是纸上校对版

## Colors

冷灰中性底打底，钠黄承担全部强调，红只在错误一档出现。状态不靠颜色区分，靠线型。

### Primary
- **钠黄** (#e0a93b): 唯一亮色。等待人工的刻度、当前步骤、主按钮底、焦点环、当前导航项的下划线。全站面积占比极小，「稀」就是它的意思。
- **钠黄上的字** (#1a1408): 钠黄底上必须用这一档深色，白字在上面只有 2.1:1。

### Neutral
- **炭灰底** (#111314): 页面底。深色主题默认。
- **板面** (#191c1e): 铭牌行、悬停行的底，比画布略浅。
- **浮起** (#212528): 等待人工那一行、被指向的行、次级按钮悬停。系统里最深的一档「抬起」。
- **分隔线** (#2c3134): 所有 1px 分隔与导轨基线。系统里唯一的线性元素。
- **强调线** (#3d4448): 输入框边框、次级按钮边框。
- **骨白** (#e6e3da): 正文。不是纯白，纯白在炭灰上刺眼。
- **次要灰** (#a8a89f): 铭牌、说明、元信息。对 #111314 是 8.1:1。
- **最弱灰** (#6f7476): 未开始的刻度、禁用文字、失活刻度。对 #111314 是 3.9:1，只用于非正文。**占位文字已从这里撤出**——它是要读的字，改用次要灰（2026-11-22：浅色主题下最弱灰只有 2.84:1，连占位都不合格）。

### Semantic
- **进行中** (#e6e3da): 实线全高，最亮。
- **已完成** (#8d9295): 短一截的实线，中灰。
- **未开始** (#4d5356): 极暗实线。
- **失活** (#6f7476): 已取消的刻度。**不用于任何正文**——3.94:1 不到 AA。
- **错误** (#d9573d): 失败/中断的刻度与错误文字。单给一档偏红的钠黄，与「要你做事」明确分开。

### Named Rules
**The Signal Rarity Rule.** 钠黄只出现在「现在最要紧的那一条」上。它不是品牌色，是警报色——用多了就不响了。

**The Never-Colour-Alone Rule.** 状态永远同时有线型与文字标签。只看颜色分不出状态，等于没分。

**The One Type Size Rule.** 层级靠字距与墨色，不靠字号。全站四档字号（micro / body / lead / display），display 一次只出现一次。

**The Measured-Contrast Rule.** 浅色的钠黄是 `#8f5c10` 而不是深色那档（#b8791a 对浅色画布只有 3.12:1）。两套主题的每个前景色都对画布实测过 ≥4.5:1；`--color-ink-faint` 因为 3.94:1 被排除在正文之外。

**The Micro-Is-For-Western Rule.** micro 档（0.75rem）是铭牌与西文小件的尺寸，**不承担中文正文**。微档此前是 11px，徽标、行元信息、计数这些中文 11px 下笔画粘连（2026-11-22 实测）——提到 12px 后仍属于「小字」，但中文可读。真·正文（会话标题、剧本段落）一律 body 档（14px）起步。

## Typography

**Display Font / Body Font:** Archivo（自托管 woff2，随包分发，EXE 形态不依赖网络）
**Label/Mono Font:** 系统等宽（`ui-monospace, SF Mono, Consolas`），只用于数字与路径。

**Character:** Archivo 是窄体建筑感的 grotesk，小字号下字面紧凑、字距稳定，正适合「一行一续」的导轨密度。中文回落到系统黑体——中日文与西文混排时字面高度一致，不需要第二套字体去调。

### Hierarchy
- **Display** (500, 1.75rem, 1.55, -0.02em): 产品名。全站只出现一次。
- **Lead** (500, 1rem, 1.55): 页面标题、板面标题。
- **Body** (400, 0.875rem, 1.55): 列表行、正文、按钮。
- **Micro** (400, 0.75rem, 0.04em): 铭牌、元信息、刻度标签、计数。
- **Data** (400, 0.75rem, 等宽, tabular-nums): 波长、修订号、条数、路径。

### Named Rules
**The No-Tracking-On-Chinese Rule.** 中文标题不加字距。`--track-wide`（0.14em）只给全大写的西文铭牌，中文加了会散成「一个 一 个 字」。

**The Tabular Rule.** 任何会变化的数字（修订号、条数、时长、页码）走等宽 + `tabular-nums`，否则 9→10 时整列错位。

## Layout

**满幅构图。** 列表页不锁最大宽度——行从导轨一路铺到右缘，那是「摊开的板面」的口气。行内容本身有 1500px 上限，免得超宽屏上标题横跨全屏。详情页 880px，`≥1200px` 放宽到 1080px；正文块单独收到 72ch。

列表页主体是 **导轨 + 板面** 两列 grid：导轨固定 56px（窄屏 28px），承载纵向基线与每行的刻度；板面承载行内容。行高固定 44px，行与行靠 1px 分隔线，不用卡片、不用间隙。

列表上方还有一条 **波长轴**：一页会话压成一行竖刻度，等待人工那根是双线钠黄——扫一眼轴就知道本页分布，不必先读完二十行。

间距六档：4 / 8 / 16 / 24 / 32 / 48px。区块之间 24px，主要分区之间 32–48px。

响应式只有一个断点：`< 720px`。窄屏导轨收窄到 28px、行改成两行折行（标题占满第一行，状态与元信息第二行），悬停指示线关掉（44px 步进是鼠标桌面的语法）；刻度右端仍顶在基线上，不脱开。

### Named Rules
**The Rail Owns The Left Rule.** 左导轨是列表页的骨架，不是装饰。任何新增行状态都必须先回答「它在导轨上长什么样、在波长轴上长什么样」。

## Elevation & Depth

**平面分层，零阴影。** 深度由两件事表达：

1. **底色差**：炭灰 → 板面 → 浮起，三档极近的灰叠出层次。
2. **1px 发光线**：`--color-rule` 的实线边界，系统里唯一的层级标记。

不用阴影的理由：这是几十行密集排布的列表界面，阴影在小尺寸下反而模糊边界；而且这个世界的语法里，「浮起」由灰阶差与线型共同表达，阴影是另一套语言。

### Named Rules
**The Flat-By-Default Rule.** 表面一律平。层次靠灰阶差与 1px 线，不靠 `box-shadow`。

## Shapes

**零圆角。** 按钮、铭牌、输入框留 `2px`（免得看起来像没画完），其余全部方角。没有胶囊、没有大圆角卡片。

状态标记的形态全部由线构成：横刻度、双线、短刻度、斜线、下划线、竖分隔线。**任何「盒子一侧的色条」都是禁止的**——那不是这个世界的语法，是卡片装饰。

## Components

### Buttons
- **Shape:** 2px 圆角，高度 32px
- **Primary:** 钠黄底 + 深色字。只用于页面上「现在该点的那一下」。
- **Secondary:** 透明底 + 1px 强调线边框 + 骨白字。悬停时线先亮、再垫一层浮起底。
- **Ghost:** 无线无底，只有次要灰的字；用于同一区域的第三档出口。
- **Hover / Active:** 主按钮靠 `filter: brightness(1.08)` 提亮，不换色相——主题切换时两种底都成立。次级按钮悬停垫底色 + 边框转亮。
- **禁用:** 全局 0.38 透明度 + `not-allowed`。
- **Focus:** `:focus-visible` 的 2px 钠黄描边，`outline-offset: 2px`。

### Inputs / Fields
- **Style:** 透明底、1px `--color-rule-strong` 边框、2px 圆角、`--text-body`。样式在 `styles/reset.css` 里统一给 `input`（排除 checkbox/radio/range/file）/`textarea`/`select`，不靠各组件自己写。
- **Focus:** 边框转钠黄 + 去掉默认 outline；`caret-color` 也是钠黄。
- **File picker:** `::file-selector-button` 单独造型——浏览器原生部件同样属于设计系统。
- **Error / Disabled:** 错误文字用 `--color-error`；禁用走全局透明度。
- 浏览器自己的部件同样属于设计系统：`::selection` 用 `--color-selection`，滚动条用 `--color-rule-strong`，数字列等宽。

### Status Badge（线段语言）
- **形态:** 左侧一根 14px 小线段 + 右侧文字。线段 `aria-hidden`，读屏只读文字。
- **Tone:** `attention` 双线钠黄 + 字加粗；`ok` 单实线中灰；`error` 短划线红；`neutral` 极暗线。
- **行为:** 始终带文字，不只靠颜色。状态名来自集中映射表（`status.ts`），不在组件里硬编码。

### Markdown 正文（产物、段词、注入卡）
产物正文是模型写的 Markdown，统一由 `MarkdownBlock` 渲染——它只负责**排版**，外框
（1px 线、板面底、最大高度、72ch 限宽）由所在分区在同一类名上给。两条硬约束：

- **禁用原始 HTML**（`html: false`）。正文里的 `<Picture 1>`、`<Subject 5>`、`<d>…</d>`
  是接线时要照抄的**占位符**（实测 770 处），允许原始 HTML 会让它们被解析吞掉。
  同一条禁用也消除了注入面；链接再只放行 http(s)、外链补 `noopener`。
- **复制与下载取原文**，不取渲染结果。渲染是给人看的，产物文件本身一字不动。

排版服从线段语言：标题只多半档（lead 档 500 骨白，不铺字号）、粗体是 500 + 骨白、
行内代码等宽 + 浮起底、分割线就是 1px 发光线、引用块左侧竖线、表格 1px 线无斑马底。
标识符里的下划线（`integrated_multimodal_description` 等 941 处）按标准不触发斜体。

### Signature Component: 会话导轨列表页左侧的纵向基线，每条会话在它上面刻一道横线。线型承载七种状态：

| 状态 | 线形 |
|---|---|
| 进行中 | 实线，18px 全宽，最亮 |
| 等待人工 | **双线**（两根 2px 平行，间隔 2px），钠黄，行底浮起 + 行尾 `NEXT` 铭牌 |
| 已完成 | 短一截（9px）的实线，中灰 |
| 已创建 | 极暗实线 |
| 已中断 / 已取消 / 失败 | 整根刻度自己倾斜 45°（**不是**在横线上叠斜线——那会读成「>」箭头）。失败用红，中断与取消用中灰/失活灰 |

**徽标与刻度必须同口径**：`interrupted` 是 neutral（中灰斜划），`failed` 才是 error（红）。两处映射都住在 `status.ts`，改一处必须同时想另一处。

**签名动效：** 被指向的行，导轨上多出一根钠黄指示线，用 `translateY(行号 × 44px)` 落到该行，指数缓出（`cubic-bezier(.19,1,.22,1)`）。键盘 Tab 同样触发。

### 动效（2026-11-22 增补）

动效只做三件事：**解释入场、解释切换、解释反馈**。全站只有一个编排过的时刻，其余一律短促。

**落线（唯一编排时刻）。** 列表数据到手时，波长轴的刻度从基线上依次向上长出来，同时每行导轨刻度的可见范围从基线向两侧展开。这是「一张谱线板被曝光」——这个世界自己的比喻，不是通用的淡入上升。
- 刻度：`scaleY(0) → 1`，`transform-origin: 50% 100%`（底边即基线）。
- 行刻度：`clip-path: inset(−2px 50%) → inset(−2px)`，从横向基线向两边展开。**不用 scaleX**——刻度宽度承载状态（已完成是半宽），scaleX 会把它压成另一种长度。
- 节奏：`--duration-line` 260ms、`--stagger-line` 10ms、延迟封顶 `--stagger-cap` 12 格，总时长 **120 + 260 = 380ms**，不随行数膨胀（20 行与 12 行同时收尾）。
- 曲线单独一条 `--ease-grow`（`cubic-bezier(.4,.1,.2,1)`）：生长要前段缓。用 `--ease-expo` 时 40ms 已长到 57%，读起来是「啪一下出现」，不是「长出来」。
- 翻页重播（`:key="pageIndex"`）；同一页内数据刷新不重播。

**连续性。** 详情页分区切换 180ms、新建表单展开 220ms，都用 `--ease-out`。它们会随用户操作反复出现，所以必须短——Operate 模式不让人等编排。

- **替换型内容只动位置，不动透明度**（The No-Blank-Swap Rule）。分区切换是旧面板被 `v-if` 移走、新面板挂上来；若新面板从 `opacity: 0` 起播（`animation-fill-mode: both` 会让首帧就应用 `from`），切换那一帧整块区域是空的，读起来像闪白。这类内容该从「差一点到位」开始（`translateY(-4px)`），不是从「看不见」开始。**从无到有**的内容（新建表单、等待提示）才用淡入，因为那时候页面上本来就没有东西可被替换。

**反馈。** 「等待你操作」横带 220ms：`clip-path` 从上下两端向中间揭开 + 淡入，呼应「一条被点亮的基线」。按钮、tab、导航、列表行都有 `:active`（按下再压一档），不只 hover。

**预算与降级。** 只用 `transform` / `opacity` / `clip-path`，不触发布局，无滚动监听、无逐元素 JS。`prefers-reduced-motion` 下：入场编排与循环动画整条关掉（刻度直接以终态出现，`clip-path` 复位），颜色与透明度保留 100ms——**不是**把所有动效压成 0，否则点下去像没反应。

### Signature Component: 波长轴
列表正上方的一条横轴：本页每个会话压成一根竖刻度挂在 1px 基线上，等待人工那根加倍 + 钠黄，已死三态矮一截。与导轨七态同构，只是缩成一行——扫一眼轴就知道这一页的分布，不必先读二十行。纯装饰，`aria-hidden`；读屏由页眉那句「N 条记录 · M 条要你动手」承担。

### Tabs
- **Style:** 纯文字 + 底部 2px 指示线。没有白底盒子、没有三边框。
- **选中:** 骨白 + 底部钠黄线 + 字重 500。
- **待办计数:** 分区名后跟一个 `--text-micro` 的方括号数字，钠黄；`data-count` 属性承载，`aria-label` 给完整一句（「审查与待办，1 项等待操作」）。
- **Keyboard:** WAI-ARIA tabs 规范，roving tabindex，方向键 + Home/End。
- **窄屏:** `flex-wrap: wrap` 折行，不改宽度。

### Signature Component: 步骤带
详情页顶部的流程指示器。一条贯穿的水平 1px 基线，七个步骤是挂在基线上的刻度（2px × 12px）。

- `done`：实心刻度，中灰
- `current`：**双线 + 钠黄**，基线之上再落一枚 45° 三角指示器
- `pending`：极暗刻度
- 超过 7 步时横向滚动，右缘一道 12px 渐隐提示还有内容
- 无障碍：`aria-current="step"`；每格有 `sr-only` 完整播报（「第 3 步（共 7 步）素材循环，12/12，已完成」）；整条带子只有**一个** `role="status"`。

### Navigation
- **Top bar:** 56px 高，板面底，底部 1px 线。左产品名（micro 大写 + 宽字距）+ 竖分隔线 + 两项导航；右侧主题切换。
- **当前项:** 底部 2px 钠黄下划线。路由按前缀判断（`/sessions/<id>` 仍算「会话」）。
- **主题切换:** 单按钮三态轮转（跟随系统 → 深 → 浅），`aria-label` 同时给当前态与下一态。

### Error Notice
- **Style:** 板面底 + 1px `--color-error` 全边框 + 2px 圆角。**不用一侧色条**。
- **行为:** `role="alert"`，操作按钮由使用方通过插槽注入。

## Do's and Don'ts

### Do:
- **Do** 让状态同时有线型与文字。刻度、徽标、步骤带都是这样。
- **Do** 钠黄只给「现在最要紧的那一条」。
- **Do** 用 1px 线与灰阶差分层次，不用阴影。
- **Do** 数字一律等宽 + `tabular-nums`。
- **Do** 焦点态统一 2px 钠黄描边 + 2px 偏移，覆盖所有交互元素。
- **Do** 在 `prefers-reduced-motion` 下关掉入场编排与循环动画，但保留颜色与透明度的短过渡——反馈不能减到零。
- **Do** 替换型内容只做位移，从 `opacity: 0` 起播会闪出一帧空白。
- **Do** 渲染产物 Markdown 时禁用原始 HTML——`<Picture N>` 是占位符，不是标签。
- **Do** 术语与 `CONTEXT.md` 保持一致（分段、素材、产物、交付、导演台）。
- **Do** 让「要你动手」的状态在列表里跳出来（双线刻度 + 浮起底 + `NEXT` 铭牌），并把状态写进 `document.title`。

### Don't:
- **Don't** 引入第二个强调色。红只给错误，不是第二个强调色。
- **Don't** 用「盒子一侧的色条」（`border-left` / `inset box-shadow` / 伪元素）标记任何状态——换成全边框、整块底色，或前置一个图标／标签。
- **Don't** 用阴影、渐变文字、玻璃拟态。
- **Don't** 给中文标题加字距。
- **Don't** 把状态表达成纯色块或纯图标。
- **Don't** 在圆角白卡里装列表——这个世界没有卡片流。
- **Don't** 用回弹／弹性缓动（`bounce` / `elastic` 曲线）。落位用指数缓出。
- **Don't** 用 `--color-ink-faint` 写任何正文——它只有 3.94:1，是注脚与禁用态的档。
- **Don't** 在界面里编造数据、进度百分比或评价——服务端是唯一真源。
