<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import {
  ApiError,
  getArtifactContent,
  getSession,
  resolveActionItem,
  uploadSessionAsset,
  type ActionItemOut,
  type ArtifactInfo,
} from '@/api/client'
import type { RefOut } from '@/api/client'
import Button from '@/components/ui/Button.vue'
import ErrorNotice from '@/components/ui/ErrorNotice.vue'

import { assetPromptName } from './artifactKinds'
import { newIdempotencyKey } from './keys'
import { useClipboardCopy } from './useClipboardCopy'

// 素材待办卡（D7 spec §4.4 / #256）：第 N 件 → 词全文（可复制）→ 上传生成结果。
//
// 一次只显示一件（系统严格串行），卡片随快照换件；**没有放行键**——不通过只有
// 「按差异改词后重交」这一条路，所以页面上刻意不提供「就这样吧」。
const props = defineProps<{
  sessionId: string
  item: ActionItemOut
  refs: RefOut[]
  artifacts: ArtifactInfo[]
}>()

const emit = defineEmits<{ submitted: [] }>()

const number = computed(() => props.item.asset_index ?? 0)
const pictureRefs = computed(() =>
  props.refs.filter((ref) => ref.kind === 'picture').sort((a, b) => a.number - b.number),
)
const semanticName = computed(
  () => pictureRefs.value.find((ref) => ref.number === number.value)?.name ?? '未命名',
)
// 按状态数，不按编号比：有了 `abandoned` 之后，「编号小于当前」里可能夹着放弃件，
// 把放弃件念成「已定稿」等于替用户宣布一张不存在的图已经交了。
const passedRefs = computed(() => pictureRefs.value.filter((ref) => ref.status === 'passed'))
const abandonedRefs = computed(() => pictureRefs.value.filter((ref) => ref.status === 'abandoned'))

const artifactName = computed(() => assetPromptName(number.value))
const promptArtifact = computed(
  () => props.artifacts.find((artifact) => artifact.name === artifactName.value) ?? null,
)

const prompt = ref<string | null>(null)
const promptError = ref<string | null>(null)
const { label: copyLabel, copy } = useClipboardCopy('复制词')

let loadGeneration = 0
async function loadPrompt(): Promise<void> {
  const generation = ++loadGeneration
  promptError.value = null
  const artifact = promptArtifact.value
  if (!artifact) {
    prompt.value = null
    return
  }
  try {
    const text = await getArtifactContent(artifact.artifact_id)
    if (generation === loadGeneration) {
      prompt.value = text
    }
  } catch {
    if (generation === loadGeneration) {
      prompt.value = null
      promptError.value = '词读取失败，请刷新重试。'
    }
  }
}

// 产物 id 不变、正文会变（不通过时改写覆盖同一份文件），所以按 created_at（mtime）
// 与待办 id 一起判「内容可能变了」。
watch(
  () => [promptArtifact.value?.created_at ?? '', props.item.item_id],
  () => void loadPrompt(),
  { immediate: true },
)

const selectedFile = ref<File | null>(null)
const submitting = ref(false)
const submitError = ref<string | null>(null)
const submitted = ref(false)

// 放弃这一件（#279 US9 / #282）：模型怎么都画不出来时的出口。代价只有一句，但必须
// 在点下去之前看得到——所以先展开确认，不点确认不发请求。
const confirmingAbandon = ref(false)
const abandoning = ref(false)
const abandonError = ref<string | null>(null)
const abandonNote = ref<string | null>(null)

// 与服务端 ``_abandon_asset_note`` 同一句代价（服务端把它写进已决议待办的 reason
// 与事件流）：放弃的是图，不是那些文字。确认前用现在时说明「会发生什么」，
// 成功后的回执才用服务端那句完成时。
const abandonCost = computed(
  () =>
    `放弃的是「${semanticName.value}」这一件图，不是那些文字：分镜表与段词里对` +
    `「${semanticName.value}」的指称会变成无图指称（照抄接线时这一格没有参考图可填）。` +
    '已经定稿的文本不会被自动改写。',
)
// 决议响应是 ``JobSummary``，不带这句话；回执优先取服务端写进已决议待办的 ``reason``
// （与事件流里那句同源），取不到才退回下面这份镜像——两份各写一遍时，改了一处就会
// 让「界面上说的代价」与「事件里记的代价」慢慢分家。
const abandonReceipt = computed(
  () =>
    `已放弃「${semanticName.value}」这一件：分镜表与段词里对「${semanticName.value}」` +
    '的指称会变成无图指称（照抄接线时这一格没有参考图可填）。' +
    '已经定稿的文本不会被自动改写。',
)

/** 已决议待办上的服务端原句；读不到（网络/刷新失败）时返回 null，由调用方兜底。 */
async function serverAbandonReason(): Promise<string | null> {
  try {
    const detail = await getSession(props.sessionId)
    const resolved = (detail.action_items ?? []).find(
      (entry) => entry.item_id === props.item.item_id,
    )
    return resolved?.reason?.trim() || null
  } catch {
    return null
  }
}

// 卡片随快照换件：上一件的回执与确认态不能带到下一件上。
watch(
  () => props.item.item_id,
  () => {
    confirmingAbandon.value = false
    abandonNote.value = null
    abandonError.value = null
  },
)

function onFileChange(event: Event): void {
  const input = event.target as HTMLInputElement
  selectedFile.value = input.files?.[0] ?? null
}

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof ApiError ? (error.problem?.detail ?? error.message) : fallback
}

async function submit(): Promise<void> {
  const file = selectedFile.value
  if (!file || submitting.value) {
    return
  }
  submitting.value = true
  submitError.value = null
  submitted.value = false
  try {
    // 先上传（不带语义名：生成结果不是参考素材，不进台账），再把返回的素材标识
    // 当作这次的决议载荷——待办的唯一决议就是「交哪张图」。
    const asset = await uploadSessionAsset(props.sessionId, file)
    await resolveActionItem(
      props.sessionId,
      props.item.item_id,
      { asset_id: asset.asset_id },
      newIdempotencyKey(),
    )
    submitted.value = true
    emit('submitted')
  } catch (error) {
    submitError.value = errorMessage(error, '提交失败，请稍后重试。')
  } finally {
    submitting.value = false
  }
}

async function confirmAbandon(): Promise<void> {
  if (abandoning.value) {
    return
  }
  abandoning.value = true
  abandonError.value = null
  try {
    // 不带 asset_id：放弃与交图是互斥的两种决议（服务端也会拒两者同时提交）。
    await resolveActionItem(
      props.sessionId,
      props.item.item_id,
      { decision: 'abandon_asset' },
      newIdempotencyKey(),
    )
    abandonNote.value = (await serverAbandonReason()) ?? abandonReceipt.value
    confirmingAbandon.value = false
    emit('submitted')
  } catch (error) {
    abandonError.value = errorMessage(error, '放弃失败，请稍后重试。')
  } finally {
    abandoning.value = false
  }
}
</script>

<template>
  <section class="asset-card">
    <h2>第 {{ number }} 件素材图 · {{ semanticName }}</h2>
    <p class="muted">
      已定稿 {{ passedRefs.length }} / 共 {{ pictureRefs.length }} 件<template
        v-if="abandonedRefs.length"
        >（其中已放弃 {{ abandonedRefs.length }} 件）</template
      >。
      <template v-if="passedRefs.length">
        已定稿：{{ passedRefs.map((ref) => ref.name).join('、') }}。
      </template>
    </p>
    <!-- 同一件号重开的两种情况：第一轮还没交（没有差异）与校对没过、等你重交。
         后者在这一格说清楚，用户不会以为「刚交的那张已经算数」。 -->
    <p v-if="item.reason" class="muted">这一件上一轮没通过校对：按下面的差异改词后重交。</p>

    <div class="prompt">
      <div class="prompt__head">
        <h3>
          资产图词<span class="file-name">{{ artifactName }}</span>
        </h3>
        <Button
          variant="secondary"
          :disabled="prompt === null"
          @click="prompt !== null && copy(prompt)"
        >
          {{ copyLabel }}
        </Button>
      </div>
      <p v-if="promptError" class="field-error">{{ promptError }}</p>
      <pre v-else-if="prompt === null" class="muted">词加载中…</pre>
      <pre v-else class="prompt__text">{{ prompt }}</pre>
    </div>

    <section v-if="item.reason" class="diff" aria-label="校对差异">
      <h3>校对结论：不通过</h3>
      <pre class="diff__text">{{ item.reason }}</pre>
      <p class="muted">按上面的差异改词后重新生成一张，再交回这一件。</p>
    </section>

    <form class="upload" @submit.prevent="submit">
      <label for="asset-image-file">上传这一件生成的图</label>
      <input
        id="asset-image-file"
        name="asset-image-file"
        type="file"
        accept="image/*"
        :disabled="submitting"
        @change="onFileChange"
      />
      <ErrorNotice v-if="submitError" :message="submitError" />
      <div class="actions">
        <Button type="submit" :disabled="submitting || !selectedFile">
          {{ submitting ? '正在提交…' : '提交并校对' }}
        </Button>
      </div>
    </form>

    <p v-if="submitted" role="status" class="muted">已提交，正在读图校对…</p>

    <!-- 放弃这一件（#279 US9）：模型怎么都画不出来时的出口。代价放在确认里，
         点「放弃这一件」之前不铺开，但确认按钮旁边一定看得见。 -->
    <section class="abandon" aria-label="放弃这一件">
      <Button
        v-if="!confirmingAbandon"
        type="button"
        variant="secondary"
        :disabled="submitting || abandoning"
        @click="confirmingAbandon = true"
      >
        放弃这一件
      </Button>
      <div v-else class="abandon__confirm">
        <p class="abandon__cost">{{ abandonCost }}</p>
        <div class="actions">
          <Button type="button" :disabled="abandoning" @click="confirmAbandon">
            {{ abandoning ? '正在放弃…' : '确认放弃' }}
          </Button>
          <Button
            type="button"
            variant="secondary"
            :disabled="abandoning"
            @click="confirmingAbandon = false"
          >
            取消
          </Button>
        </div>
      </div>
      <ErrorNotice v-if="abandonError" :message="abandonError" />
      <p v-if="abandonNote" role="status" class="muted abandon__note">{{ abandonNote }}</p>
    </section>
  </section>
</template>

<style scoped>
.asset-card {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  padding: var(--space-3);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-surface);
}
.asset-card h2 {
  margin: 0;
  font-size: 1.125rem;
}
.asset-card h3 {
  margin: 0 0 var(--space-1);
  font-size: 0.95rem;
}
.prompt__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
}
.file-name {
  margin-left: var(--space-2);
  color: var(--color-text-muted);
  font-size: 0.8125rem;
  font-weight: 400;
}
.prompt__text,
.diff__text {
  margin: 0;
  padding: var(--space-2);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  background: var(--color-bg);
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 0.875rem;
}
.diff {
  padding: var(--space-2);
  border: 1px solid var(--color-error-text);
  border-radius: var(--radius-md);
  background: var(--color-error-bg);
}
.upload {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}
.actions {
  display: flex;
  justify-content: flex-end;
  gap: var(--space-2);
}
.abandon {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding-top: var(--space-2);
  border-top: 1px solid var(--color-border);
}
.abandon__confirm {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}
.abandon__cost {
  margin: 0;
  color: var(--color-text-muted);
  font-size: 0.875rem;
}
.abandon__note {
  margin: 0;
}
.field-error {
  margin: 0;
  color: var(--color-error-text);
}
.muted {
  color: var(--color-text-muted);
}
</style>
