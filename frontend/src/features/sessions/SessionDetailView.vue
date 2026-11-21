<script setup lang="ts">
import { ArrowLeft } from '@lucide/vue'
import { useQuery, useQueryClient } from '@tanstack/vue-query'
import { computed, inject, onBeforeUnmount, ref, watch } from 'vue'
import { RouterLink, useRoute } from 'vue-router'

import { ApiError, cancelSessionJob, getSession, startSessionJob } from '@/api/client'
import Button from '@/components/ui/Button.vue'
import ErrorNotice from '@/components/ui/ErrorNotice.vue'
import LoadingState from '@/components/ui/LoadingState.vue'
import StatusBadge from '@/components/ui/StatusBadge.vue'
import { useUiStore, type WorkbenchTab } from '@/stores/ui'

import AssetsTab from './AssetsTab.vue'
import ArtifactsTab from './ArtifactsTab.vue'
import AssetExtraCard from './AssetExtraCard.vue'
import AssetImageCard from './AssetImageCard.vue'
import DeliveryTab from './DeliveryTab.vue'
import DirectorSegmentImageCard from './DirectorSegmentImageCard.vue'
import DirectorStartImageCard from './DirectorStartImageCard.vue'
import FrameSubmissionCard from './FrameSubmissionCard.vue'
import { newIdempotencyKey } from './keys'
import {
  buildProgress,
  pendingAction,
  pendingCountsByTarget,
  type JobProgressView,
  type ProgressView,
} from './progress'
import ProgressBand from './ProgressBand.vue'
import { isRefSettled } from './refCapacity'
import ReviewTab from './ReviewTab.vue'
import SegmentsTab from './SegmentsTab.vue'
import { nodeLabel, statusLabel, statusTone, stepLabel } from './status'
import { SESSION_STREAM_FETCH, useSessionStream } from './useSessionStream'

// 工作台单页（D4 spec §4）：四分区是页内 tab、不进 URL，刷新回概览。
// 壳与分区切换、实时失效订阅与运行控制（#231）；审查与修订（#232）；
// 素材与帧提交（#233）；产物与交付（#234）。
//
// 会话标识本身含斜杠（`<slug>/GEN001`），路由参数用 `:id(.*)` 承接；这里把它原样
// 交给生成客户端，URL 编码由 openapi-fetch 负责。
const route = useRoute()
const ui = useUiStore()
const queryClient = useQueryClient()

const sessionId = computed(() => {
  const id = route.params.id
  return Array.isArray(id) ? (id[0] ?? '') : (id ?? '')
})

// 快照是唯一数据源（spec §6.1）：刷新即重拉，不做事件投影。
const { data, isPending, isError, error } = useQuery({
  queryKey: computed(() => ['session', sessionId.value]),
  queryFn: () => getSession(sessionId.value),
  enabled: computed(() => sessionId.value.length > 0),
  retry: false,
})

const BASE_TABS: { id: WorkbenchTab; label: string }[] = [
  { id: 'overview', label: '概览与进度' },
  { id: 'review', label: '审查与待办' },
  { id: 'assets', label: '素材' },
  { id: 'artifacts', label: '产物' },
]

// 「分段」分区（D6 spec §4.2 / D7 §5.4 / D8 §6.1）：判据是**路线或时长**，不是段
// 列表。导演台**恒有**这一格（这条路线永远有段表，哪怕只有一段；段数是规划产物，
// 不看 10 秒阈值）；base 只在 >10 秒出现（规划还没落盘的长片也要有这一格，显示
// 「规划中」）；ref 一律走段循环。短视频（base）的四个分区与它们的位置都不变。
const LONG_FILM_SECONDS = 10
const tabs = computed(() => {
  const entries = [...BASE_TABS]
  const mode = data.value?.mode
  if (
    mode === 'director' ||
    mode === 'ref' ||
    (data.value?.brief.duration ?? 0) > LONG_FILM_SECONDS
  ) {
    entries.splice(2, 0, { id: 'segments', label: '分段' })
  }
  // 交付分区（D8 §4.7 / #266）：只有导演台恒有——成片与最终包都由导演台导出、再交回。
  // base/ref 的交付形态是「拼接成片/交付文本」，没有这一格。
  if (mode === 'director') {
    entries.push({ id: 'delivery', label: '交付' })
  }
  return entries
})

// 事件流只作失效信号：事件、断线重同步都走同一个口——重拉详情与列表两处快照。
// 详情 query 是活动查询，失效后会立刻重拉；列表不活动时只是标记过期。
const streamFetch = inject(SESSION_STREAM_FETCH, undefined)
const {
  connection,
  currentNode,
  currentStage,
  currentNodeStartedAt,
  lastDoneNode,
  start: startStream,
  stop: stopStream,
} = useSessionStream({
  sessionId: () => sessionId.value,
  snapshotCursor: () => data.value?.last_event_seq ?? null,
  onInvalidate: async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ['session', sessionId.value] }),
      queryClient.invalidateQueries({ queryKey: ['sessions'] }),
    ])
  },
  fetchImpl: streamFetch,
})

// 快照优先（spec §6.2）：第一次拿到详情之后才订阅，补发起点用快照的 last_event_seq。
let streamArmed = false
function armStream(): void {
  if (streamArmed || !data.value) {
    return
  }
  streamArmed = true
  startStream()
}
watch(data, armStream, { immediate: true })

// 同组件承接不同会话（路由参数变化）时：断掉旧订阅、清掉旧节点、分区回概览。
// 不在这里立刻重置订阅：此时 data 可能还是上一个会话的快照，等新快照落地后
// data 的 watch 再按**新会话**的 last_event_seq 起订阅。
watch(sessionId, () => {
  streamArmed = false
  stopStream()
  currentNode.value = null
  currentStage.value = null
  ui.selectWorkbenchTab('overview')
})

function selectTab(tab: WorkbenchTab): void {
  ui.selectWorkbenchTab(tab)
}

// 页标题（2026-11-21 critique）：这是「跑十几分钟 → 停下 → 人接手」的工具，最关键
// 那一跳发生在用户切到别的标签页的时候，而此前 `document.title` 全仓零写入，标题
// 恒为「MiniMax H3 工作台」——切走就再也收不到任何信号。
// 只在「要人动手」的三态改标题：等待人工 / 失败 / 中断。切回默认态、换会话或离开
// 本页时一律还原，免得把上一个会话的待办留在标签上。
const DEFAULT_TITLE = 'MiniMax H3 工作台'
const ATTENTION_STATUSES: ReadonlySet<string> = new Set(['awaiting_human', 'failed', 'interrupted'])
const pageTitle = computed(() => {
  const detail = data.value
  if (!detail || !ATTENTION_STATUSES.has(detail.status)) {
    return DEFAULT_TITLE
  }
  // 剧情常上百字，标签页只放得下前一小截；取首个换行/标点前的一段。
  const head = (detail.plot || detail.session_id).trim().split(/[\n。，、；]/)[0] ?? ''
  const short = head.length > 20 ? `${head.slice(0, 20)}…` : head
  return `${statusLabel(detail.status)} · ${short} — ${DEFAULT_TITLE}`
})
watch(
  pageTitle,
  (value) => {
    document.title = value
  },
  { immediate: true },
)
onBeforeUnmount(() => {
  document.title = DEFAULT_TITLE
})

// 键盘切换沿用 WAI-ARIA tabs 的 roving tabindex：方向键/Home/End 换分区并移焦点。
// jsdom 不实现 Enter→click，所以真实按键由 Playwright 验证（frontend.md 的坑）。
function onTabKeydown(event: KeyboardEvent): void {
  const keys = ['ArrowLeft', 'ArrowRight', 'Home', 'End']
  if (!keys.includes(event.key)) {
    return
  }
  event.preventDefault()
  const index = tabs.value.findIndex((tab) => tab.id === ui.workbenchTab)
  let next = index
  if (event.key === 'ArrowRight') {
    next = (index + 1) % tabs.value.length
  } else if (event.key === 'ArrowLeft') {
    next = (index - 1 + tabs.value.length) % tabs.value.length
  } else if (event.key === 'Home') {
    next = 0
  } else if (event.key === 'End') {
    next = tabs.value.length - 1
  }
  ui.selectWorkbenchTab(tabs.value[next]!.id)
  const buttons = (
    event.currentTarget as HTMLElement
  ).parentElement?.querySelectorAll<HTMLButtonElement>('[role="tab"]')
  buttons?.[next]?.focus()
}

const status = computed(() => data.value?.status)

// 步骤带（#272）：快照 + 流上进度 → 有序步骤。纯函数在 progress.ts，这里只负责
// 喂参数与一个只在 running 时走动的秒表（已耗时是流上事件时间与「现在」的差）。
const now = ref(Date.now())
let clock: ReturnType<typeof setInterval> | null = null
function startClock(): void {
  if (clock === null) {
    clock = setInterval(() => {
      now.value = Date.now()
    }, 1000)
  }
}
function stopClock(): void {
  if (clock !== null) {
    clearInterval(clock)
    clock = null
  }
}
onBeforeUnmount(stopClock)
watch(
  status,
  (value) => {
    if (value === 'running') {
      startClock()
    } else {
      stopClock()
    }
  },
  { immediate: true },
)

// 流上进度只在有节点/上一步时才成立；done 之后节点名还在、但 started 时刻已清空，
// 所以「status」由计时是否存在反推——不再另加一份业务状态。
const streamProgress = computed<JobProgressView | null>(() => {
  if (currentNode.value === null && lastDoneNode.value === null) {
    return null
  }
  return {
    stage: currentStage.value,
    node: currentNode.value,
    status: currentNodeStartedAt.value !== null ? 'started' : 'done',
    startedAt: currentNodeStartedAt.value,
    previousNode: lastDoneNode.value,
  }
})

const progressView = computed<ProgressView>(() => {
  const detail = data.value
  if (!detail) {
    return { steps: [], nodeLabel: null, previousLabel: null, elapsedMs: null }
  }
  return buildProgress({
    route: detail.mode,
    variant: detail.brief.variant,
    duration: detail.brief.duration,
    directorMode: detail.brief.director_mode ?? '',
    status: detail.status,
    actionItems: detail.action_items ?? [],
    artifacts: detail.artifacts ?? [],
    segments: detail.segments ?? [],
    refs: detail.brief.refs ?? [],
    progress: streamProgress.value,
    now: now.value,
  })
})

// 失败/取消/中断后步骤带仍按快照事实保留「走到了哪一步」（pure 函数里同一套推导），
// 不在组件里另立第二份状态：状态徽章已经说明成败，步骤带只负责最后到达的位置。

// 「等待你操作」常驻行（#276）：与步骤带同层常驻，判据是快照里是否有待处理的人工
// 待办——与步骤带同一份事实，不新增业务状态。真正带副作用的操作仍在各分区，所以这
// 一行只负责把话说到「去哪一格」，点击由上面的按钮完成（2026-11-21 layout：此前它
// 是一行灰字，等待人工的会话在概览分区里全屏没有一个可点的入口）。
const pendingActionView = computed(() =>
  pendingAction(data.value?.action_items ?? [], data.value?.brief.refs ?? []),
)

// 分区 tab 上的待办计数（2026-11-21 layout）：让「哪一格在等你」在切过去之前就可见。
// 只给真实挂着卡片的几格计数；未知 kind 不计入——那类卡片前端还没有。
const pendingCounts = computed(() => pendingCountsByTarget(data.value?.action_items ?? []))
const TAB_TARGETS: Partial<Record<WorkbenchTab, 'review' | 'segments' | 'delivery'>> = {
  review: 'review',
  segments: 'segments',
  delivery: 'delivery',
}
function tabPendingCount(tab: WorkbenchTab): number {
  const target = TAB_TARGETS[tab]
  return target ? (pendingCounts.value[target] ?? 0) : 0
}

// 待办行是不是可点的：未知待办没有入口分区，那一行只能是只读文字。
const pendingTarget = computed(() => pendingActionView.value?.target ?? null)
function gotoPending(): void {
  if (pendingTarget.value) {
    selectTab(pendingTarget.value)
  }
}

// 素材分区的三态（#283）：清单还没派生 / 循环里逐件交 / 循环走完。判据全是快照事实，
// 不新增业务状态——`stage1_review` 待办与派生落行是同一次写入（#279），它出现即
// 「清单已派生」；剩下的只看台账还有没有既没定稿也没放弃的 Picture。
// 「空台账 + 未派生」与「本片确实 0 件」因此能分开（#279 的唯一分界）。
const assetPhase = computed<'created' | 'asset_loop' | 'after'>(() => {
  const detail = data.value
  if (!detail || detail.status === 'created') {
    return 'created'
  }
  const refs = detail.brief.refs ?? []
  const derived =
    refs.length > 0 || (detail.action_items ?? []).some((item) => item.kind === 'stage1_review')
  if (!derived) {
    return 'created'
  }
  const unsettled = refs.some((ref) => ref.kind === 'picture' && !isRefSettled(ref))
  return unsettled ? 'asset_loop' : 'after'
})

// 帧提交卡只在 frame_submission 待办仍 pending 时出现；决议后由快照移除。
const pendingFrameItem = computed(
  () =>
    data.value?.action_items?.find(
      (item) => item.kind === 'frame_submission' && item.status === 'pending',
    ) ?? null,
)
// 素材待办卡（D7 §4.4）：ref 会话在阶段 1 之后逐件等「交这张生成结果」，同样一次只一张。
const pendingAssetItem = computed(
  () =>
    data.value?.action_items?.find(
      (item) => item.kind === 'asset_image' && item.status === 'pending',
    ) ?? null,
)
// 素材视频/音频补交待办卡（#279 §补交口 / #284）：循环走完之后的阻塞决定点，
// 「没有，就这样」也是能点下去的答案。它排在素材图卡之后、阶段 2 之前。
const pendingAssetExtraItem = computed(
  () =>
    data.value?.action_items?.find(
      (item) => item.kind === 'asset_extra' && item.status === 'pending',
    ) ?? null,
)
// 素材分区要指出「就是这一件在等你」（#283）：件号取待办本身，不从台账反推——
// 审查还没通过、正在读图校对时台账里也有未定稿行，但那两种情况都没有卡片可指。
const pendingAssetNumber = computed(() => pendingAssetItem.value?.asset_index ?? null)
// 导演台开场首帧卡（D8 §4.4 / §7.1）：i2v / fl2v 审查通过后一个待办收两张图。
// 它不是 base 的 frame_submission——两条路线各自一个 kind，界面也各一张卡。
const pendingDirectorStartItem = computed(
  () =>
    data.value?.action_items?.find(
      (item) => item.kind === 'director_start_image' && item.status === 'pending',
    ) ?? null,
)
// 切镜段的段首图（D8 §5.6 / #264）：逐段出现，不是开场首帧的别名。
const pendingDirectorSegmentImageItem = computed(
  () =>
    data.value?.action_items?.find(
      (item) => item.kind === 'director_segment_image' && item.status === 'pending',
    ) ?? null,
)
const isCreated = computed(() => status.value === 'created')
const canRetry = computed(
  () => status.value === 'failed' || status.value === 'cancelled' || status.value === 'interrupted',
)
// 当前节点只是「最后一条进度信号」：快照里没有节点字段，断线后暂时没有它也不影响
// 业务状态（业务状态永远来自快照）。节点名翻成中文再显示（#272），与步骤带同一份
// 映射；未知节点回退原名，不隐藏。
const runningNode = computed(() =>
  status.value === 'running' && currentNode.value ? nodeLabel(currentNode.value) : null,
)
// 步骤标签来自同一条进度事件（stage）：素材循环期间这里显示「素材循环」，
// 而状态徽章仍是七态之一（D7 §5.1：SSE 与徽章都不新增分支）。
const runningStep = computed(() =>
  status.value === 'running' ? stepLabel(currentStage.value) : null,
)
const cancelRequested = computed(() => data.value?.current_job?.cancel_requested === true)
const canCancel = computed(
  () => status.value === 'running' && Boolean(data.value?.current_job?.job_id),
)

// 失败/取消/中断的诊断（2026-11-21 layout）：徽标只说成败，用户看不出「为什么停的、
// 现在重试会从哪接着跑」。两条都从快照里已有的字段推，不新增业务状态：
// 原因来自最近一次跑砸的那个 job（`error_code` / `cancelled_at_node` / `detail`），
// 后果来自服务端的续跑语义（阶段 1 从最后一个已完成节点之后接着跑，见 `stage1_resume`）。
const lastStoppedJob = computed(() => {
  if (!canRetry.value) {
    return null
  }
  const jobs = data.value?.jobs ?? []
  for (let index = jobs.length - 1; index >= 0; index -= 1) {
    const job = jobs[index]!
    if (job.status === 'failed' || job.status === 'cancelled' || job.status === 'interrupted') {
      return job
    }
  }
  return null
})

// 原因：能说清就说清。失败有 `detail`/`error_code`；取消有「停在哪一节点」；中断是
// 服务端重启对账补的标记，`job.interrupted` 的载荷是空的——这时**明说没有记录**，
// 不留一个空白让用户以为界面漏了。
const stopReason = computed(() => {
  if (!canRetry.value) {
    return null
  }
  const job = lastStoppedJob.value
  if (status.value === 'failed') {
    const detail = job?.detail?.trim()
    if (detail) {
      return `原因：${detail}`
    }
    return job?.error_code
      ? `原因：${job.error_code}（服务端未记录详细说明）`
      : '原因：服务端未记录失败原因。'
  }
  if (status.value === 'cancelled') {
    const node = job?.cancelled_at_node
    return node
      ? `原因：你在「${nodeLabel(node)}」这一步之后取消了它。`
      : '原因：你取消了这次运行。'
  }
  return '原因：服务重启或连接中断导致这次运行被标记为中断，服务端没有记录具体原因。'
})

// 后果：重试不是「从头再来」。阶段 1 已过闸门就接着跑阶段 2，否则从最后一个已完成
// 节点之后接着跑（`JobRunner._resume_step` / `step_machine.stage1_resume`）。
// 后果：重试不是「从头再来」。服务端按已落盘的事实续跑——阶段 1 已过闸门就接着跑
// 阶段 2，否则从最后一个已完成节点之后接着跑（`JobRunner._resume_step` /
// `step_machine.stage1_resume`）。两种情况的说法本来就一样，不按闸门分叉：分开写
// 只会在后端续跑语义变化时多一句假话。
const retryOutcome = computed(() =>
  canRetry.value ? '重试会从上次停下的地方接着跑，已经跑完的步骤不会重做。' : null,
)

const busy = ref(false)
const runError = ref<string | null>(null)

function errorMessageOf(err: unknown, fallback: string): string {
  return err instanceof ApiError ? (err.problem?.detail ?? err.message) : fallback
}

async function invalidateSnapshot(): Promise<void> {
  await queryClient.invalidateQueries({ queryKey: ['session', sessionId.value] })
  await queryClient.invalidateQueries({ queryKey: ['sessions'] })
}

// created 兜底 / 失败重试都是同一个动作：再 POST 一次 /jobs（ADVANCEABLE_STATUSES）。
async function runJob(): Promise<void> {
  if (busy.value || !sessionId.value) {
    return
  }
  busy.value = true
  runError.value = null
  try {
    await startSessionJob(sessionId.value, newIdempotencyKey())
    await invalidateSnapshot()
  } catch (err) {
    runError.value = errorMessageOf(err, '起跑失败，请稍后重试。')
  } finally {
    busy.value = false
  }
}

// 取消是协作式：只登记请求，任务在当前节点跑完后才停（ADR 0032）。
async function cancelRun(): Promise<void> {
  const jobId = data.value?.current_job?.job_id
  if (busy.value || !jobId) {
    return
  }
  busy.value = true
  runError.value = null
  try {
    await cancelSessionJob(sessionId.value, jobId)
    await invalidateSnapshot()
  } catch (err) {
    runError.value = errorMessageOf(err, '取消失败，请稍后重试。')
  } finally {
    busy.value = false
  }
}

const errorMessage = computed(() =>
  error.value instanceof ApiError ? error.value.message : '加载失败，请稍后重试。',
)
</script>

<template>
  <section class="detail">
    <p class="back">
      <RouterLink to="/" class="back__link">
        <ArrowLeft class="back__icon" aria-hidden="true" />返回会话列表
      </RouterLink>
    </p>

    <LoadingState v-if="isPending" />

    <ErrorNotice v-else-if="isError" :message="errorMessage">
      <Button @click="invalidateSnapshot()">重试</Button>
    </ErrorNotice>

    <template v-else-if="data">
      <header class="head">
        <h1 class="detail-title" :title="data.plot || data.session_id">
          {{ data.plot || data.session_id }}
        </h1>
        <p class="status-line">
          <StatusBadge :tone="statusTone(data.status)" :label="statusLabel(data.status)" />
          <span class="meta">{{ data.session_id }}</span>
        </p>
      </header>

      <!-- 步骤带与「等待你操作」（#276）：上移到分区 tab 之外，成为详情页常驻
           元素——切到分段/产物/审查等任一非概览分区都仍可见。取消按钮有副作用，
           仍留在概览分区（见下）。 -->
      <div class="progress-region">
        <ProgressBand v-if="progressView.steps.length" :view="progressView" />
        <!-- 有可去的分区时是按钮，点了就切过去；未知待办没有入口，退回只读一行
             （不能渲染一个点了没反应的按钮）。整行仍是 live region：新待办出现时
             读屏要听到完整一句，只播报前缀等于没说清在等什么。 -->
        <p v-if="pendingActionView" role="status" class="pending-hint">
          等待你操作：<button
            v-if="pendingTarget"
            type="button"
            class="pending-hint__goto"
            @click="gotoPending"
          >
            {{ pendingActionView.text }}
          </button>
          <template v-else>{{ pendingActionView.text }}</template>
        </p>
      </div>

      <div class="tabs" role="tablist" aria-label="工作台分区">
        <button
          v-for="tab in tabs"
          :id="`workbench-tab-${tab.id}`"
          :key="tab.id"
          type="button"
          role="tab"
          class="tab"
          :class="{ 'tab--active': ui.workbenchTab === tab.id }"
          :aria-selected="ui.workbenchTab === tab.id"
          :aria-controls="`workbench-panel-${tab.id}`"
          :tabindex="ui.workbenchTab === tab.id ? 0 : -1"
          :data-count="tabPendingCount(tab.id) || undefined"
          :aria-label="
            tabPendingCount(tab.id)
              ? `${tab.label}，${tabPendingCount(tab.id)} 项等待操作`
              : undefined
          "
          @click="selectTab(tab.id)"
          @keydown="onTabKeydown"
        >
          {{ tab.label }}
        </button>
      </div>

      <!-- 概览与进度 -->
      <section
        v-if="ui.workbenchTab === 'overview'"
        id="workbench-panel-overview"
        role="tabpanel"
        aria-labelledby="workbench-tab-overview"
        class="panel"
      >
        <ErrorNotice v-if="runError" :message="runError" />

        <p v-if="connection === 'reconnecting'" role="status" class="muted">
          实时连接中断，正在重连…
        </p>

        <div v-if="status === 'running'" class="run-controls">
          <p role="status" class="run-node">
            正在执行：<strong
              ><template v-if="runningStep">{{ runningStep }} · </template
              >{{ runningNode ?? '等待进度…' }}</strong
            >
          </p>
          <p v-if="cancelRequested" class="muted">已请求取消，当前步骤结束后停止。</p>
          <Button v-else-if="canCancel" variant="secondary" :disabled="busy" @click="cancelRun">
            {{ busy ? '正在请求取消…' : '取消' }}
          </Button>
        </div>

        <p v-else-if="isCreated" class="run-controls">
          <Button :disabled="busy" @click="runJob">{{ busy ? '正在开始…' : '开始' }}</Button>
        </p>

        <p v-else-if="canRetry" class="run-controls">
          <Button :disabled="busy" @click="runJob">{{ busy ? '正在重试…' : '重试' }}</Button>
        </p>

        <!-- 停下来的诊断：为什么停、重试会从哪接着跑。以前只有一句「失败」徽标与
             一个重试按钮，用户既不知道为什么，也不知道重试会不会白跑一遍。 -->
        <div v-if="canRetry" class="stop-report" data-stop-report>
          <p v-if="stopReason" class="stop-reason">{{ stopReason }}</p>
          <p v-if="retryOutcome" class="stop-outcome">{{ retryOutcome }}</p>
        </div>

        <dl class="facts">
          <dt>剧情</dt>
          <dd>{{ data.plot }}</dd>
          <dt>模式</dt>
          <dd>{{ data.brief.mode }}</dd>
          <dt>变体</dt>
          <dd>{{ data.brief.variant }}</dd>
          <dt>时长</dt>
          <dd>{{ data.brief.duration }} 秒</dd>
          <dt>风格</dt>
          <dd>{{ data.brief.style }}</dd>
          <dt>语言</dt>
          <dd>{{ data.brief.language }}</dd>
          <template v-if="data.brief.clarifications">
            <dt>补充说明</dt>
            <dd>{{ data.brief.clarifications }}</dd>
          </template>
          <template v-if="data.brief.draft">
            <dt>草稿</dt>
            <dd>{{ data.brief.draft }}</dd>
          </template>
        </dl>
      </section>

      <!-- 审查与待办：审查卡（#232）与帧提交卡（#233）共用本分区 -->
      <section
        v-else-if="ui.workbenchTab === 'review'"
        id="workbench-panel-review"
        role="tabpanel"
        aria-labelledby="workbench-tab-review"
        class="panel"
      >
        <AssetExtraCard
          v-if="pendingAssetExtraItem"
          :session-id="data.session_id"
          :item="pendingAssetExtraItem"
          :refs="data.brief.refs ?? []"
          @submitted="invalidateSnapshot"
          @uploaded="invalidateSnapshot"
        />
        <AssetImageCard
          v-if="pendingAssetItem"
          :session-id="data.session_id"
          :item="pendingAssetItem"
          :refs="data.brief.refs ?? []"
          :artifacts="data.artifacts ?? []"
          @submitted="invalidateSnapshot"
        />
        <FrameSubmissionCard
          v-if="pendingFrameItem"
          :session-id="data.session_id"
          :item="pendingFrameItem"
          :assets="data.assets ?? []"
          :artifacts="data.artifacts ?? []"
          @submitted="invalidateSnapshot"
        />
        <DirectorStartImageCard
          v-if="pendingDirectorStartItem"
          :session-id="data.session_id"
          :item="pendingDirectorStartItem"
          :assets="data.assets ?? []"
          @uploaded="invalidateSnapshot"
          @submitted="invalidateSnapshot"
        />
        <DirectorSegmentImageCard
          v-if="pendingDirectorSegmentImageItem"
          :session-id="data.session_id"
          :item="pendingDirectorSegmentImageItem"
          :assets="data.assets ?? []"
          @uploaded="invalidateSnapshot"
          @submitted="invalidateSnapshot"
        />
        <ReviewTab
          :session-id="sessionId"
          :action-items="data.action_items ?? []"
          :artifacts="data.artifacts ?? []"
          @resolved="invalidateSnapshot()"
        />
      </section>

      <!-- 分段（D6-1 只读规划 / D6-4 段视频回交 / D7 ref 段循环）：按路线或时长出现 -->
      <section
        v-else-if="ui.workbenchTab === 'segments'"
        id="workbench-panel-segments"
        role="tabpanel"
        aria-labelledby="workbench-tab-segments"
        class="panel"
      >
        <SegmentsTab
          :session-id="data.session_id"
          :mode="data.mode"
          :segments="data.segments ?? []"
          :assets="data.assets ?? []"
          :action-items="data.action_items ?? []"
          :artifacts="data.artifacts ?? []"
          :director-notes="data.director_notes ?? []"
          @refresh="invalidateSnapshot"
        />
      </section>

      <!-- 素材（完整交互归 #233） -->
      <section
        v-else-if="ui.workbenchTab === 'assets'"
        id="workbench-panel-assets"
        role="tabpanel"
        aria-labelledby="workbench-tab-assets"
        class="panel"
      >
        <AssetsTab
          :session-id="data.session_id"
          :assets="data.assets ?? []"
          :mode="data.mode"
          :director-mode="data.brief.director_mode ?? ''"
          :refs="data.brief.refs ?? []"
          :status="data.status"
          :phase="assetPhase"
          :current-number="pendingAssetNumber"
          :current-reason="pendingAssetItem?.reason ?? ''"
          :action-items="data.action_items ?? []"
          @refresh="invalidateSnapshot"
          @goto-review="selectTab('review')"
        />
      </section>

      <!-- 产物（D4-7）：中间产物与交付文本可看/复制/下载；媒体通用播放 -->
      <section
        v-else-if="ui.workbenchTab === 'artifacts'"
        id="workbench-panel-artifacts"
        role="tabpanel"
        aria-labelledby="workbench-tab-artifacts"
        class="panel"
      >
        <ArtifactsTab :session-id="data.session_id" :artifacts="data.artifacts ?? []" />
      </section>

      <!-- 交付（D8 §4.7 / #266）：非导演台会话没有这一格，走不到这里；用 v-else
           兜住「分区值来自持久化 UI 状态、但新会话不是导演台」的边界。 -->
      <section
        v-else
        id="workbench-panel-delivery"
        role="tabpanel"
        aria-labelledby="workbench-tab-delivery"
        class="panel"
      >
        <DeliveryTab
          :session-id="data.session_id"
          :status="data.status"
          :segments="data.segments ?? []"
          :action-items="data.action_items ?? []"
          :artifacts="data.artifacts ?? []"
          :jobs="data.jobs ?? []"
          @refresh="invalidateSnapshot"
        />
      </section>
    </template>
  </section>
</template>

<style scoped>
.detail {
  max-width: 880px;
  /* 之前只设了上限没居中，1440 宽下右侧空出 536px（2026-11-21 critique）。 */
  margin-inline: auto;
}
/* 标题按四档刻度定档（此前实渲染成浏览器默认 32px，1440 下吃掉 96px、420 下
   吃掉 240px，把 tab 与步骤带挤出首屏），并用两行截断。中文不加字距。 */
.detail-title {
  font-size: var(--text-lead);
  font-weight: 500;
  line-height: 1.5;
  color: var(--color-ink);
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
  line-clamp: 2;
  overflow: hidden;
}
/* 1440 下 880px 的内容区两侧各留 280px，读起来像一窄条。把量宽放宽到 1080px，
   正文行宽仍在可读区间（约 90 字符），同时让标题区不再显得空。 */
@media (min-width: 1200px) {
  .detail {
    max-width: 1080px;
  }
}
/* 面包屑：一枚铭牌链接，不是一句带箭头符号的正文——箭头交给图标，字距走铭牌档。 */
.back {
  margin: 0 0 var(--space-4);
}
.back__link {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  color: var(--color-ink-soft);
  font-size: var(--text-micro);
  letter-spacing: var(--track-wide);
  transition: color var(--duration-fast) var(--ease-out);
}
.back__link:hover {
  color: var(--color-ink);
}
.back__icon {
  width: 1em;
  height: 1em;
  stroke-width: 2;
}
.head {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  margin-bottom: var(--space-4);
}
/* 元信息带：徽标 + 会话 ID，同一行且垂直居中。会话 ID 走等宽数据档。 */
.status-line {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--space-2);
}
.meta {
  color: var(--color-ink-soft);
  font-family: var(--font-data);
  font-size: var(--text-micro);
  letter-spacing: var(--track-mid);
}
.tabs {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-4);
  margin-bottom: var(--space-4);
  border-bottom: 1px solid var(--color-rule);
}
/* Tab：纯文字 + 底部 2px 指示线，没有白底盒子、没有三边框。 */
.tab {
  padding: var(--space-2) 0;
  border: 0;
  border-bottom: 2px solid transparent;
  background: transparent;
  color: var(--color-ink-soft);
  font: inherit;
  font-size: var(--text-body);
  cursor: pointer;
  transition: color var(--duration-fast) var(--ease-out);
  /* 不换行 + 不压缩：窄屏下宁可折到第二行，也不能把「分段」压成单字一行
     （2026-11-21 critique：420 宽下曾各 57px 宽、单字一行）。 */
  white-space: nowrap;
  flex: 0 0 auto;
}
.tab:hover {
  color: var(--color-ink);
}
.tab--active {
  color: var(--color-ink);
  font-weight: 500;
  border-bottom-color: var(--color-signal);
}
/* 待办计数徽标：用伪元素挂数字，这样它不进入 tab 的文本内容——读屏从 aria-label
   读「审查与待办，1 项等待操作」，可见文本仍是「审查与待办」。方括号写法，钠黄。 */
.tab[data-count]::after {
  content: '[' attr(data-count) ']';
  margin-left: var(--space-1);
  color: var(--color-signal);
  font-family: var(--font-data);
  font-size: var(--text-micro);
  font-variant-numeric: tabular-nums;
}
.tab:focus-visible {
  outline: 2px solid var(--color-signal);
  outline-offset: 2px;
}
/* 内容区直接落在画布上，不进白卡；纵向节奏由各自的块间距给。 */
.panel {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
  /* tab 栏自己的 24px 下边距已经把距离给够了，这里再叠一层会让分区首行离 tab
     有 48px 以上的空档（2026-11-21 目检：素材页首屏像没排完）。 */
  padding-top: 0;
}
/* 步骤带与「等待你操作」常驻在标题之下、分区 tab 之上（#276）。 */
.progress-region {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
  margin-bottom: var(--space-4);
}
/* 「要你动手」条（#276 / 2026-11-21 重做）：整块是一条**满幅横带**——方角、无圆角、
   无边框，靠底色与上下各一道 1px 钠黄线把它从板面上切出来。
   不用「圆角盒 + 左侧色条」：那是 craft floor 明令禁止的形态，读起来像卡片装饰而不是
   「整个流程在这里停住」。整宽横带更接近本世界的语言（一条基线被打亮）。 */
.pending-hint {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: var(--space-1);
  margin: 0;
  padding: var(--space-2) var(--space-3);
  border-top: 1px solid var(--color-signal);
  border-bottom: 1px solid var(--color-signal);
  background: var(--color-plate-raised);
  color: var(--color-ink);
  font-size: var(--text-body);
}
/* 「等待你操作」的跳转按钮：视觉上仍是那行提示里的文字，但整句可点（有下划线
   与指针，键盘可聚焦）。此前它只是一段灰字，等待人工的会话在概览里无处可点。 */
.pending-hint__goto {
  padding: 0;
  border: 0;
  background: none;
  color: var(--color-signal);
  font: inherit;
  text-align: left;
  text-decoration: underline;
  text-underline-offset: 2px;
  cursor: pointer;
}
.pending-hint__goto:hover {
  text-decoration-thickness: 2px;
}
.run-controls {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--space-3);
  margin: 0;
}
.run-node {
  margin: 0;
}
/* 停下来之后的诊断：两行说明，不做成卡片也不加单边色条——它解释上面那个按钮，
   不是并列的一块内容，靠缩进与字号分层就够了。 */
.stop-report {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  font-size: var(--text-micro);
}
.stop-report p {
  margin: 0;
}
.stop-reason {
  color: var(--color-ink);
}
.stop-outcome {
  color: var(--color-ink-soft);
}
.muted {
  margin: 0;
  color: var(--color-ink-soft);
}
/* 事实表：两列 grid，行间是 1px 发光线；标签走铭牌档（字距 + 次要墨色），值是正文。 */
.facts {
  display: grid;
  grid-template-columns: max-content 1fr;
  margin: 0;
}
.facts dt {
  padding: var(--space-2) var(--space-4) var(--space-2) 0;
  border-top: 1px solid var(--color-rule);
  color: var(--color-ink-soft);
  font-size: var(--text-micro);
  letter-spacing: var(--track-wide);
}
.facts dd {
  padding: var(--space-2) 0;
  border-top: 1px solid var(--color-rule);
  color: var(--color-ink);
  font-size: var(--text-body);
  overflow-wrap: anywhere;
}
</style>
