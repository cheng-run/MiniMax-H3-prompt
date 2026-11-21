import type { SessionSummary } from '@/api/client'

// 会话状态的七态展示（spec §4）：状态值来自契约，徽标文案与色调在这里集中，
// 列表与详情共用，避免两处各写一张表悄悄漂移。
export type SessionStatus = SessionSummary['status']
// `attention` 是「要你做事」这一档（2026-11-21 critique：等待人工此前用 neutral，
// 与「已创建」「已取消」像素级同貌，20 行列表里那一行扫视时读起来像「没状态」）。
// 用 accent 蓝而不是新颜色——The One Accent Rule 只允许一个强调色。
export type StatusTone = 'ok' | 'error' | 'neutral' | 'attention'

const LABELS: Record<SessionStatus, string> = {
  created: '已创建',
  running: '运行中',
  awaiting_human: '等待人工',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
  interrupted: '已中断',
}

const TONES: Record<SessionStatus, StatusTone> = {
  created: 'neutral',
  running: 'neutral',
  // 等待人工是本产品的常态，也是唯一「必须由你动手」的状态：它要在列表里跳出来。
  awaiting_human: 'attention',
  completed: 'ok',
  failed: 'error',
  cancelled: 'neutral',
  // 中断与失败**不是**同一档（2026-11-21 复审：曾把 interrupted 映射成 error，
  // 与 failed 像素级同貌，也与 rail-contract 冲突——那里 interrupted 是中灰斜划，
  // failed 才是红）。中断是「被打断」，不是「出错了」，用 neutral。
  interrupted: 'neutral',
}

/** 全部七态，顺序固定；用例据此断言没有漏掉徽标分支。 */
export const SESSION_STATUSES: SessionStatus[] = [
  'created',
  'running',
  'awaiting_human',
  'completed',
  'failed',
  'cancelled',
  'interrupted',
]

export function statusLabel(status: SessionStatus): string {
  return LABELS[status]
}

export function statusTone(status: SessionStatus): StatusTone {
  return TONES[status]
}

// 步骤标签（`job.progress` 的 stage，D7 §5.2 增 `asset_loop`）：只给「正在执行」
// 那一行做说明，**不是**会话状态——会话仍是七态，状态徽章不因它长分支。
// 取值与后端 `step_machine.STEPS` 同源（契约里的进度事件），前端只负责翻译。
const STEP_LABELS: Record<string, string> = {
  stage1: '阶段 1',
  asset_loop: '素材循环',
  asset_extra: '素材补交',
  stage2: '阶段 2',
  packing: '打包',
  segmenting: '分段循环',
}

/** 进度阶段 → 给人看的步骤名；未知/缺席返回 null（不猜）。 */
export function stepLabel(step: string | null | undefined): string | null {
  if (!step) {
    return null
  }
  return STEP_LABELS[step] ?? null
}

// 内部节点名 → 中文标签的**唯一映射**（#272）：节点名来自 `job.progress` 的
// `node`，取值与后端 `graph.pipeline._STAGE1_CHAIN` / `_STAGE2_CHAIN` 及各
// `*_NODE` 常量同源。未知节点一律回退原名（不隐藏），所以这张表只补「认得出来的」。
const NODE_LABELS: Record<string, string> = {
  producer: '制片',
  director: '导演',
  creative_rt: '创意评审',
  screenwriter: '编剧',
  parallel_designers: '人物与场景设计',
  art_director: '美术指导',
  parallel_image_prompts: '资产图提示词',
  storyboard: '分镜',
  parallel_decisions: '镜头与一致性决策',
  parallel_visual: '视听设计',
  fl2va_frame_prompts: '首尾帧提示词',
  parallel_sound: '声音设计',
  prompt_engineer: '视频正文',
  finalize: '终检',
  asset_write: '素材出词',
  asset_proofread: '素材校对',
  asset_reword: '素材改词',
  asset_restore: '恢复原文',
  segment_plan: '分段规划',
  segment_assemble: '拼接',
  director_plan: '段规划',
  director_segment_write: '段写词',
  director_pack: '打包',
  director_delivery: '交付归档',
}

// 带段号的节点名（`segment_write-01`、`director_tailframe-03`）：段号进标签，
// 让人看得出「卡在第几段」。模式与后端拼节点名的那几处逐字对应。
const SUFFIXED_NODE_LABELS: { pattern: RegExp; label: (number: number) => string }[] = [
  { pattern: /^segment_write-(\d+)$/, label: (number) => `第 ${number} 段写词` },
  { pattern: /^segment_validate-(\d+)$/, label: (number) => `第 ${number} 段校验` },
  { pattern: /^director_firstframe-(\d+)$/, label: (number) => `第 ${number} 段首帧词` },
  { pattern: /^director_tailframe-(\d+)$/, label: (number) => `第 ${number} 段尾帧词` },
  { pattern: /^director_finalize-(\d+)$/, label: (number) => `第 ${number} 段收尾` },
]

/** 进度节点 → 给人看的名字；未知节点回退原名（不隐藏，否则界面像卡死了）。 */
export function nodeLabel(node: string): string {
  const exact = NODE_LABELS[node]
  if (exact) {
    return exact
  }
  for (const { pattern, label } of SUFFIXED_NODE_LABELS) {
    const match = pattern.exec(node)
    if (match) {
      return label(Number(match[1]))
    }
  }
  return node
}
