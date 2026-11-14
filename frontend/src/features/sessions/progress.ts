import type { ActionItemOut, ArtifactInfo, SegmentOut, SessionDetail } from '@/api/client'

import { nodeLabel } from './status'

// 概览分区顶部的步骤带（#272）：把「这条路线一共要经过哪些步骤、现在到哪一步」
// 从快照与流上进度算成一份**纯数据**。没有 DOM、没有定时器、没有全局状态——
// 输入全在参数里，所以短视频/长片/15 段/失败态都能直接用单测钉住。
//
// 数据来源只有两处（与 #271 的实时接入约定一致）：
//   - 快照：状态、人工待办、产物、段列表——业务事实；
//   - 流：`job.progress` 的 stage/node/时间——纯展示，不写快照。
// 步骤状态是这两处的**推导结果**，不是新的业务状态。

export type StepState = 'done' | 'current' | 'pending'

export interface ProgressStep {
  key: string
  label: string
  state: StepState
  /** 附加说明（今天只有分段循环的 `x/y`）；空串表示不显示。 */
  detail: string
}

export interface ProgressView {
  steps: ProgressStep[]
  /** 当前节点的中文名（未知回退原名）；没有进度信号时为 null。 */
  nodeLabel: string | null
  /** 最近一条 `done` 事件的节点中文名——「上一步」。 */
  previousLabel: string | null
  /** 当前节点 started 事件到 `now` 的毫秒数；不在跑或没有起点时为 null。 */
  elapsedMs: number | null
}

/** 流上 `job.progress` 的展示切片（由 useSessionStream 汇总，不进快照）。 */
export interface JobProgressView {
  stage: string | null
  node: string | null
  status: 'started' | 'done' | null
  /** 当前节点 started 事件的时刻（epoch ms）。 */
  startedAt: number | null
  /** 最近一条 done 事件的节点名。 */
  previousNode: string | null
}

export interface ProgressInput {
  /** 路线：`base` / `ref` / `director`。本票只定义 base，其余返回空步骤（#273）。 */
  route: string
  variant: string
  duration: number
  status: SessionDetail['status']
  actionItems: ActionItemOut[]
  artifacts: ArtifactInfo[]
  segments: SegmentOut[]
  progress: JobProgressView | null
  /** 计算已耗时的「现在」（epoch ms）；由调用方给，保持函数纯。 */
  now: number
}

// 长片判据与后端 `step_machine.LONG_FORM_MIN_SECONDS` 同值（>10 秒，D6 §3）。
// 步骤带只判「要不要列分段三步」，真正的路线判据仍由后端给。
export const LONG_FORM_SECONDS = 10
const FRAME_CHAIN_VARIANT = 'FL2VA'

interface StepDefinition {
  key: string
  label: string
}

// 每条路线各一份步骤定义，集中在这一个模块：新增路线时只加一条链，状态推导
// 仍走同一套（比较 key 的顺序），不会出现第二份步骤表。
const BASE_SHORT_STEPS: StepDefinition[] = [
  { key: 'stage1', label: '阶段 1' },
  { key: 'review', label: '审查' },
  { key: 'frame', label: '帧提交' },
  { key: 'stage2', label: '阶段 2' },
  { key: 'complete', label: '完成' },
]

const BASE_LONG_STEPS: StepDefinition[] = [
  { key: 'stage1', label: '阶段 1' },
  { key: 'review', label: '审查' },
  { key: 'frame', label: '帧提交' },
  { key: 'stage2', label: '阶段 2' },
  { key: 'segment_plan', label: '分段规划' },
  { key: 'segment_loop', label: '分段循环' },
  { key: 'assemble', label: '拼接' },
  { key: 'complete', label: '完成' },
]

const STAGE_RANK: Record<string, number> = {
  stage1: 0,
  asset_loop: 1,
  stage2: 2,
  packing: 3,
  segmenting: 4,
}

function rankOf(stage: string | null): number {
  return stage === null ? -1 : (STAGE_RANK[stage] ?? -1)
}

function isPending(items: ActionItemOut[], kind: string): boolean {
  return items.some((item) => item.kind === kind && item.status === 'pending')
}

function hasItem(items: ActionItemOut[], kind: string): boolean {
  return items.some((item) => item.kind === kind)
}

function hasResolved(items: ActionItemOut[], kind: string): boolean {
  return items.some((item) => item.kind === kind && item.status === 'resolved')
}

/** 当前步骤的 key——由流上 stage/node 优先，其次由快照事实推。 */
function currentStepKey(input: {
  hasFrameStep: boolean
  longForm: boolean
  status: SessionDetail['status']
  actionItems: ActionItemOut[]
  artifacts: ArtifactInfo[]
  segments: SegmentOut[]
  progress: JobProgressView | null
}): string | null {
  const { hasFrameStep, longForm, status, actionItems, artifacts, segments, progress } = input
  const stageRank = rankOf(progress?.stage ?? null)
  const node = progress?.node ?? ''
  const completed = status === 'completed'

  if (completed) {
    return null // 全部 done，没有当前步骤
  }

  // 流上进度只有「正在跑」时才算当前步骤：等待人工/失败后服务端补发的最后一条
  // 往往是 done（节点已经跑完），照它走会把步骤带钉在上一跳，与快照事实打架。
  const progressActive =
    progress !== null && (status === 'running' || progress.status === 'started')

  if (progressActive && stageRank >= 0) {
    if (progress.stage === 'stage1') {
      return 'stage1'
    }
    if (progress.stage === 'stage2') {
      return 'stage2'
    }
    if (progress.stage === 'segmenting') {
      if (node === 'segment_plan') {
        return 'segment_plan'
      }
      if (node === 'segment_assemble') {
        return 'assemble'
      }
      return 'segment_loop'
    }
  }

  const pendingReview = isPending(actionItems, 'stage1_review')
  const pendingFrame = isPending(actionItems, 'frame_submission')
  const pendingSegmentVideo = isPending(actionItems, 'segment_video')
  const scriptArtifact = artifacts.some((artifact) => artifact.kind === 'script')
  const videoArtifact = artifacts.some((artifact) => artifact.kind === 'video_prompt')
  const stage1Done =
    stageRank >= 2 ||
    hasItem(actionItems, 'stage1_review') ||
    scriptArtifact ||
    pendingFrame ||
    segments.length > 0
  const reviewDone =
    stage1Done &&
    !pendingReview &&
    (stageRank >= 2 ||
      hasResolved(actionItems, 'stage1_review') ||
      pendingFrame ||
      pendingSegmentVideo ||
      segments.length > 0 ||
      videoArtifact)
  const stage2Done = stageRank >= 4 || segments.length > 0 || videoArtifact
  const passed = segments.filter((segment) => segment.status === 'passed').length
  const total = segments.length

  if (pendingReview) {
    return 'review'
  }
  if (hasFrameStep && pendingFrame) {
    return 'frame'
  }
  if (pendingSegmentVideo) {
    return 'segment_loop'
  }
  if (total > 0) {
    return passed === total ? (longForm ? 'assemble' : 'stage2') : 'segment_loop'
  }
  if (!reviewDone) {
    return 'stage1'
  }
  if (longForm && stage2Done) {
    return 'segment_plan'
  }
  // running 且审查已过、段表还没落盘：多半在阶段 2（规划要等阶段 2 收尾）。
  return 'stage2'
}

/** 把「当前步骤」翻译成有序步骤上的三种状态；当前步骤之后（含）按顺序铺开。 */
function applyStates(
  steps: StepDefinition[],
  currentKey: string | null,
  detailOf: (key: string) => string,
): ProgressStep[] {
  const currentIndex =
    currentKey === null ? steps.length : steps.findIndex((step) => step.key === currentKey)
  return steps.map((step, index) => ({
    key: step.key,
    label: step.label,
    state:
      currentIndex === steps.length || index < currentIndex
        ? 'done'
        : index === currentIndex
          ? 'current'
          : 'pending',
    detail: detailOf(step.key),
  }))
}

export function buildProgress(input: ProgressInput): ProgressView {
  // 本票只接 base；ref / 导演台的步骤定义由 #273 补。返回空步骤＝组件不渲染，
  // 比拿 base 的步骤冒充其他路线更诚实。
  if (input.route !== 'base') {
    return { steps: [], nodeLabel: null, previousLabel: null, elapsedMs: null }
  }

  const longForm = input.duration > LONG_FORM_SECONDS
  const hasFrameStep = input.variant.toUpperCase() === FRAME_CHAIN_VARIANT
  const definitions = (longForm ? BASE_LONG_STEPS : BASE_SHORT_STEPS).filter(
    (step) => step.key !== 'frame' || hasFrameStep,
  )

  const currentKey = currentStepKey({
    hasFrameStep,
    longForm,
    status: input.status,
    actionItems: input.actionItems,
    artifacts: input.artifacts,
    segments: input.segments,
    progress: input.progress,
  })

  const passed = input.segments.filter((segment) => segment.status === 'passed').length
  const total = input.segments.length
  const detailOf = (key: string): string =>
    key === 'segment_loop' && total > 0 ? `${passed}/${total}` : ''

  const steps = applyStates(definitions, currentKey, detailOf)

  const progress = input.progress
  const running = input.status === 'running'
  const elapsedMs =
    running && progress?.status === 'started' && progress.startedAt !== null
      ? Math.max(0, input.now - progress.startedAt)
      : null

  // 完成态没有「当前步骤」：刷新时补发的最后一条 done 进度不能变成一句
  // 「当前：终检」挂在一个已经交付的会话上。
  const completed = input.status === 'completed'
  return {
    steps,
    nodeLabel: !completed && progress?.node ? nodeLabel(progress.node) : null,
    previousLabel: !completed && progress?.previousNode ? nodeLabel(progress.previousNode) : null,
    elapsedMs,
  }
}

/** 已耗时的中文写法：秒级直接给秒，分钟级补两位秒。 */
export function formatElapsed(ms: number): string {
  const totalSeconds = Math.floor(Math.max(0, ms) / 1000)
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  if (minutes === 0) {
    return `${seconds} 秒`
  }
  return `${minutes} 分 ${String(seconds).padStart(2, '0')} 秒`
}
