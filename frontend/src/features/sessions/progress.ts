import type { ActionItemOut, ArtifactInfo, RefOut, SegmentOut, SessionDetail } from '@/api/client'

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
  /** 路线：`base` / `ref` / `director`（与 `SessionDetail.mode` 同源）。 */
  route: string
  variant: string
  duration: number
  /** 导演台的生成模式（`brief.director_mode`）：`t2v` / `i2v` / `fl2v` / `r2v`。 */
  directorMode: string
  status: SessionDetail['status']
  actionItems: ActionItemOut[]
  artifacts: ArtifactInfo[]
  segments: SegmentOut[]
  /** 参考素材台账摘要（`brief.refs`）：素材循环的 `n` 从这里数 Picture。 */
  refs: RefOut[]
  progress: JobProgressView | null
  /** 计算已耗时的「现在」（epoch ms）；由调用方给，保持函数纯。 */
  now: number
}

// 长片判据与后端 `step_machine.LONG_FORM_MIN_SECONDS` 同值（>10 秒，D6 §3）。
// 步骤带只判「要不要列分段三步」，真正的路线判据仍由后端给。
export const LONG_FORM_SECONDS = 10
// 帧链变体集合与后端 `brief_parser.FRAME_VARIANTS` 同源（契约允许这三档）。
// base 的创建界面只开 T2VA/FL2VA，但 I2VA/L2VA 是导演台 i2v/fl2v 的帧变体，
// 会话读回时会出现在 `brief.variant` 上——判据不能只认 FL2VA。
export const FRAME_VARIANTS = ['I2VA', 'FL2VA', 'L2VA'] as const
const FRAME_VARIANT_SET: ReadonlySet<string> = new Set(FRAME_VARIANTS)
// 导演台里需要开场首帧（i2v 首帧 / fl2v 首尾帧）的模式；t2v/r2v 没有这一格。
const DIRECTOR_START_FRAME_MODES: ReadonlySet<string> = new Set(['i2v', 'fl2v'])
// 素材循环只逐件处理 Picture（视频/音频没有 `assets/picture-NN.md`），
// 与后端 `kinds.KIND_PICTURE` 同值。
const KIND_PICTURE = 'picture'

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

// ref：审查通过后先逐件定稿资产图（素材循环），再问一次要不要补交视频/音频
// （补交档，#284），然后进阶段 2；分段循环一律存在（单段也走同一套待办与拼接），
// 拼接是段循环的内部收尾、不单独占一格。
const REF_STEPS: StepDefinition[] = [
  { key: 'stage1', label: '阶段 1' },
  { key: 'review', label: '审查' },
  { key: 'asset_loop', label: '素材循环' },
  { key: 'asset_extra', label: '素材补交' },
  { key: 'stage2', label: '阶段 2' },
  { key: 'segment_loop', label: '分段循环' },
  { key: 'complete', label: '完成' },
]

// 导演台：审查后可能先收开场首帧，阶段 2 之后是打包，段循环走完停在交付闸门。
const DIRECTOR_STEPS: StepDefinition[] = [
  { key: 'stage1', label: '阶段 1' },
  { key: 'review', label: '审查' },
  { key: 'start_frame', label: '开场帧' },
  { key: 'stage2', label: '阶段 2' },
  { key: 'pack', label: '打包' },
  { key: 'segment_loop', label: '段循环' },
  { key: 'delivery', label: '交付' },
  { key: 'complete', label: '完成' },
]

const STAGE_RANK: Record<string, number> = {
  stage1: 0,
  asset_loop: 1,
  // 补交档夹在两者之间（#284）；用小数是为了不动既有的整数比较（它们问的是
  // 「阶段 2 开始了吗 / 阶段 1 结束了吗」，两者都不该被这一档改变）。
  asset_extra: 1.5,
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

/** 从快照与流上进度抽出三条路线共用的判定事实（不猜业务状态，只做分类）。 */
interface RouteFacts {
  completed: boolean
  /** 流上进度只有「正在跑」时才算当前步骤：等待人工/失败后服务端补发的最后一条
   *  往往是 done（节点已经跑完），照它走会把步骤带钉在上一跳，与快照事实打架。 */
  progressActive: boolean
  stage: string | null
  node: string
  stageRank: number
  pendingReview: boolean
  pendingFrame: boolean
  pendingSegmentVideo: boolean
  pendingAsset: boolean
  pendingExtra: boolean
  hasExtraItem: boolean
  pendingStart: boolean
  pendingSegmentImage: boolean
  pendingDelivery: boolean
  hasStage1Review: boolean
  hasResolvedReview: boolean
  hasResolvedStart: boolean
  scriptArtifact: boolean
  videoPromptArtifact: boolean
  directorPackArtifact: boolean
  totalSegments: number
  passedSegments: number
}

function routeFacts(input: ProgressInput): RouteFacts {
  const stage = input.progress?.stage ?? null
  const node = input.progress?.node ?? ''
  const completed = input.status === 'completed'
  const progressActive =
    input.progress !== null && (input.status === 'running' || input.progress.status === 'started')
  return {
    completed,
    progressActive,
    stage,
    node,
    stageRank: rankOf(stage),
    pendingReview: isPending(input.actionItems, 'stage1_review'),
    pendingFrame: isPending(input.actionItems, 'frame_submission'),
    pendingSegmentVideo: isPending(input.actionItems, 'segment_video'),
    pendingAsset: isPending(input.actionItems, 'asset_image'),
    pendingExtra: isPending(input.actionItems, 'asset_extra'),
    hasExtraItem: hasItem(input.actionItems, 'asset_extra'),
    pendingStart: isPending(input.actionItems, 'director_start_image'),
    pendingSegmentImage: isPending(input.actionItems, 'director_segment_image'),
    pendingDelivery: isPending(input.actionItems, 'director_delivery'),
    hasStage1Review: hasItem(input.actionItems, 'stage1_review'),
    hasResolvedReview: hasResolved(input.actionItems, 'stage1_review'),
    hasResolvedStart: hasResolved(input.actionItems, 'director_start_image'),
    scriptArtifact: input.artifacts.some((artifact) => artifact.kind === 'script'),
    videoPromptArtifact: input.artifacts.some((artifact) => artifact.kind === 'video_prompt'),
    directorPackArtifact: input.artifacts.some((artifact) => artifact.kind === 'director_pack'),
    totalSegments: input.segments.length,
    passedSegments: input.segments.filter((segment) => segment.status === 'passed').length,
  }
}

/** base 的当前步骤：由流上 stage/node 优先，其次由快照事实推。 */
function baseCurrentKey(input: ProgressInput, facts: RouteFacts): string | null {
  if (facts.completed) {
    return null // 全部 done，没有当前步骤
  }
  const { progressActive, stage, node, stageRank } = facts
  const longForm = input.duration > LONG_FORM_SECONDS
  const hasFrameStep = FRAME_VARIANT_SET.has(input.variant.toUpperCase())
  const { totalSegments: total, passedSegments: passed } = facts

  if (progressActive && stageRank >= 0) {
    if (stage === 'stage1') {
      return 'stage1'
    }
    if (stage === 'stage2') {
      return 'stage2'
    }
    if (stage === 'segmenting') {
      if (node === 'segment_plan') {
        return 'segment_plan'
      }
      if (node === 'segment_assemble') {
        return 'assemble'
      }
      return 'segment_loop'
    }
  }

  const stage1Done =
    stageRank >= 2 ||
    facts.hasStage1Review ||
    facts.scriptArtifact ||
    facts.pendingFrame ||
    total > 0
  const reviewDone =
    stage1Done &&
    !facts.pendingReview &&
    (stageRank >= 2 ||
      facts.hasResolvedReview ||
      facts.pendingFrame ||
      facts.pendingSegmentVideo ||
      total > 0 ||
      facts.videoPromptArtifact)
  const stage2Done = stageRank >= 4 || total > 0 || facts.videoPromptArtifact

  if (facts.pendingReview) {
    return 'review'
  }
  if (hasFrameStep && facts.pendingFrame) {
    return 'frame'
  }
  if (facts.pendingSegmentVideo) {
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

/** ref 的当前步骤：阶段 1 → 审查 → 素材循环 → 阶段 2 → 分段循环 → 完成。 */
function refCurrentKey(input: ProgressInput, facts: RouteFacts): string | null {
  if (facts.completed) {
    return null
  }
  const { progressActive, stage, stageRank } = facts
  if (progressActive) {
    if (stage === 'stage1') {
      return 'stage1'
    }
    if (stage === 'asset_loop') {
      return 'asset_loop'
    }
    if (stage === 'asset_extra') {
      return 'asset_extra'
    }
    if (stage === 'stage2') {
      return 'stage2'
    }
    if (stage === 'segmenting') {
      return 'segment_loop'
    }
  }
  if (facts.pendingReview) {
    return 'review'
  }
  if (facts.pendingAsset) {
    return 'asset_loop'
  }
  if (facts.pendingExtra) {
    return 'asset_extra'
  }
  if (facts.pendingSegmentVideo || facts.totalSegments > 0) {
    return 'segment_loop'
  }
  // 阶段 2 已经开始（产物或流上最后一条阶段标签都算）。
  if (facts.videoPromptArtifact || stageRank >= 2) {
    return 'stage2'
  }
  // 补交待办出现过且已不在等（不 pending）＝循环走完、补交也落定了：下一步就是
  // 阶段 2，即便阶段 2 的产物还没落盘。没有这一条会退回「素材循环」，看起来像
  // 循环又活过来了。
  if (facts.hasExtraItem) {
    return 'stage2'
  }
  // 素材循环动过（有件待办或已产出资产图词）同样证明阶段 1 与审查都已过去。
  const assetLoopStarted =
    input.actionItems.some((item) => item.kind === 'asset_image') ||
    input.artifacts.some((artifact) => artifact.kind === 'asset_prompt')
  const stage1Done =
    stageRank >= 1 || facts.hasStage1Review || facts.scriptArtifact || assetLoopStarted
  if (!stage1Done) {
    return 'stage1'
  }
  return 'asset_loop'
}

/** 导演台的当前步骤：阶段 1 → 审查 → 开场帧 → 阶段 2 → 打包 → 段循环 → 交付 → 完成。 */
function directorCurrentKey(facts: RouteFacts, hasStartFrame: boolean): string | null {
  if (facts.completed) {
    return null
  }
  const { progressActive, stage, node, stageRank } = facts
  if (progressActive) {
    if (stage === 'stage1') {
      return 'stage1'
    }
    if (stage === 'stage2') {
      return 'stage2'
    }
    if (stage === 'packing') {
      return 'pack'
    }
    if (stage === 'segmenting') {
      // 交付归档 job 也记 `segmenting` 阶段标签，用节点名分开。
      return node === 'director_delivery' ? 'delivery' : 'segment_loop'
    }
  }
  if (facts.pendingReview) {
    return 'review'
  }
  if (hasStartFrame && facts.pendingStart) {
    return 'start_frame'
  }
  if (facts.pendingSegmentImage || facts.pendingSegmentVideo) {
    return 'segment_loop'
  }
  if (facts.pendingDelivery) {
    return 'delivery'
  }
  if (facts.totalSegments > 0) {
    // 全部段通过后只剩交付闸门（归档 job 跑的时候流上进度已经把它标成 delivery）。
    return facts.passedSegments === facts.totalSegments ? 'delivery' : 'segment_loop'
  }
  if (facts.directorPackArtifact) {
    // 打包已落盘、段表还没进快照：段循环马上开始。
    return 'segment_loop'
  }
  const stage1Done = stageRank >= 1 || facts.hasStage1Review || facts.scriptArtifact
  if (!stage1Done) {
    return 'stage1'
  }
  if (!facts.hasResolvedReview) {
    return 'review'
  }
  if (hasStartFrame && !facts.hasResolvedStart) {
    return 'start_frame'
  }
  return 'stage2'
}

/** ref 的素材循环进度：n 只数 Picture（素材循环只逐件处理 Picture）。 */
function refAssetProgress(
  input: ProgressInput,
  facts: RouteFacts,
): { passed: number; total: number } {
  const total = input.refs.filter((ref) => ref.kind === KIND_PICTURE).length
  const items = input.actionItems.filter((item) => item.kind === 'asset_image')
  const pending = items.find((item) => item.status === 'pending')
  const lastResolved = items.reduce(
    (max, item) => (item.status === 'resolved' ? Math.max(max, item.asset_index ?? 0) : max),
    0,
  )
  let passed: number
  if (pending) {
    // 等待的这件还没交：已过件数就是它前面那几件。
    passed = (pending.asset_index ?? 1) - 1
  } else if (facts.videoPromptArtifact || facts.totalSegments > 0) {
    // 已进阶段 2／段循环：素材循环整个收尾了。
    passed = total
  } else if (facts.progressActive && facts.node === 'asset_proofread') {
    // 正在校对刚交的这一件：它已 resolve 但还没定稿，别算作已通过。
    passed = Math.max(0, lastResolved - 1)
  } else {
    passed = lastResolved
  }
  return { passed: Math.min(passed, total), total }
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

/** 步骤的「当前节点 + 已耗时 + 上一步」：三条路线共用同一份出口。 */
function finishView(input: ProgressInput, steps: ProgressStep[]): ProgressView {
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

/** 段循环的 `x/y`（三条路线同一口径；没有段表时不显示）。 */
function segmentDetail(facts: RouteFacts): string {
  return facts.totalSegments > 0 ? `${facts.passedSegments}/${facts.totalSegments}` : ''
}

export function buildProgress(input: ProgressInput): ProgressView {
  const facts = routeFacts(input)

  if (input.route === 'ref') {
    const currentKey = refCurrentKey(input, facts)
    const asset = refAssetProgress(input, facts)
    const detailOf = (key: string): string => {
      if (key === 'asset_loop') {
        return asset.total > 0 ? `${asset.passed}/${asset.total}` : ''
      }
      return key === 'segment_loop' ? segmentDetail(facts) : ''
    }
    return finishView(input, applyStates(REF_STEPS, currentKey, detailOf))
  }

  if (input.route === 'director') {
    // 只列本路线实际步骤：开场首帧只在 i2v/fl2v（或快照里还留着这条待办）时出现。
    const hasStartFrame =
      DIRECTOR_START_FRAME_MODES.has(input.directorMode.toLowerCase()) ||
      hasItem(input.actionItems, 'director_start_image')
    const definitions = DIRECTOR_STEPS.filter((step) => step.key !== 'start_frame' || hasStartFrame)
    const currentKey = directorCurrentKey(facts, hasStartFrame)
    const detailOf = (key: string): string => (key === 'segment_loop' ? segmentDetail(facts) : '')
    return finishView(input, applyStates(definitions, currentKey, detailOf))
  }

  const longForm = input.duration > LONG_FORM_SECONDS
  const hasFrameStep = FRAME_VARIANT_SET.has(input.variant.toUpperCase())
  const definitions = (longForm ? BASE_LONG_STEPS : BASE_SHORT_STEPS).filter(
    (step) => step.key !== 'frame' || hasFrameStep,
  )
  const currentKey = baseCurrentKey(input, facts)
  const detailOf = (key: string): string => (key === 'segment_loop' ? segmentDetail(facts) : '')
  return finishView(input, applyStates(definitions, currentKey, detailOf))
}

// 待办 kind → 「等待你操作」那一行的中文说明（#276）。它和步骤带同层常驻，判据
// 与步骤带用同一份 pending 事实（`status === 'pending'` + kind），不新增业务状态，
// 也不另立一张待办表：kind 知识本来就集中在本模块，翻译留在这里免得漂成两份。
// 需要段号/件号的 kind 从待办自带的索引取；素材的 Picture 语义名从 refs 台账取。
const PENDING_HINTS: Record<string, (item: ActionItemOut, refs: RefOut[]) => string> = {
  stage1_review: () => '审查阶段 1 结果',
  frame_submission: () => '提交首帧/尾帧素材',
  segment_video: (item) =>
    item.segment_index ? `提交第 ${item.segment_index} 段视频` : '提交段视频',
  asset_image: (item, refs) => {
    const number = item.asset_index ?? 0
    if (!number) {
      return '交回素材图'
    }
    const name = refs.find((ref) => ref.kind === KIND_PICTURE && ref.number === number)?.name
    return name ? `交回第 ${number} 件素材图 · ${name}` : `交回第 ${number} 件素材图`
  },
  // 补交口（#284）：循环之后的阻塞决定点，两句答案都在卡片上。
  asset_extra: () => '确认要不要补交素材视频/音频',
  director_start_image: () => '提交开场首帧',
  director_segment_image: (item) =>
    item.segment_index ? `提交第 ${item.segment_index} 段首帧图` : '提交段首帧图',
  director_delivery: () => '交付成片与最终包',
}

// 未知 kind 的兜底文案：后端先加了一种人工待办、前端这版还不认识时，「等待你操作」
// 整行不能消失——那会让用户以为无事可做，而会话其实正卡在一个没有入口的待办上。
const UNKNOWN_PENDING_HINT = '有新的待办需要处理'

/**
 * 当前待办的中文说明；没有任何待处理的人工待办时为 null。
 *
 * 优先精确文案：任一已知 kind 处于 pending 就按它翻译；全是未知 kind 时回退一句
 * 通用说明，**不返回 null**——未知待办同样是「等你操作」，整行消失等于静默丢提示。
 */
export function pendingActionHint(actionItems: ActionItemOut[], refs: RefOut[]): string | null {
  const known = actionItems.find(
    (entry) => entry.status === 'pending' && entry.kind in PENDING_HINTS,
  )
  if (known) {
    return PENDING_HINTS[known.kind]!(known, refs)
  }
  const unknown = actionItems.find((entry) => entry.status === 'pending')
  return unknown ? UNKNOWN_PENDING_HINT : null
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
