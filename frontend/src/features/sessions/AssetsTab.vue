<script setup lang="ts">
import { computed, reactive, ref } from 'vue'

import {
  ApiError,
  assetMediaUrl,
  deleteSessionAsset,
  reopenSessionAsset,
  uploadSessionAsset,
  type ActionItemOut,
  type Asset,
  type RefOut,
} from '@/api/client'
import ErrorNotice from '@/components/ui/ErrorNotice.vue'

import { newIdempotencyKey } from './keys'
import { REF_KIND_LABELS, REF_STATUS_LABELS, capacityRows } from './refCapacity'
import type { AssetPhase } from './refCapacity'

// 素材分区（D4 spec §9 / D7 spec §4.2 / D7 修订 #279 / #283）。
//
// 三种形态共用这一块，但语义不同：
// - 不走台账（base）：帧/素材上传，只增不删，不带语义名，没有容量条（D4-AC13 原样保留）。
// - 导演台 r2v：起跑前由用户上传建立台账（#279「路线差异」），语义名输入与删除照旧。
// - ref：素材集合由阶段 1 派生，上传发生在素材循环的待办卡上。这里是**只读台账**——
//   三态分别回答「这片要几件、现在到哪一步、还差谁」，不提供改名改号入口（语义名与
//   编号来自资产图词，改一个字符就会让分镜表标签对不上）。唯一的写操作是循环进行中
//   对已定稿行的「换一张图」（#282）：打回这一件重交，语义名与编号仍然不动。
const props = withDefaults(
  defineProps<{
    sessionId: string
    assets: Asset[]
    mode?: string
    directorMode?: string
    refs?: RefOut[]
    status?: string
    /** ref 的三态（#283）：父组件按「清单已派生 + 台账还有没有未定稿件」算好传入。 */
    phase?: AssetPhase | ''
    /** 正在等用户交图的那一件（父组件从 pending 的 `asset_image` 待办取）。 */
    currentNumber?: number | null
    /** 当前件带上轮的校对差异＝「交过但没过，等你重交」，不是「还没交」。 */
    currentReason?: string
    /** 会话的全部待办（快照原样）：回执从这里取最近一张已决议的素材卡。 */
    actionItems?: ActionItemOut[]
  }>(),
  {
    mode: 'base',
    directorMode: '',
    refs: () => [],
    status: 'created',
    phase: '',
    currentNumber: null,
    currentReason: '',
    actionItems: () => [],
  },
)

const emit = defineEmits<{ refresh: []; gotoReview: [] }>()

const isRef = computed(() => props.mode === 'ref')
const usesLedger = computed(
  () => isRef.value || (props.mode === 'director' && props.directorMode === 'r2v'),
)
// 起跑锁（D7 §4.3 / #282 位置锁）：走台账的会话离开 created 后不能再增删参考素材。
// ref 的上传改由循环里的待办卡负责，这个锁只影响 r2v 的删除与上传框。
const locked = computed(() => usesLedger.value && props.status !== 'created')

// phase 由父组件按快照算好传入（SessionDetailView 的 `assetPhase`）。子组件不再按行数
// 猜第二遍：两份判据在「已派生但 0 件」与「还没派生」上会分家，正好是 #279 用
// `asset_manifest_derived` 才分开的那两态。空串＝父组件没给（base 不上台账）。
const phase = computed<AssetPhase>(() => (props.phase === '' ? 'created' : props.phase))
// created 态连容量条都不出现：那时台账还空着，「0/9」只会让人以为要自己凑够 9 张。
const showLedger = computed(() => !isRef.value || phase.value !== 'created')

// 已放弃的行永远不会有正本（#279）：算进用量会让容量条凭空变红。
const counted = computed(() => props.refs.filter((ref) => ref.status !== 'abandoned'))
const rows = computed(() => capacityRows(counted.value))
const overRows = computed(() => rows.value.filter((row) => row.over))
const overCapacity = computed(() => overRows.value.length > 0)
// 超限要说明超的是哪一档（#283 AC4）：只说「超了」用户不知道该减图还是减视频。
const capacityHint = computed(() =>
  overRows.value.map((row) => `${row.label} ${row.used}/${row.cap}`).join('、'),
)

const semanticName = ref('')
const uploading = ref(false)
const uploadError = ref<string | null>(null)
const deleting = ref<string | null>(null)
const deleteError = ref<string | null>(null)
const reopening = ref<string | null>(null)
const reopenError = ref<string | null>(null)
// 视频/音频时长由元素自己的 loadedmetadata 给出；jsdom 里不触发，显示为「—」。
const durations = reactive<Record<string, number>>({})

interface RowView {
  /** 列表键：台账行在还未交图时没有 asset_id，用「类型-编号」才是稳定键。 */
  key: string
  asset_id: string
  name: string
  /** 走台账（ref / 导演台 r2v）是官方三型（picture/video/audio），其余是嗅探类型。 */
  kind: string
  number?: number
  /** 台账四态之一；不上台账的形态为空串（没有登记这回事）。 */
  status: string
  /** 本件生图档位的显示名（#292）；空串＝旧台账行没有记录，界面说「未知」。 */
  image_model_label: string
}

const listed = computed<RowView[]>(() => {
  if (usesLedger.value) {
    return props.refs.map((ref) => ({
      key: `${ref.kind}-${ref.number}`,
      asset_id: ref.asset_id,
      name: ref.name,
      kind: ref.kind,
      number: ref.number,
      status: ref.status,
      image_model_label: ref.image_model_label ?? '',
    }))
  }
  return props.assets.map((asset) => ({
    key: asset.asset_id,
    asset_id: asset.asset_id,
    name: asset.name,
    kind: asset.kind,
    status: '',
    image_model_label: '',
  }))
})

const pictureRows = computed(() =>
  props.refs
    .filter((ref) => ref.kind === 'picture')
    .slice()
    .sort((left, right) => left.number - right.number),
)
// 「当前件」= 待办卡上那一件（父组件从 pending 的 `asset_image` 待办取件号）。
// 不从台账反推「第一件没定稿的」：审查还没通过、正在读图校对时台账里同样有未定稿行，
// 但那两种情况都没有卡片在等你——按台账推会凭空多出一个「等你交图」。
const currentNumber = computed(() => props.currentNumber ?? null)
const passedCount = computed(
  () => pictureRows.value.filter((ref) => ref.status === 'passed').length,
)
const abandonedCount = computed(
  () => pictureRows.value.filter((ref) => ref.status === 'abandoned').length,
)

// 决议值的人话标签（#286 复审 F）：回执要说得清「这一件是怎么定下来的」。
// 只覆盖素材卡会出现的那几档；未知值原样显示，不隐藏也不编造。
const DECISION_LABELS: Record<string, string> = {
  accept_image_facts: '按图改文案',
  keep_description: '保留原描述',
  reword_reroll: '改词再掷',
  reroll: '原词再掷',
  restore_prompt: '恢复原文',
  abandon_asset: '放弃这一件',
  submit_image: '交图',
}

interface ReceiptView {
  /** 换件后回执不串件：键取自决议本身，不是「当前这一件」。 */
  key: string
  number: number | null
  name: string
  decisionLabel: string
  reason: string
}

// 最近一张已决议素材卡的回执（#286 复审 F）：已决议的卡不再渲染，`job.detail` 只在
// 概览分区可见——「已按图补入 N 条」「校对未完成：…」「放弃说明」这些**事后**事实
// 在界面上原本没有落点，用户只看到选择前的预测文案。服务端写进已决议待办的那句
// `reason` 就是回执本体，这里原样显示，不在前端重写一份。
// 只认 `asset_image`：补交口的 reason 属于另一件事；件号与语义名一起显示，
// 因为「回执说的是哪一件」不能靠用户自己记。
const receipt = computed<ReceiptView | null>(() => {
  let latest: ActionItemOut | null = null
  for (const item of props.actionItems) {
    if (item.kind !== 'asset_image' || item.status !== 'resolved') {
      continue
    }
    if (!(item.reason ?? '').trim()) {
      continue
    }
    // 按 `resolved_at` 取最新：补报告走同一条事件（#287）但不动决议时间，
    // 所以这里的比较口径就是「人按下那一刻」。同刻取后出现的。
    if (latest === null || (item.resolved_at ?? '') >= (latest.resolved_at ?? '')) {
      latest = item
    }
  }
  if (latest === null) {
    return null
  }
  const number = latest.asset_index ?? null
  const name = pictureRows.value.find((ref) => ref.number === number)?.name ?? ''
  return {
    key: latest.item_id,
    number,
    name,
    decisionLabel: DECISION_LABELS[latest.decision ?? ''] ?? latest.decision ?? '',
    reason: (latest.reason ?? '').trim(),
  }
})

const assetById = computed(() => new Map(props.assets.map((asset) => [asset.asset_id, asset])))

function mediaKind(kind: string): string {
  return kind === 'picture' ? 'image' : kind
}

function kindLabel(kind: string): string {
  if (kind === 'picture' || kind === 'video' || kind === 'audio') {
    return REF_KIND_LABELS[kind]
  }
  return kind
}

/** 一行的状态文案：定稿/放弃/校对中都是台账事实，剩下交给「当前件是不是它」。 */
function statusText(item: RowView): string {
  if (isCurrent(item)) {
    // `pending` 一个状态盖两种处境：第一轮还没交，与校对没过、等你按差异重交。
    return props.currentReason ? '校对没过，等你重交' : '正在等你交图'
  }
  // 「待交」是台账的静态状态，但队列里排在后面的那几件并不是现在要交的——
  // 统一说「还没轮到」，免得三行都写着「待交」让人不知道该交哪一张。
  if (item.status === 'pending') {
    return '还没轮到'
  }
  return REF_STATUS_LABELS[item.status] ?? item.status
}

function isCurrent(item: RowView): boolean {
  return isRef.value && item.number !== undefined && item.number === currentNumber.value
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    return error.problem?.detail ?? error.message
  }
  return '请求失败，请稍后重试。'
}

function onDurationLoaded(event: Event, assetId: string): void {
  const element = event.target as HTMLMediaElement
  if (Number.isFinite(element.duration) && element.duration > 0) {
    durations[assetId] = Math.round(element.duration * 10) / 10
  }
}

async function onFileChange(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file || uploading.value) {
    return
  }
  const name = semanticName.value.trim()
  if (usesLedger.value && name.length === 0) {
    uploadError.value = '请先填语义名：参考素材按语义名登记，会话内不能重名。'
    input.value = ''
    return
  }
  uploading.value = true
  uploadError.value = null
  try {
    await uploadSessionAsset(props.sessionId, file, usesLedger.value ? name : undefined)
    semanticName.value = ''
    emit('refresh')
  } catch (error) {
    uploadError.value = errorMessage(error)
  } finally {
    uploading.value = false
    // 清空选择：同一份文件再次选中时也会触发 change（重试不被吞掉）。
    input.value = ''
  }
}

async function onDelete(assetId: string): Promise<void> {
  if (deleting.value !== null) {
    return
  }
  deleting.value = assetId
  deleteError.value = null
  try {
    await deleteSessionAsset(props.sessionId, assetId)
    emit('refresh')
  } catch (error) {
    deleteError.value = errorMessage(error)
  } finally {
    deleting.value = null
  }
}

/** 「换一张图」只对循环中已定稿的那一行出现（#279 US11 / #282）：循环走完后
 *  注入卡与段内子集已经用掉那份台账快照，后端同样会拒，页面不摆一个必然失败的键。 */
function canReopen(item: RowView): boolean {
  return isRef.value && phase.value === 'asset_loop' && item.status === 'passed'
}

/** 两种 409 的出路不同（路线/时机不对 vs 这一行没有可换的图），失败时按后端
 *  `problem.detail` 说话；缺文案时才按错误码兜底，不统一成「操作失败」。 */
function reopenErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    const detail = error.problem?.detail
    if (detail) {
      return detail
    }
    if (error.problem?.code === 'asset_reopen_unsupported') {
      return '这个会话或这个时机不能换图：只有素材循环进行中的 ref 会话可以换。'
    }
    if (error.problem?.code === 'asset_not_reopenable') {
      return '这一件没有可换的图：已放弃或还没定稿的那一件不在可换范围。'
    }
    return error.message
  }
  return '请求失败，请稍后重试。'
}

async function onReopen(assetId: string): Promise<void> {
  if (reopening.value !== null) {
    return
  }
  reopening.value = assetId
  reopenError.value = null
  try {
    await reopenSessionAsset(props.sessionId, assetId, newIdempotencyKey())
    emit('refresh')
  } catch (error) {
    reopenError.value = reopenErrorMessage(error)
  } finally {
    reopening.value = null
  }
}
</script>

<template>
  <section class="assets">
    <h2>素材</h2>

    <!-- ref 三态（#283）：created 只给引导文案，没有上传框——起跑不需要先有素材。 -->
    <template v-if="isRef">
      <p v-if="!showLedger" class="muted" data-testid="assets-guidance">
        起跑不需要先交图：阶段 1 完成后这里会列出本片需要的 N
        件参考素材（件数、语义名与编号都由流程定）。
      </p>
      <template v-else>
        <p class="muted" data-testid="assets-summary">
          <template v-if="pictureRows.length">
            本片共 {{ pictureRows.length }} 件参考素材：已定稿 {{ passedCount }} 件<template
              v-if="abandonedCount"
              >、已放弃 {{ abandonedCount }} 件</template
            >。
          </template>
          <template v-else>
            本片没有参考素材：阶段 1 的资产清单是空的，素材循环直接走完。
          </template>
        </p>
        <p class="muted" data-testid="assets-readonly">
          语义名与编号只读：它们由阶段 1 的资产图词决定。交图在「审查与待办」的待办卡上逐件进行。
        </p>
        <!-- 最近一件已决议素材卡的回执（#286 复审 F）：结果写在卡上，而卡换件后就不再
             渲染——不在这里留一句，「已按图补入 N 条」「校对未完成」就只剩决策前的
             预测文案可看。件号与语义名钉住它说的是哪一件。 -->
        <article v-if="receipt" class="receipt" data-testid="asset-receipt">
          <p class="receipt__title">
            上一件已决议：第 {{ receipt.number }} 件<template v-if="receipt.name"
              >「{{ receipt.name }}」</template
            ><template v-if="receipt.decisionLabel">（{{ receipt.decisionLabel }}）</template>
          </p>
          <pre class="receipt__text">{{ receipt.reason }}</pre>
        </article>
      </template>
    </template>
    <p v-else-if="usesLedger" class="muted">
      参考素材（导演台 r2v）：语义名必填、会话内唯一；类型由内容判定，编号按同类上传序自动分配。
    </p>
    <p v-else class="muted">图片可用作首/尾帧；视频与音频作为本会话素材。素材只增不删。</p>

    <!-- 容量条（D7 §4.2 / §8）：任一档超限即红，并点名超的是哪一档。 -->
    <ul v-if="showLedger && usesLedger" class="capacity" aria-label="参考素材容量">
      <li
        v-for="row in rows"
        :key="row.kind"
        :class="{ capacity__over: row.over }"
        :data-kind="row.kind"
        :data-over="row.over ? 'true' : 'false'"
      >
        {{ row.label }} {{ row.used }}/{{ row.cap }}
      </li>
    </ul>
    <p v-if="showLedger && overCapacity" class="capacity-hint" role="status">
      已超过官方单次请求上限：{{ capacityHint }}；超出的那一件提交会被拒绝。
    </p>

    <div v-if="!isRef" class="upload">
      <template v-if="usesLedger">
        <label class="upload__label" for="asset-semantic-name">语义名</label>
        <input
          id="asset-semantic-name"
          v-model="semanticName"
          name="name"
          type="text"
          :disabled="uploading || locked"
          placeholder="例如：橘猫"
        />
      </template>
      <label class="upload__label" for="asset-upload">上传素材</label>
      <input
        id="asset-upload"
        type="file"
        accept="image/*,video/*,audio/*"
        :disabled="uploading || locked"
        @change="onFileChange"
      />
      <span v-if="uploading" role="status" class="muted">正在上传…</span>
    </div>

    <p v-if="locked && !isRef" class="muted" data-testid="assets-locked">
      会话已起跑：参考素材不能再增删（已写好的资产图词基于当时的台账）。
    </p>

    <ErrorNotice v-if="uploadError" :message="uploadError" />
    <ErrorNotice v-if="deleteError" :message="deleteError" />
    <ErrorNotice v-if="reopenError" :message="reopenError" data-testid="reopen-error" />

    <p v-if="!listed.length && !isRef" class="muted">本会话还没有素材。</p>
    <ul v-else-if="listed.length" class="asset-list">
      <li
        v-for="item in listed"
        :key="item.key"
        class="asset"
        :data-kind="item.kind"
        :data-number="item.number ?? undefined"
        :data-status="item.status || undefined"
        :data-current="isCurrent(item) ? 'true' : undefined"
      >
        <div class="asset__preview">
          <img
            v-if="mediaKind(item.kind) === 'image' && item.asset_id"
            :src="assetMediaUrl(item.asset_id)"
            :alt="item.name"
          />
          <video
            v-else-if="mediaKind(item.kind) === 'video' && item.asset_id"
            :src="assetMediaUrl(item.asset_id)"
            controls
            muted
            preload="metadata"
            @loadedmetadata="onDurationLoaded($event, item.asset_id)"
          />
          <audio
            v-else-if="mediaKind(item.kind) === 'audio' && item.asset_id"
            :src="assetMediaUrl(item.asset_id)"
            controls
            preload="metadata"
            @loadedmetadata="onDurationLoaded($event, item.asset_id)"
          />
          <!-- 台账里还没有正本的行（待交/已放弃）：不画一个打不开的槽。 -->
          <p v-else-if="isRef && item.status === 'abandoned'" class="muted">已放弃：这一件没有图</p>
          <p v-else-if="isRef && item.status" class="muted">尚无图</p>
          <p v-else class="muted">无法预览的素材类型：{{ item.kind }}</p>
        </div>
        <div class="asset__meta">
          <div class="asset__title">
            <strong>{{ item.name }}</strong>
            <!-- 状态徽标只在 ref 出现：r2v 的 `registered` 只是「起跑前传过」，说成
                 「校对中」会让人以为流程在读图。 -->
            <span v-if="isRef && item.status" class="asset__status" :data-status="item.status">
              {{ statusText(item) }}
            </span>
          </div>
          <span class="muted">
            <template v-if="usesLedger">
              {{ kindLabel(item.kind) }} · {{ item.kind }} {{ item.number }}
            </template>
            <template v-else>{{ item.kind }}</template>
            <template v-if="item.asset_id">
              · {{ assetById.get(item.asset_id)?.bytes ?? 0 }} 字节
            </template>
            <template v-if="mediaKind(item.kind) === 'video' || mediaKind(item.kind) === 'audio'">
              · 时长 {{ durations[item.asset_id] ?? '—'
              }}{{ durations[item.asset_id] ? ' 秒' : '' }}
            </template>
          </span>
          <!-- 每一行都看得见自己的生图档位（#292）：12 件里一眼找到该用哪一档的那张。
               只对 Picture 显示——视频/音频没有「生图档」这回事；旧台账行没有记录时
               说「未知」，不编造。 -->
          <span
            v-if="usesLedger && item.kind === 'picture'"
            class="muted"
            data-testid="asset-model-label"
          >
            生图档：{{ item.image_model_label || '未知' }}
          </span>
          <button
            v-if="isCurrent(item)"
            type="button"
            class="asset__goto"
            @click="emit('gotoReview')"
          >
            去交这一件
          </button>
          <button
            v-if="canReopen(item)"
            type="button"
            class="asset__reopen"
            :disabled="reopening !== null"
            @click="onReopen(item.asset_id)"
          >
            {{ reopening === item.asset_id ? '正在打回…' : '换一张图' }}
          </button>
          <button
            v-if="usesLedger && !locked"
            type="button"
            class="asset__delete"
            :disabled="deleting !== null"
            @click="onDelete(item.asset_id)"
          >
            删除
          </button>
        </div>
      </li>
    </ul>
  </section>
</template>

<style scoped>
.assets {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}
.assets h2 {
  margin: 0;
  font-size: var(--font-size-title);
}
.capacity {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
  list-style: none;
  margin: 0;
  padding: 0;
  font-variant-numeric: tabular-nums;
}
.capacity li {
  padding: 2px 8px;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
}
.capacity__over {
  border-color: var(--color-error-text);
  color: var(--color-error-text);
  font-weight: 600;
}
.capacity-hint {
  margin: 0;
  color: var(--color-error-text);
  font-size: var(--font-size-label);
}
.receipt {
  margin: 0;
  padding: var(--space-2);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-surface);
}
.receipt__title {
  margin: 0 0 var(--space-1);
  font-weight: 600;
}
.receipt__text {
  margin: 0;
  white-space: pre-wrap;
  font: inherit;
  color: var(--color-text-muted);
}
.upload {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--space-2);
}
.upload__label {
  font-weight: 600;
}
.asset-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}
.asset {
  display: flex;
  gap: var(--space-3);
  padding: var(--space-2);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-surface);
}
.asset[data-current='true'] {
  border-color: var(--color-focus);
}
.asset__preview {
  flex: 0 0 160px;
}
.asset__preview img,
.asset__preview video {
  width: 160px;
  max-height: 120px;
  object-fit: contain;
  background: var(--color-media-backdrop);
}
.asset__meta {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: var(--space-1);
}
.asset__title {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}
.asset__status {
  padding: 0 6px;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  font-size: var(--font-size-caption);
}
.asset__status[data-status='passed'] {
  border-color: var(--color-focus);
}
.asset__status[data-status='abandoned'] {
  color: var(--color-text-muted);
  text-decoration: line-through;
}
.asset__delete,
.asset__goto,
.asset__reopen {
  cursor: pointer;
}
.muted {
  color: var(--color-text-muted);
}
</style>
