<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import {
  ApiError,
  assetMediaUrl,
  getArtifactContent,
  reopenSegment,
  resolveActionItem,
  uploadSessionAsset,
  type ActionItemOut,
  type ArtifactInfo,
  type Asset,
  type SegmentOut,
} from '@/api/client'
import Button from '@/components/ui/Button.vue'
import ErrorNotice from '@/components/ui/ErrorNotice.vue'

import DirectorPromptBlock from './DirectorPromptBlock.vue'
import { newIdempotencyKey } from './keys'
import { useClipboardCopy } from './useClipboardCopy'

// 单个执行段的展示（D6 spec §4.2 / §4.3）：段状态、提示词查看/复制、已交视频回看，
// 以及本段 `segment_video` 待办的回交表单。提示词正文不在快照里，点开才经 content
// 端点读一次（与产物分区同一判据）。
const props = defineProps<{
  sessionId: string
  segment: SegmentOut
  item: ActionItemOut | null
  assets: Asset[]
  // 本段的段级注入卡（D7 §9 / #257）：产物清单里有它才传，没有就不显示卡块。
  cardArtifact: ArtifactInfo | null
  // 本段的按需词（D8 §8.3 / #264）：切镜段的首帧词、运镜/收尾段的尾帧词。产物
  // 清单里有才传——词是按段产出的，还没轮到的段没有这两份文件。
  firstFramePromptArtifact?: ArtifactInfo | null
  tailframePromptArtifact?: ArtifactInfo | null
  // 路线能力（D7 §5.5 / #258；D8 §11 / #265）：ref 与导演台的已通过段都能「换一段」，
  // base 不开这条边（中间段换掉会断掉下一段的帧锚）。
  canResubmit: boolean
  // 导演台换段是**级联**：服务端作废第 N..末段并删缓存，随后起段任务重生成按需词，
  // 这次没有待办可立刻返回。前端据此给一句「在准备」而不是等一个不存在的待办。
  isDirector: boolean
}>()

const emit = defineEmits<{ refresh: [] }>()

// 状态文案集中一处：未知状态原样显示，不猜一个更好看的词。
const STATUS_LABELS: Record<string, string> = {
  pending: '待开始',
  waiting_video: '等待交视频',
  validating: '校验中',
  passed: '已通过',
}

function statusLabel(status: string): string {
  return STATUS_LABELS[status] ?? status
}

// 导演台的段事实（D8 §6.1 / #262）：段类型与给图清单都是**规划产物的派生值**，
// 页面上只读展示。没有 ``segment_type`` 的段（base/ref）整块不渲染。
const SEGMENT_TYPE_LABELS: Record<string, string> = {
  opening: '开场',
  continue: '续接',
  camera_same: '运镜（同机位）',
  camera_cut: '切镜',
  ending: '收尾',
}

const GIVE_IMAGE_LABELS: Record<string, string> = {
  first: '首帧',
  end: '尾帧（终点画面）',
  assets: '本段参考素材',
}

function segmentTypeLabel(type: string | null | undefined): string {
  return type ? (SEGMENT_TYPE_LABELS[type] ?? type) : ''
}

function giveImagesLabel(images: string[] | undefined): string {
  if (!images || images.length === 0) {
    return '无（纯文字）'
  }
  return images.map((token) => GIVE_IMAGE_LABELS[token] ?? token).join('、')
}

function shotsLabel(shots: number[] | undefined): string {
  return shots && shots.length > 0 ? shots.map((shot) => `镜头 ${shot}`).join('、') : '—'
}

// 换一段（D7 §5.5 / #258；D8 §11 / #265）：两条新路线能力不同构——ref 只换视频，
// 段词与段内参考素材子集一个字都不动；导演台是级联，第 N..末段一起作废。重交后
// 照样重过双闸门。base 走不到这里（按钮不渲染，服务端也会 409）。
const reopening = ref(false)
const reopenError = ref<string | null>(null)
// 导演台换段没有立刻返回的待办：段任务在后台重生成按需词，然后才挂视频待办。
// 待办出现（或段进入等视频）就收起这句提示，刷新本身仍由快照/SSE 驱动。
const preparing = ref(false)

async function reopen(): Promise<void> {
  if (reopening.value) {
    return
  }
  reopening.value = true
  reopenError.value = null
  try {
    await reopenSegment(props.sessionId, props.segment.index, newIdempotencyKey())
    preparing.value = props.isDirector
    emit('refresh')
  } catch (error) {
    // 服务端原因原样呈现：会话忙、段未通过、路线不支持、路径越界各有各的话。
    reopenError.value = errorMessage(error, '换一段失败，请稍后重试。')
  } finally {
    reopening.value = false
  }
}

watch(
  () => [props.segment.status, props.item?.item_id] as const,
  ([status, itemId]) => {
    if (itemId || status === 'waiting_video') {
      preparing.value = false
    }
  },
)

const promptOpen = ref(false)
const promptText = ref<string | null>(null)
const promptLoading = ref(false)
const promptError = ref<string | null>(null)
const copy = useClipboardCopy('复制提示词')

async function togglePrompt(): Promise<void> {
  promptOpen.value = !promptOpen.value
  // 只在第一次点开时读：段正文落盘后同一 artifact_id 可能被 revise 重写，但当前
  // 段通过前不会再写，重复请求只是浪费。
  if (!promptOpen.value || promptText.value !== null || promptLoading.value) {
    return
  }
  promptLoading.value = true
  promptError.value = null
  try {
    promptText.value = await getArtifactContent(props.segment.prompt_artifact_id)
  } catch {
    promptError.value = '提示词读取失败。'
  } finally {
    promptLoading.value = false
  }
}

async function copyPrompt(): Promise<void> {
  if (promptText.value === null) {
    return
  }
  await copy.copy(promptText.value)
}

// 段级注入卡与段词同屏（D7 §9）：卡是给人照抄的接线清单，进入这一段就自动读回，
// 不藏在按钮后面；只读展示，没有编辑控件。
const cardText = ref<string | null>(null)
const cardError = ref<string | null>(null)

async function loadCard(): Promise<void> {
  const artifact = props.cardArtifact
  cardText.value = null
  cardError.value = null
  if (!artifact) {
    return
  }
  try {
    cardText.value = await getArtifactContent(artifact.artifact_id)
  } catch {
    cardError.value = '注入卡读取失败。'
  }
}

watch(
  () => props.cardArtifact?.artifact_id,
  () => void loadCard(),
  { immediate: true },
)

// 已选素材来自两处：父组件快照里的本会话素材，以及刚上传成功、快照还没刷回来的
// 那一份。合并去重后只留 mp4——服务端按嗅探到的 media_type 硬判容器，前端能拿到的
// 只有素材名，按扩展名先筛（嗅探结果不符的由服务端 422 兜底）。
const selectedAsset = ref('')
const uploadedAssets = ref<Asset[]>([])
const videoAssets = computed(() => {
  const seen = new Set<string>()
  return [...props.assets, ...uploadedAssets.value].filter((asset) => {
    if (seen.has(asset.asset_id)) {
      return false
    }
    seen.add(asset.asset_id)
    return asset.kind === 'video' && /\.mp4$/i.test(asset.name)
  })
})

const uploading = ref(false)
const uploadError = ref<string | null>(null)
const submitting = ref(false)
const submitError = ref<string | null>(null)
const submitted = ref(false)

// 待办换成下一段时，上一段的选择/错误不能带过来（否则第二段可能拿旧素材提交）。
watch(
  () => props.item?.item_id,
  () => {
    selectedAsset.value = ''
    uploadError.value = null
    submitError.value = null
    submitted.value = false
  },
)

function errorMessage(error: unknown, fallback: string): string {
  if (error instanceof ApiError) {
    // 422 的技术档/归属原因由服务端给全，原样呈现，前端不翻译成笼统文案。
    return error.problem?.detail ?? error.message
  }
  return fallback
}

async function onUpload(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file || uploading.value) {
    return
  }
  uploading.value = true
  uploadError.value = null
  try {
    const asset = await uploadSessionAsset(props.sessionId, file)
    uploadedAssets.value = [...uploadedAssets.value, asset]
    // 上传即选中：用户上传就是为了交这一段，不必再回下拉里找一次。
    selectedAsset.value = asset.asset_id
    emit('refresh')
  } catch (error) {
    uploadError.value = errorMessage(error, '上传失败，请稍后重试。')
  } finally {
    uploading.value = false
    // 清空选择：同一份文件再次选中时也会触发 change（重试不被吞掉）。
    input.value = ''
  }
}

async function submitVideo(): Promise<void> {
  if (!props.item || !selectedAsset.value || submitting.value) {
    return
  }
  submitting.value = true
  submitError.value = null
  try {
    // 幂等键挡双击：同一次回交重复发送时服务端按键重放，不会把第二段判成第一段。
    await resolveActionItem(
      props.sessionId,
      props.item.item_id,
      { video_asset_id: selectedAsset.value },
      newIdempotencyKey(),
    )
    submitted.value = true
    emit('refresh')
  } catch (error) {
    // 422 后会话仍停在该待办：保留选择与表单，用户换一个素材可直接重交。
    submitError.value = errorMessage(error, '提交失败，请稍后重试。')
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <article class="segment-card">
    <header class="segment__head">
      <h2>第 {{ segment.index }} 段</h2>
      <span class="segment__time">{{ segment.start_s }}–{{ segment.end_s }} 秒</span>
      <span class="segment__status">{{ statusLabel(segment.status) }}</span>
      <!-- 换一段（D7 §5.5 / #258；D8 §11 / #265）：ref 与导演台的已通过段上出现，
           base 结构性禁止（按钮不渲染）。 -->
      <Button
        v-if="canResubmit && segment.status === 'passed'"
        variant="secondary"
        :disabled="reopening"
        @click="reopen"
      >
        {{ reopening ? '正在换段…' : '换一段' }}
      </Button>
    </header>
    <ErrorNotice v-if="reopenError" :message="reopenError" />
    <p v-if="preparing" role="status" class="muted">
      正在准备本段按需词…完成后会挂出本段视频待办。
    </p>
    <!-- 导演台的帧执行契约（#262）：声明帧 → 导出帧两个数都给；引导与给图清单都从
         段类型派生，界面没有任何修改入口。 -->
    <dl v-if="segment.segment_type" class="segment__grid" data-director-grid>
      <div class="segment__grid-item">
        <dt>段类型</dt>
        <dd>{{ segmentTypeLabel(segment.segment_type) }}</dd>
      </div>
      <div class="segment__grid-item">
        <dt>段间引导</dt>
        <dd>{{ segment.guided ? '开' : '关' }}</dd>
      </div>
      <div class="segment__grid-item">
        <dt>本段预期帧数（声明 → 导出）</dt>
        <dd>{{ segment.declared_frames }} → {{ segment.export_frames }}</dd>
      </div>
      <div class="segment__grid-item">
        <dt>给图清单</dt>
        <dd>{{ giveImagesLabel(segment.give_images) }}</dd>
      </div>
      <div class="segment__grid-item">
        <dt>成片帧区间</dt>
        <dd>{{ segment.start_frame }}–{{ segment.end_frame }}</dd>
      </div>
    </dl>
    <!-- 按需词（D8 §8.3 / #264）：轮到这一段才产出，产物分区里也有同一份。 -->
    <DirectorPromptBlock
      v-if="firstFramePromptArtifact"
      title="本段首帧词（切镜段）"
      :artifact="firstFramePromptArtifact"
    />
    <DirectorPromptBlock
      v-if="tailframePromptArtifact"
      title="本段尾帧词（终点画面）"
      :artifact="tailframePromptArtifact"
    />
    <p class="segment__shots">覆盖镜头：{{ shotsLabel(segment.shots_in_segment) }}</p>
    <p class="segment__summary">{{ segment.summary }}</p>

    <!-- 段内参考素材（D7 §10 / #258）：只读回显段内重编号后的 Picture 号与语义名。
         子集由规划期算好（闭包 → 撤一致性锚 → 从 1 重编号），这里没有任何手改入口。 -->
    <p v-if="segment.assets?.length" class="segment__assets" data-segment-assets>
      本段参考素材：<span v-for="(asset, index) in segment.assets" :key="asset.number"
        ><template v-if="index > 0">、</template>Picture {{ asset.number }} {{ asset.name }}</span
      >
    </p>

    <!-- 段级注入卡（D7 §9 / #257）：与本段段词同屏、只读；没有这份产物就没有这一块。 -->
    <section v-if="cardArtifact" class="injection-card" data-injection-card aria-label="本段注入卡">
      <h3>本段注入卡</h3>
      <p v-if="cardError" class="file-error">{{ cardError }}</p>
      <pre v-else-if="cardText !== null" class="prompt__text">{{ cardText }}</pre>
      <p v-else role="status" class="muted">加载中…</p>
    </section>

    <div v-if="segment.prompt_artifact_id" class="prompt">
      <button type="button" class="toggle" @click="togglePrompt">
        {{ promptOpen ? '收起提示词' : '查看提示词' }}
      </button>
      <template v-if="promptOpen">
        <p v-if="promptLoading" role="status" class="muted">加载中…</p>
        <p v-else-if="promptError" class="file-error">{{ promptError }}</p>
        <template v-else>
          <pre class="prompt__text">{{ promptText }}</pre>
          <div class="prompt__actions">
            <button type="button" class="copy" :disabled="promptText === null" @click="copyPrompt">
              {{ copy.label.value }}
            </button>
            <span v-if="copy.state.value === 'failed'" role="alert" class="file-error">
              复制失败，请手动选择文本后复制。
            </span>
          </div>
        </template>
      </template>
    </div>

    <!-- 已交视频回看：媒体元素由浏览器自己取（Cookie 鉴权），与素材分区同一约定。 -->
    <video
      v-if="segment.video_asset_id"
      class="segment__video"
      :src="assetMediaUrl(segment.video_asset_id)"
      controls
      preload="metadata"
    />

    <!-- 语义闸门拒绝后段回到等待交视频（D6 §5.4）：把服务端给的具体原因摆出来，
         否则用户只看到状态退回、不知道换哪一段。 -->
    <p
      v-if="segment.status === 'waiting_video' && segment.rejection_reason"
      class="segment__rejection"
      role="alert"
    >
      上次提交未通过：{{ segment.rejection_reason }}（请换一段视频重交）
    </p>

    <!-- 第三个人工待办（D6 §8）：本段唯一的 segment_video 待办。提交只发 resolve，
         之后由快照（SSE 失效信号）把段状态推进到 validating/passed。 -->
    <section v-if="item" class="submission" aria-label="提交本段视频">
      <h3>提交本段视频</h3>
      <label class="field">
        <span>本会话的 mp4 素材</span>
        <select v-model="selectedAsset" name="segment-video" :disabled="submitting">
          <option value="">（请选择）</option>
          <option v-for="asset in videoAssets" :key="asset.asset_id" :value="asset.asset_id">
            {{ asset.name }}
          </option>
        </select>
      </label>
      <p v-if="!videoAssets.length" class="muted">本会话还没有 mp4 素材，可在下方直接上传。</p>

      <div class="upload">
        <label :for="`segment-video-upload-${segment.index}`">上传新视频（mp4）</label>
        <input
          :id="`segment-video-upload-${segment.index}`"
          type="file"
          accept="video/mp4"
          :disabled="uploading || submitting"
          @change="onUpload"
        />
        <span v-if="uploading" role="status" class="muted">正在上传…</span>
      </div>

      <ErrorNotice v-if="uploadError" :message="uploadError" />
      <ErrorNotice v-if="submitError" :message="submitError" />

      <div class="submission__actions">
        <Button :disabled="submitting || !selectedAsset" @click="submitVideo">
          {{ submitting ? '正在提交…' : '提交视频' }}
        </Button>
        <span v-if="submitted" role="status" class="muted">已提交，正在校验…</span>
      </div>
    </section>
  </article>
</template>

<style scoped>
.segment-card {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}
.segment__head {
  display: flex;
  align-items: baseline;
  gap: var(--space-3);
}
.segment__head h2 {
  margin: 0;
  font-size: 1rem;
}
.segment__time {
  color: var(--color-text-muted);
}
.segment__status {
  margin-left: auto;
  font-weight: 600;
}
.segment__shots,
.segment__summary,
.segment__assets {
  margin: 0;
}
.segment__grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(9rem, 1fr));
  gap: var(--space-2);
  margin: 0;
  padding: var(--space-2);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
}
.segment__grid-item dt {
  color: var(--color-text-muted);
  font-size: 0.8125rem;
}
.segment__grid-item dd {
  margin: 0;
  font-variant-numeric: tabular-nums;
}
.segment__rejection {
  margin: 0;
  color: var(--color-error-text);
}
.injection-card {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}
.injection-card h3 {
  margin: 0;
  font-size: 0.95rem;
}
.prompt {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}
.prompt__text {
  margin: 0;
  padding: var(--space-2);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 20rem;
  overflow-y: auto;
}
.prompt__actions {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}
.toggle,
.copy {
  padding: var(--space-1) var(--space-3);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  background: var(--color-surface);
  color: var(--color-text);
  font: inherit;
  cursor: pointer;
}
.copy:disabled {
  cursor: not-allowed;
  opacity: 0.6;
}
.toggle:focus-visible,
.copy:focus-visible {
  outline: 2px solid var(--color-focus);
  outline-offset: 2px;
}
.segment__video {
  width: 100%;
  max-height: 22rem;
  background: #000;
}
.submission {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding-top: var(--space-2);
  border-top: 1px solid var(--color-border);
}
.submission h3 {
  margin: 0;
  font-size: 0.95rem;
}
.field {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  max-width: 22rem;
}
.upload {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--space-2);
}
.submission__actions {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}
.file-error {
  margin: 0;
  color: var(--color-error-text);
}
.muted {
  color: var(--color-text-muted);
}
</style>
