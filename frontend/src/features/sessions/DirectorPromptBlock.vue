<script setup lang="ts">
import { ref, watch } from 'vue'

import { getArtifactContent, type ArtifactInfo } from '@/api/client'
import MarkdownBlock from '@/components/ui/MarkdownBlock.vue'

import { useClipboardCopy } from './useClipboardCopy'

// 导演台按需词的可复制块（D8 §8.3 / #264）：首帧词与尾帧词同形，差别只有标题与
// artifact。正文不在快照里，挂载后才经 content 端点读一次（与段词同一条判据）。
const props = defineProps<{
  title: string
  artifact: ArtifactInfo
}>()

const text = ref<string | null>(null)
const error = ref<string | null>(null)
const copy = useClipboardCopy('复制提示词')

async function load(): Promise<void> {
  text.value = null
  error.value = null
  try {
    text.value = await getArtifactContent(props.artifact.artifact_id)
  } catch {
    error.value = '提示词读取失败。'
  }
}

watch(
  () => props.artifact.artifact_id,
  () => void load(),
  { immediate: true },
)

async function copyPrompt(): Promise<void> {
  if (text.value === null) {
    return
  }
  await copy.copy(text.value)
}
</script>

<template>
  <section class="director-prompt" :data-prompt-kind="artifact.kind" aria-label="按需词">
    <h3>{{ title }}</h3>
    <p v-if="error" class="file-error">{{ error }}</p>
    <p v-else-if="text === null" role="status" class="muted">加载中…</p>
    <template v-else>
      <MarkdownBlock class="prompt__text" :text="text" />
      <div class="prompt__actions">
        <button type="button" class="copy" :disabled="text === null" @click="copyPrompt">
          {{ copy.label.value }}
        </button>
        <span v-if="copy.state.value === 'failed'" role="alert" class="file-error">
          复制失败，请手动选择文本后复制。
        </span>
      </div>
    </template>
  </section>
</template>

<style scoped>
.director-prompt {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}
.director-prompt h3 {
  margin: 0;
  color: var(--color-ink-soft);
  font-size: var(--text-micro);
  font-weight: 400;
}
/* 按需词正文是照抄物：发光线围出的一个面，可滚。
   排版住在 MarkdownBlock（它要管标题、列表、表格），这里只给外框。 */
.prompt__text {
  margin: 0;
  padding: var(--space-3);
  border: 1px solid var(--color-rule);
  background: var(--color-plate);
  max-height: 20rem;
  overflow-y: auto;
}
.prompt__actions {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}
.copy {
  padding: var(--space-1) var(--space-3);
  border: 1px solid var(--color-rule-strong);
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--color-ink-soft);
  font: inherit;
  font-size: var(--text-micro);
  letter-spacing: var(--track-mid);
  cursor: pointer;
  transition: color var(--duration-fast) var(--ease-out);
}
.copy:hover {
  color: var(--color-ink);
  border-color: var(--color-ink-soft);
}
.copy:disabled {
  cursor: not-allowed;
}
.muted {
  margin: 0;
  color: var(--color-ink-soft);
}
.file-error {
  margin: 0;
  color: var(--color-error);
}
</style>
