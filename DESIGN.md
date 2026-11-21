---
name: MiniMax H3 工作台
description: 单人使用的本地视频提示词工作台，主题是「如实呈现流程状态」
colors:
  canvas: "#f7f7f8"
  surface: "#ffffff"
  ink: "#1c1c1e"
  ink-muted: "#5a5a62"
  border: "#d7d7dd"
  accent: "#2f6feb"
  accent-on-canvas: "#2c66e0"
  focus: "#1a73e8"
  ok-bg: "#e6f4ea"
  ok-ink: "#1b5e20"
  error-bg: "#fdecea"
  error-ink: "#b3261e"
typography:
  title:
    fontFamily: "system-ui, -apple-system, 'Segoe UI', 'Microsoft YaHei', 'PingFang SC', sans-serif"
    fontSize: "1.125rem"
    fontWeight: 600
    lineHeight: 1.5
  body:
    fontFamily: "system-ui, -apple-system, 'Segoe UI', 'Microsoft YaHei', 'PingFang SC', sans-serif"
    fontSize: "1rem"
    fontWeight: 400
    lineHeight: 1.5
  label:
    fontFamily: "system-ui, -apple-system, 'Segoe UI', 'Microsoft YaHei', 'PingFang SC', sans-serif"
    fontSize: "0.875rem"
    fontWeight: 400
    lineHeight: 1.5
rounded:
  sm: "4px"
  md: "8px"
spacing:
  1: "4px"
  2: "8px"
  3: "16px"
  4: "24px"
  5: "32px"
components:
  button-primary:
    backgroundColor: "{colors.accent}"
    textColor: "#ffffff"
    rounded: "{rounded.sm}"
    padding: "8px 16px"
  button-secondary:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.sm}"
    padding: "8px 16px"
---

# Design System: MiniMax H3 工作台

## Overview

**Creative North Star: "无形的仪表盘"**

这套界面服务于单人完成一件复杂的、多阶段的手工活。它的成功标准不是被看见，而是**不打扰**：用户盯着它的时候，应该只看见「我现在在哪一步、下一步做什么」，看不见界面本身。因此视觉纪律全部投向信息密度、状态可辨、术语一致，而不是表达欲。

密度是紧凑的。列表一条一行，卡片不堆叠阴影，分隔靠 1px 边框而不是色块。所有产物都是有价值的（真实的剧本、分镜、提示词），界面不该用装饰去和内容抢注意力。

系统当前处于**部分建成**状态：颜色、间距、圆角三组 token 已经建立且产品代码 100% 遵守；字号刻度、表单控件、响应式、动效四块是空缺。本文件记录既有事实，同时标出缺口，供后续补齐。

**Key Characteristics:**
- 冷灰中性底 + 白色卡面 + 单一蓝色强调
- 平面分层（无阴影），靠 1px 边框与底色差表达层级
- 系统字体栈，不引入 web font
- 零过渡动效，交互反馈靠即时的状态切换
- 状态不只靠颜色传达（同时有文字标签）

## Colors

冷灰中性色打底，单一蓝色承担所有交互强调，绿色与红色只用于成功与失败。

### Primary
- **工作蓝** (#2f6feb): 主按钮背景、卡面上的链接文字、当前步骤边框。整个界面唯一的「活」色，占据的面积极小。
- **画布上的工作蓝** (#2c66e0): 落在画布灰上的文字链接。`#2f6feb` 在 `#f7f7f8` 上只有 4.27:1，不到 AA 的 4.5:1（2026-11-21 实测）；白卡面上仍是 4.57:1，所以只有落在画布底上的文字换这档更深的蓝。强调色本身不动。

### Neutral
- **画布灰** (#f7f7f8): 页面底色。比纯白略灰，让白色卡面能浮起来而不需要阴影。
- **卡面白** (#ffffff): 卡片、面板、tab 选中态、输入区背景。
- **正文墨** (#1c1c1e): 主要文字。接近黑但不是纯黑，避免在灰色底上显得刺眼。
- **次要灰** (#5a5a62): 元信息、说明文字、时间戳。保持 4.5:1 对比度。
- **分隔线** (#d7d7dd): 所有边框与分隔。系统里唯一的线性元素。

### Semantic
- **成功** (#e6f4ea 底 / #1b5e20 字): 已完成状态徽标。
- **失败** (#fdecea 底 / #b3261e 字): 错误提示、失败状态徽标。

### Named Rules
**The One Accent Rule.** 蓝色是唯一的强调色，且只出现在「可以点」或「当前所在」的元素上。没有装饰性的蓝色，没有次要强调色。

**The Never-Color-Alone Rule.** 任何状态都不能只靠颜色表达。徽标同时带文字标签，错误同时带 `role="alert"` 与说明文字。

## Typography

**Display Font:** 系统 UI 字体栈（`system-ui, -apple-system, Segoe UI, Microsoft YaHei, PingFang SC`）

**Character:** 不表现。选系统字体是为了零加载、零渲染延迟，以及中日文与西文混排时的一致的字面高度。这套界面里文字是工具，不是声明的载体。

### Hierarchy
字号全部走 `--font-size-*` 令牌（2026-11-21 polish 收敛：此前 7 处 `0.95rem` + 1 处 `1.0625rem` 是裸值）。

- **Title** (`--font-size-title`, 600, 1.125rem / 18px, 1.5): 页面主标题（会话列表的「会话」、详情页的剧情标题）。
- **Subtitle** (`--font-size-subtitle`, 400, 0.95rem / 15.2px, 1.5): 卡片内 `h3` 小标题。7 处语义一致，压过正文但不与页面标题抢——这是系统真需要的一档，不是随手写的数。
- **Body** (`--font-size-body`, 400, 1rem / 16px, 1.5): 正文、表单项、按钮文字。
- **Label** (`--font-size-label`, 400, 0.875rem / 14px, 1.5): 元信息、辅助说明、错误提示。使用频次最高的一档（18 处）。
- **Caption** (`--font-size-caption`, 0.8125rem / 13px): 少量密集信息。
- **Count** (`--font-size-count`, 600, 0.75rem / 12px): 分区 tab 上的待办计数徽标。只用于徽标里的单个数字。

### Named Rules
**The Token-Scale Rule.** 字号只从 `--font-size-*` 取。新加一档必须先把它写进 `tokens.css` 并说清它回答什么问题——裸 `font-size: 0.95rem` 这种既不成体系、也无从追问语义。Count 档只服务徽标数字，不参与正文层级。

## Layout

单一列流式布局，无侧栏。列表页最大宽度 880px，详情页 880px、`≥1200px` 放宽到 1080px。两处都 `margin-inline: auto` 居中（2026-11-21 修：此前**两页**都只设上限没居中，1440 宽下列表页右侧空 536px、详情页同病）。

间距五档：4 / 8 / 16 / 24 / 32px。卡片内部统一 `8px 16px`，区块之间 16px，主要分区之间 24px。

### 已知缺口
**产品代码只有一个响应式断点**（详情页 `≥1200px` 放宽量宽）。分段 tab、步骤带、列表行在 420 宽下已经靠 `flex-wrap` 折行而不是横向滚动（2026-11-21 实测三种宽度），但没有专门的窄屏布局设计——只是「不破」而不是「为窄屏设计过」。

## Elevation & Depth

**平面分层。** 系统不使用阴影表达层级。深度由两件事表达：

1. **底色差**：画布 `#f7f7f8` 与卡面 `#ffffff` 的差异足以让卡面浮起。
2. **1px 边框**：`#d7d7dd` 的实线边界，是系统里唯一的层级标记。

不用阴影的理由：这是一个信息密集的列表型界面，几十个卡片同时带阴影会造成视觉噪音，而且阴影在小尺寸密集排布时反而模糊边界。

## Shapes

两档圆角：`4px`（按钮、徽标、tab 顶部）与 `8px`（卡片、面板、提示框）。没有全圆角胶囊，没有零圆角。圆角的选择只取决于元素尺寸：小控件用小的，容器用大的。

## Components

### Buttons
- **Shape:** 4px 圆角，1px 实线边框
- **Primary:** 蓝色底 `#2f6feb`、白字，`8px 16px` 内边距。用于页面主操作（新建会话、创建并开始）。
- **Secondary:** 白底、深色字、灰色边框。用于次要操作（复制、翻页、重试）。
- **Hover / Active:** 主按钮底色深一档（`--color-accent-hover` → `--color-accent-active`）；次要按钮底色下沉到画布灰、边框加深；按下再深一档。2026-11-21 polish 补：此前全站零 hover 反馈，鼠标移上去界面毫无反应。
- **禁用:** 透明度 0.45 + `not-allowed`，次要按钮文字再退到 `--color-text-muted`——白底按钮在灰画布上只靠 0.45 透明度，扫一眼仍像能点（「上一页／下一页」并排几乎同貌，实测）。
- **Focus:** 无过渡、无位移。`:focus-visible` 的 2px 蓝色描边（`outline-offset: 2px`）。

### Inputs / Fields
- **Style:** **没有样式。** 产品代码里的 `input` / `textarea` / `select` 全部使用浏览器默认外观，仅靠外层 `.field` 的 flex 布局对齐。
- 已知缺口：这是系统里最明显的不一致——按钮有完整样式，输入框却是原生的。文件选择、下拉框的外观同样未处理。
- **Focus:** 依赖全局 `:focus-visible` 规则（2px 蓝色描边）。
- **禁用态:** 全局 `:disabled` 给 `opacity: 0.45` + `cursor: not-allowed`（2026-11-21 补：此前禁用按钮与可用态像素级相同，点了没反应像卡死）。

### Status Badges
- **Shape:** 4px 圆角，1px 边框，`2px 8px` 内边距，0.875rem 字号
- **Tone:** `ok`（绿底绿字绿框）/ `error`（红底红字红框）/ `neutral`（默认边框）
- **行为:** 始终带文字，不只靠颜色。状态名来自集中映射表（`status.ts`），不在组件里硬编码。
- 列表行内 `white-space: nowrap` + `flex: none`（2026-11-21 修：420 宽下曾被压成「已完／成」两行）。

### Tabs
- **Style:** 底部边框贯穿整行；选中项白底、上左右三边描边、600 字重，与底边框连成一体
- **Shape:** 顶部 4px 圆角，底部直角（贴着内容区）
- **Keyboard:** WAI-ARIA tabs 规范，roving tabindex，方向键 + Home/End 切换
- **窄屏:** `flex-wrap: wrap` + `white-space: nowrap` + `flex: 0 0 auto`——420 宽下折成两行、每格宽度不变（2026-11-21 实测；此前用横向滚动，没有任何滚动提示，右边那格等于不可发现）。
- **待办计数:** 分区名后面跟一个蓝色计数徽标（`data-count` 属性 + `::after content: attr()` 渲染）。数字走伪元素是为了不进入 tab 的文本内容；读屏从 `aria-label`（「审查与待办，1 项等待操作」）读同一件事。

### Navigation
- **Top bar:** 白底、底部 1px 边框、`16px 24px` 内边距。链接用强调蓝色。
- 只有两项：会话 / 诊断。没有侧栏，没有面包屑，没有二级导航。

### Error Notice
- **Style:** 红色系，`role="alert"`，16px 内边距，8px 圆角
- **行为:** 操作按钮由使用方通过插槽注入（保持组件无副作用）

### Signature Component: 步骤带
详情页顶部的流程指示器。把当前会话在主管线上的位置铺成一行方块，每个方块是一个阶段，带状态（已完成 / 当前 / 未到），下方一行「第 N / M 步：当前步骤 · 上一步：X」。

已知缺口：方块之间没有连接线，看不出方向与剩余量；状态色（绿框/蓝框）在密集时难以扫读。这是本系统最值得重新设计的组件——它是「知道自己在哪」这个核心诉求的承载者。

无障碍（2026-11-21 补）：当前步骤带 `aria-current="step"`；每格有 `sr-only` 完整播报（「第 3 步（共 7 步）素材循环，12/12，已完成」），可见标签标 `aria-hidden` 作为缩写；整条带子只有**一个** `role="status"`，位置与当前节点合成一句，不拆成两段抢播报。

## Do's and Don'ts

### Do:
- **Do** 所有颜色、间距、圆角走 CSS 变量，不写裸值（产品代码目前 100% 遵守，需保持）。
- **Do** 状态同时用文字与颜色表达，徽标永远带标签。
- **Do** 用 1px 边框与底色差分层次，不用阴影。
- **Do** 焦点态用 2px 描边 + 2px 偏移，覆盖所有交互元素。
- **Do** 在 `prefers-reduced-motion` 下关闭一切动效。
- **Do** 术语与 `CONTEXT.md` 保持一致（分段、素材、产物、交付、导演台）。

### Don't:
- **Don't** 引入第二个强调色。
- **Don't** 用阴影做卡片浮起效果。
- **Don't** 把状态表达成纯色块或纯图标——必须有文字。
- **Don't** 新增字号档位，除非现有四档放不下。
- **Don't** 在界面里编造数据、进度百分比或评价——服务端是唯一真源。
- **Don't** 用中间点连接元信息后不加层级（如「A · B · C」平铺），元信息要能被扫读区分。
