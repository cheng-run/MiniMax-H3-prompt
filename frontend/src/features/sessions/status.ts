import type { SessionSummary } from '@/api/client'

// 会话状态的七态展示（spec §4）：状态值来自契约，徽标文案与色调在这里集中，
// 列表与详情共用，避免两处各写一张表悄悄漂移。
export type SessionStatus = SessionSummary['status']
export type StatusTone = 'ok' | 'error' | 'neutral'

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
  awaiting_human: 'neutral',
  completed: 'ok',
  failed: 'error',
  cancelled: 'neutral',
  interrupted: 'error',
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
