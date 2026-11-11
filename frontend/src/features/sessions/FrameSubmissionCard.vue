<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import {
  ApiError,
  getArtifactText,
  resolveActionItem,
  type ActionItemOut,
  type ArtifactInfo,
  type Asset,
} from '@/api/client'
import Button from '@/components/ui/Button.vue'
import ErrorNotice from '@/components/ui/ErrorNotice.vue'

// 帧提交卡（D4 spec §9）：审查通过后出现的第二个人工待办。首/尾槽各绑**本会话**
// 一个图片素材，可留空；双空提交是「显式跳过」，必须先让用户看见会降级为纯文字。
//
// 提交只发 resolve；之后阶段 2 的进度由工作台的实时通道（SSE 失效信号）推着走，
// 这里不轮询。
const FIRST_FRAME_PROMPT = 'first-frame-prompt.md'
const LAST_FRAME_PROMPT = 'last-frame-prompt.md'

const props = defineProps<{
  sessionId: string
  item: ActionItemOut
  assets: Asset[]
  artifacts: ArtifactInfo[]
}>()

const emit = defineEmits<{ submitted: [] }>()

// 帧槽在服务端只接受图片素材（frame_invalid），选择器不列视频/音频。
const imageAssets = computed(() => props.assets.filter((asset) => asset.kind === 'image'))

const firstAsset = ref('')
const lastAsset = ref('')

// 帧提示词在批准前已经作为阶段 1 产物出现过；这里按登记表的文件名取回全文再展示。
const firstPrompt = ref<string | null>(null)
const lastPrompt = ref<string | null>(null)

const submitting = ref(false)
const submitError = ref<string | null>(null)
const confirmingSkip = ref(false)
const submitted = ref(false)

function promptArtifact(name: string): ArtifactInfo | null {
  return (
    props.artifacts.find(
      (artifact) => artifact.name === name && artifact.kind === 'frame_prompt',
    ) ?? null
  )
}

async function readPrompt(artifact: ArtifactInfo | null): Promise<string | null> {
  if (!artifact) {
    return null
  }
  try {
    return await getArtifactText(artifact.artifact_id)
  } catch {
    // 读不到提示词不阻断提交：帧槽本身仍可用，缺的只是参考文案。
    return null
  }
}

async function loadPrompts(): Promise<void> {
  const [first, last] = await Promise.all([
    readPrompt(promptArtifact(FIRST_FRAME_PROMPT)),
    readPrompt(promptArtifact(LAST_FRAME_PROMPT)),
  ])
  firstPrompt.value = first
  lastPrompt.value = last
}

watch(() => props.artifacts, loadPrompts, { immediate: true, deep: false })

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    return error.problem?.detail ?? error.message
  }
  return '提交失败，请稍后重试。'
}

function requestSubmit(): void {
  if (submitting.value) {
    return
  }
  submitError.value = null
  if (!firstAsset.value && !lastAsset.value) {
    confirmingSkip.value = true
    return
  }
  void submitFrames()
}

async function submitFrames(): Promise<void> {
  if (submitting.value) {
    return
  }
  submitting.value = true
  submitError.value = null
  try {
    await resolveActionItem(props.sessionId, props.item.item_id, {
      // 空槽传 null 而不是省略：双空提交是「显式跳过」，服务端据此走降级矩阵。
      frames: {
        first: firstAsset.value || null,
        last: lastAsset.value || null,
      },
    })
    submitted.value = true
    confirmingSkip.value = false
    emit('submitted')
  } catch (error) {
    submitError.value = errorMessage(error)
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <section class="frame-card">
    <h2>提交关键帧</h2>
    <p class="muted">
      审查已通过。请为本会话的首帧与尾帧各选一个图片素材；留空的可稍后通过确认跳过。
    </p>

    <div v-if="firstPrompt !== null || lastPrompt !== null" class="prompts">
      <div v-if="firstPrompt !== null" class="prompt">
        <h3>首帧提示词</h3>
        <pre>{{ firstPrompt }}</pre>
      </div>
      <div v-if="lastPrompt !== null" class="prompt">
        <h3>尾帧提示词</h3>
        <pre>{{ lastPrompt }}</pre>
      </div>
    </div>

    <div class="slots">
      <label class="slot">
        <span>首帧</span>
        <select v-model="firstAsset" name="first-frame" :disabled="submitting">
          <option value="">（不选）</option>
          <option v-for="asset in imageAssets" :key="asset.asset_id" :value="asset.asset_id">
            {{ asset.name }}
          </option>
        </select>
      </label>
      <label class="slot">
        <span>尾帧</span>
        <select v-model="lastAsset" name="last-frame" :disabled="submitting">
          <option value="">（不选）</option>
          <option v-for="asset in imageAssets" :key="asset.asset_id" :value="asset.asset_id">
            {{ asset.name }}
          </option>
        </select>
      </label>
    </div>

    <p v-if="!imageAssets.length" class="muted">本会话还没有图片素材，请先到「素材」分区上传。</p>

    <ErrorNotice v-if="submitError" :message="submitError" />

    <div v-if="confirmingSkip" role="alertdialog" aria-label="跳过帧确认" class="confirm">
      <p>首帧与尾帧都未选择：提交后将降级为纯文字模式（T2VA）。确认跳过帧？</p>
      <div class="confirm__actions">
        <Button variant="secondary" :disabled="submitting" @click="confirmingSkip = false">
          取消
        </Button>
        <Button :disabled="submitting" @click="submitFrames">确认提交</Button>
      </div>
    </div>
    <div v-else class="actions">
      <Button :disabled="submitting" @click="requestSubmit">
        {{ submitting ? '正在提交…' : '提交帧并继续' }}
      </Button>
    </div>

    <p v-if="submitted" role="status" class="muted">已提交，阶段 2 正在运行…</p>
  </section>
</template>

<style scoped>
.frame-card {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  padding: var(--space-3);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-surface);
}
.frame-card h2 {
  margin: 0;
  font-size: 1.125rem;
}
.frame-card h3 {
  margin: 0 0 var(--space-1);
  font-size: 0.95rem;
}
.prompts {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}
.prompt pre {
  margin: 0;
  padding: var(--space-2);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 0.875rem;
}
.slots {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-3);
}
.slot {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  min-width: 180px;
}
.actions,
.confirm__actions {
  display: flex;
  gap: var(--space-2);
}
.confirm {
  padding: var(--space-2);
  border: 1px solid var(--color-error-text);
  border-radius: var(--radius-md);
  background: var(--color-error-bg);
}
.muted {
  color: var(--color-text-muted);
}
</style>
