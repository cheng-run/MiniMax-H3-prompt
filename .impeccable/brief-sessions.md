# Surface: 会话工作台（列表页 + 详情页 + 诊断）

Mode: Operate（v1 前端改造后重做视觉世界，2026-11-21）

## 范围与访客模式

单人本地工具。访客只有一位：项目作者本人，在 Windows 桌面上一边跑 ComfyUI、一边用浏览器盯会话。
列表页要回答「哪些会话要动手」，详情页要回答「这一步该做什么」。诊断页是低频附属面。

本次重做**替换**此前 2026-11-21 固化的「无形的仪表盘」世界。产品事实、内容、功能、无障碍基线、
E2E 允许修改的裁定、EXE 打包约束一律继承 PRODUCT.md，不改。

## Direction contract

THESIS: 状态不是颜色，是线的形态。整个界面只用一种字号、一种无衬线体，靠字距、墨色深度与
线型（实/虚/半高/划过/加倍）建立层级与状态；拒绝「彩色徽标 + 白卡 + 圆角」那套同类工具的默认排布。

OWN-WORLD: 炭灰连续底（#141618 系）、骨白铭牌（#d8d3c6）、钠黄（#e0a93b）唯一亮色只给最要紧的一条；
左侧一条纵向导轨承载全部会话，其余是摊开的板面；1px 发光线取代边框与阴影；零圆角（或 2px）。

STORY: 进页面第一眼看导轨——哪一根线是双的、哪一根是虚的，就知道几条要动手、几条在跑；
点开一根，它在右侧绽成整块板面；所有操作仍是标准按钮与表单，不需要学。

FIRST VIEWPORT: 顶栏极窄（产品名 + 会话/诊断 + 新建），下方一条横向波长轴式的会话计数；
主体左侧 180px 导轨，八根刻度线，双线+钠黄的就是「等待人工」；右侧板面显示选中会话的完整行与操作。

FORM: 选择「谱线导轨」，用户从决策页选中它（challenger-emission 赢了掷骰分配给我的接触印相）。
seed: none (pinned) —— 用户以「Build this」明确指定该卡，草图 `.impeccable/mocks/decision/challenger-emission.html` 即构图契约。

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance
