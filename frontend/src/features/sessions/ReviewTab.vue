<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'

import type { ActionItemOut, ArtifactInfo } from '@/api/client'
import { ApiError, getArtifactContent, resolveActionItem } from '@/api/client'
import Button from '@/components/ui/Button.vue'
import ErrorNotice from '@/components/ui/ErrorNotice.vue'

import { newIdempotencyKey } from './keys'
import { artifactGroup, artifactLabel, sortArtifacts } from './artifactKinds'
import { REVIEW_LAYERS, revisionLayerLabel } from './reviewLayers'

// 「审查与待办」分区（D4 spec §8）：把阶段 1 的中间产物摊开给用户审，再收一个决定。
// 数据全部来自父级持有的会话快照——本组件不自己拉详情、不订阅事件；决议成功后只
// 发 `resolved`，由工作台重拉快照（实时通道也会推一轮）。
//
// 中间产物是**按文件看**的：文本经 content 端点读，不在快照里塞正文（stage_state
// 不出 API，spec §7）。产物清单本身就是「该显示哪些文件」的事实，不在前端再判变体。

const props = defineProps<{
  sessionId: string
  actionItems: ActionItemOut[]
  artifacts: ArtifactInfo[]
}>()

const emit = defineEmits<{ resolved: [] }>()

// 阶段 1 的中间产物按 kind 派生（`artifactKinds`）：T2VA 只有剧本与分镜表，
// 帧链才会多出两帧词。前端不抄文件名白名单，登记表改名后这里自动跟上。
interface ReviewFile {
  name: string
  label: string
  artifact: ArtifactInfo
}

interface FileState {
  loading: boolean
  text: string
  error: string | null
}

const fileStates = reactive<Record<string, FileState>>({})

const reviewFiles = computed<ReviewFile[]>(() =>
  sortArtifacts(props.artifacts.filter((item) => artifactGroup(item) === 'intermediate')).map(
    (artifact) => ({
      name: artifact.name,
      label: artifactLabel(artifact),
      artifact,
    }),
  ),
)

// 待审查的闸门：只认 `stage1_review` 的 pending。帧提交待办属于素材分区（#233）。
const pendingReview = computed(
  () =>
    props.actionItems.find((item) => item.kind === 'stage1_review' && item.status === 'pending') ??
    null,
)

// 修订历史从**待办列表**重建（spec §7）：决议时收到什么就回显什么，不读 stage_state。
// 已决议但没带修订的（approve）不进历史；列表本身就是时间序，不再自己排序。
const revisionHistory = computed(() =>
  props.actionItems.filter((item) => (item.revisions?.length ?? 0) > 0),
)

let loadGeneration = 0

function errorTextOf(error: unknown, fallback: string): string {
  return error instanceof ApiError ? (error.problem?.detail ?? error.message) : fallback
}

async function loadFiles(): Promise<void> {
  const generation = ++loadGeneration
  await Promise.all(
    reviewFiles.value.map(async ({ name, artifact }) => {
      fileStates[name] = { loading: true, text: '', error: null }
      try {
        const text = await getArtifactContent(artifact.artifact_id)
        if (generation === loadGeneration) {
          fileStates[name] = { loading: false, text, error: null }
        }
      } catch (error) {
        if (generation === loadGeneration) {
          fileStates[name] = {
            loading: false,
            text: '',
            error: errorTextOf(error, '读取失败。'),
          }
        }
      }
    }),
  )
}

// 重新加载的触发条件＝产物清单或当前闸门的待办变了。revise 重跑后会挂一个**新
// item_id** 的待办，文件正文也在同一轮刷新，所以待办 id 是「内容可能变了」的信号。
// 只看产物 id 不够：文件名不变、内容会变（spec §7 要求 revise 后文件同步更新）。
watch(
  () => [
    props.artifacts.map((artifact) => artifact.artifact_id).join('|'),
    pendingReview.value?.item_id ?? '',
  ],
  () => void loadFiles(),
  { immediate: true },
)

// 决议：同一时刻只允许一次提交（幂等键挡双击，这里再挡一次重复点击的视觉抖动）。
const resolving = ref(false)
const actionError = ref<string | null>(null)

async function approve(): Promise<void> {
  const item = pendingReview.value
  if (!item || resolving.value) {
    return
  }
  resolving.value = true
  actionError.value = null
  try {
    // 「通过」就是通过：不带 revisions（服务端也会拒），避免把没提交的意见当决定带上。
    await resolveActionItem(
      props.sessionId,
      item.item_id,
      { decision: 'approve' },
      newIdempotencyKey(),
    )
    emit('resolved')
  } catch (error) {
    actionError.value = errorTextOf(error, '提交失败，请稍后重试。')
  } finally {
    resolving.value = false
  }
}

// 提意见：四档层次 + 一条意见，**单条提交**（对齐向导一轮一条）；历史随重跑累积。
const revisionLayer = ref(REVIEW_LAYERS[0]!.value)
const revisionText = ref('')
const revisionError = ref<string | null>(null)
const revising = ref(false)

async function submitRevision(): Promise<void> {
  const item = pendingReview.value
  if (!item || revising.value) {
    return
  }
  actionError.value = null
  const text = revisionText.value.trim()
  // 空文本即时拦下，但服务端仍是权威（它也会 422；用例咬住 detail 原样呈现）。
  revisionError.value = text ? null : '修订内容不能为空。'
  if (!text) {
    return
  }
  revising.value = true
  try {
    await resolveActionItem(
      props.sessionId,
      item.item_id,
      { decision: 'revise', revisions: [{ layer: revisionLayer.value, text }] },
      newIdempotencyKey(),
    )
    revisionText.value = ''
    emit('resolved')
  } catch (error) {
    actionError.value = errorTextOf(error, '提交失败，请稍后重试。')
  } finally {
    revising.value = false
  }
}
</script>

<template>
  <div class="review">
    <section v-if="pendingReview" class="review-card" aria-label="阶段 1 审查">
      <h2>阶段 1 中间产物</h2>

      <article v-for="file in reviewFiles" :key="file.name" class="review-file">
        <h3>
          {{ file.label }}<span class="file-name">{{ file.name }}</span>
        </h3>
        <p v-if="fileStates[file.name]?.loading" role="status" class="muted">加载中…</p>
        <p v-else-if="fileStates[file.name]?.error" class="file-error">
          {{ fileStates[file.name]?.error }}
        </p>
        <pre v-else class="file-text">{{ fileStates[file.name]?.text }}</pre>
      </article>

      <p v-if="reviewFiles.length === 0" class="muted">暂无可查看的中间产物。</p>

      <ErrorNotice v-if="actionError" :message="actionError" />

      <div class="decision">
        <Button :disabled="resolving" @click="approve">
          {{ resolving ? '正在提交…' : '通过' }}
        </Button>

        <form class="revision-form" novalidate @submit.prevent="submitRevision">
          <label for="revision-layer">修改层次</label>
          <select
            id="revision-layer"
            v-model="revisionLayer"
            name="revision-layer"
            :disabled="revising"
          >
            <option v-for="layer in REVIEW_LAYERS" :key="layer.value" :value="layer.value">
              {{ layer.value }}
            </option>
          </select>
          <p class="layer-note">
            {{ REVIEW_LAYERS.find((layer) => layer.value === revisionLayer)?.note }}
          </p>

          <label for="revision-text">修改意见</label>
          <textarea
            id="revision-text"
            v-model="revisionText"
            name="revision-text"
            rows="3"
            :disabled="revising"
            :aria-invalid="revisionError !== null"
            :aria-describedby="revisionError ? 'revision-text-error' : undefined"
            placeholder="一轮提一条：说清要改什么"
          />
          <p v-if="revisionError" id="revision-text-error" class="field-error">
            {{ revisionError }}
          </p>

          <div class="actions">
            <Button type="submit" variant="secondary" :disabled="revising">
              {{ revising ? '正在提交…' : '提交意见' }}
            </Button>
          </div>
        </form>
      </div>
    </section>
    <p v-else class="muted">
      {{ actionItems.length === 0 ? '暂无待办。' : '当前没有待审查的待办。' }}
    </p>

    <section v-if="revisionHistory.length" class="revision-history" aria-label="修订历史">
      <h2>修订历史</h2>
      <ol>
        <li v-for="item in revisionHistory" :key="item.item_id">
          <ul>
            <li v-for="(revision, index) in item.revisions" :key="index">
              <span class="layer">{{ revisionLayerLabel(revision.layer) }}</span>
              <span class="revision-text">{{ revision.text }}</span>
            </li>
          </ul>
        </li>
      </ol>
    </section>
  </div>
</template>

<style scoped>
.review {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}
.review-card {
  padding: var(--space-3);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
}
.review-file {
  margin-top: var(--space-3);
}
.review-file h3 {
  margin: 0 0 var(--space-1);
  font-size: 1rem;
}
.file-name {
  margin-left: var(--space-2);
  color: var(--color-text-muted);
  font-size: 0.8125rem;
  font-weight: 400;
}
.file-text {
  margin: 0;
  padding: var(--space-2);
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  white-space: pre-wrap;
  word-break: break-word;
  overflow-x: auto;
}
.file-error {
  color: var(--color-error-text);
  margin: 0;
}
.muted {
  color: var(--color-text-muted);
}
.decision {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  margin-top: var(--space-4);
  padding-top: var(--space-3);
  border-top: 1px solid var(--color-border);
}
.decision > .btn {
  align-self: flex-start;
}
.revision-form {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}
.layer-note {
  margin: 0 0 var(--space-2);
  color: var(--color-text-muted);
  font-size: 0.875rem;
}
.field-error {
  margin: 0;
  color: var(--color-error-text);
  font-size: 0.875rem;
}
.actions {
  display: flex;
  justify-content: flex-end;
}
.revision-history ol,
.revision-history ul {
  margin: 0;
  padding-left: var(--space-4);
}
.layer {
  margin-right: var(--space-2);
  font-size: 0.875rem;
  color: var(--color-text-muted);
}
</style>
