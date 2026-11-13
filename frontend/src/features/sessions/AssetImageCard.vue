<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import {
  ApiError,
  getArtifactContent,
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
// 严格串行：当前件之前的编号必然已定稿（不通过时重开的还是同一个件号）。
const settled = computed(() => pictureRefs.value.filter((ref) => ref.number < number.value))

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
</script>

<template>
  <section class="asset-card">
    <h2>第 {{ number }} 件素材图 · {{ semanticName }}</h2>
    <p class="muted">
      已定稿 {{ settled.length }} / 共 {{ pictureRefs.length }} 件。
      <template v-if="settled.length">
        已定稿：{{ settled.map((ref) => ref.name).join('、') }}。
      </template>
    </p>

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
}
.field-error {
  margin: 0;
  color: var(--color-error-text);
}
.muted {
  color: var(--color-text-muted);
}
</style>
