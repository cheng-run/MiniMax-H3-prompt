<script setup lang="ts">
import { useQuery, useQueryClient } from '@tanstack/vue-query'
import { computed, inject, ref, watch } from 'vue'
import { RouterLink, useRoute } from 'vue-router'

import { ApiError, cancelSessionJob, getSession, startSessionJob } from '@/api/client'
import Button from '@/components/ui/Button.vue'
import ErrorNotice from '@/components/ui/ErrorNotice.vue'
import LoadingState from '@/components/ui/LoadingState.vue'
import StatusBadge from '@/components/ui/StatusBadge.vue'
import { useUiStore, type WorkbenchTab } from '@/stores/ui'

import AssetsTab from './AssetsTab.vue'
import ArtifactsTab from './ArtifactsTab.vue'
import AssetImageCard from './AssetImageCard.vue'
import DirectorStartImageCard from './DirectorStartImageCard.vue'
import FrameSubmissionCard from './FrameSubmissionCard.vue'
import { newIdempotencyKey } from './keys'
import ReviewTab from './ReviewTab.vue'
import SegmentsTab from './SegmentsTab.vue'
import { statusLabel, statusTone, stepLabel } from './status'
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
  return entries
})

// 事件流只作失效信号：事件、断线重同步都走同一个口——重拉详情与列表两处快照。
// 详情 query 是活动查询，失效后会立刻重拉；列表不活动时只是标记过期。
const streamFetch = inject(SESSION_STREAM_FETCH, undefined)
const {
  connection,
  currentNode,
  currentStage,
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
// 导演台开场首帧卡（D8 §4.4 / §7.1）：i2v / fl2v 审查通过后一个待办收两张图。
// 它不是 base 的 frame_submission——两条路线各自一个 kind，界面也各一张卡。
const pendingDirectorStartItem = computed(
  () =>
    data.value?.action_items?.find(
      (item) => item.kind === 'director_start_image' && item.status === 'pending',
    ) ?? null,
)
const isCreated = computed(() => status.value === 'created')
const canRetry = computed(
  () => status.value === 'failed' || status.value === 'cancelled' || status.value === 'interrupted',
)
// 当前节点只是「最后一条进度信号」：快照里没有节点字段，断线后暂时没有它也不影响
// 业务状态（业务状态永远来自快照）。
const runningNode = computed(() => (status.value === 'running' ? currentNode.value : null))
// 步骤标签来自同一条进度事件（stage）：素材循环期间这里显示「素材循环」，
// 而状态徽章仍是七态之一（D7 §5.1：SSE 与徽章都不新增分支）。
const runningStep = computed(() =>
  status.value === 'running' ? stepLabel(currentStage.value) : null,
)
const cancelRequested = computed(() => data.value?.current_job?.cancel_requested === true)
const canCancel = computed(
  () => status.value === 'running' && Boolean(data.value?.current_job?.job_id),
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
    <p class="back"><RouterLink to="/">← 返回会话列表</RouterLink></p>

    <LoadingState v-if="isPending" />

    <ErrorNotice v-else-if="isError" :message="errorMessage">
      <Button @click="invalidateSnapshot()">重试</Button>
    </ErrorNotice>

    <template v-else-if="data">
      <h1>{{ data.plot || data.session_id }}</h1>
      <p class="status-line">
        <StatusBadge :tone="statusTone(data.status)" :label="statusLabel(data.status)" />
        <span class="meta">{{ data.session_id }}</span>
      </p>

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
          :refs="data.brief.refs ?? []"
          :status="data.status"
          @refresh="invalidateSnapshot"
        />
      </section>

      <!-- 产物（D4-7）：中间产物与交付文本可看/复制/下载；媒体通用播放 -->
      <section
        v-else
        id="workbench-panel-artifacts"
        role="tabpanel"
        aria-labelledby="workbench-tab-artifacts"
        class="panel"
      >
        <ArtifactsTab :session-id="data.session_id" :artifacts="data.artifacts ?? []" />
      </section>
    </template>
  </section>
</template>

<style scoped>
.detail {
  max-width: 880px;
}
.back {
  margin: 0 0 var(--space-2);
}
.status-line {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}
.meta {
  color: var(--color-text-muted);
  font-size: 0.875rem;
}
.tabs {
  display: flex;
  gap: var(--space-1);
  margin-top: var(--space-3);
  border-bottom: 1px solid var(--color-border);
}
.tab {
  padding: var(--space-2) var(--space-3);
  border: 1px solid transparent;
  border-bottom: none;
  border-radius: var(--radius-sm) var(--radius-sm) 0 0;
  background: transparent;
  color: var(--color-text);
  font: inherit;
  cursor: pointer;
}
.tab--active {
  background: var(--color-surface);
  border-color: var(--color-border);
  font-weight: 600;
}
.tab:focus-visible {
  outline: 2px solid var(--color-focus);
  outline-offset: 2px;
}
.panel {
  padding-top: var(--space-4);
}
.run-controls {
  display: flex;
  align-items: center;
  gap: var(--space-3);
}
.run-node {
  margin: 0;
}
.muted {
  color: var(--color-text-muted);
}
.facts {
  display: grid;
  grid-template-columns: max-content 1fr;
  gap: var(--space-2) var(--space-3);
  margin-top: var(--space-4);
}
</style>
