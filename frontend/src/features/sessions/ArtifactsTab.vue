<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue'

import { artifactContentUrl, getArtifactContent, type ArtifactInfo } from '@/api/client'

import {
  archiveDownloadLabel,
  artifactGroup,
  artifactLabel,
  isArchiveArtifact,
  sortArtifacts,
  type ArtifactGroup,
} from './artifactKinds'

// 「产物」分区（D4 spec §10）：阶段 1 的中间产物与阶段 2 的交付文本都在这里看。
// 正文经 content 端点读，不在快照里塞（stage_state 不出 API，spec §7）。
//
// 产物清单本身就是「该显示哪些文件」的事实：分组与标签按契约里的 ``kind`` 派生
// （``artifactKinds``），前端不再抄一份文件名白名单——登记表改名后这里自动跟上。
// 下载 = content 端点原样落盘；复制 = 剪贴板。媒体产物按 kind 通用渲染
// （`<video>` / `<audio>`），**不按文件名特判**成片。
const props = defineProps<{
  sessionId: string
  artifacts: ArtifactInfo[]
}>()

interface FileEntry {
  name: string
  label: string
  primary?: boolean
  artifact: ArtifactInfo
}

const byName = computed(() => new Map(props.artifacts.map((item) => [item.name, item])))

function grouped(group: ArtifactGroup): FileEntry[] {
  return sortArtifacts(props.artifacts.filter((item) => artifactGroup(item) === group)).map(
    (artifact) => ({
      name: artifact.name,
      label: artifactLabel(artifact),
      primary: artifact.kind === 'video_prompt',
      artifact,
    }),
  )
}

const intermediateFiles = computed(() => grouped('intermediate'))
const deliverableFiles = computed(() => grouped('deliverable'))
// 媒体不需要标签：保留原样的 ArtifactInfo，模板按 kind 通用渲染。
const mediaArtifacts = computed(() =>
  sortArtifacts(props.artifacts.filter((item) => artifactGroup(item) === 'media')),
)

interface FileState {
  loading: boolean
  text: string
  error: string | null
}

const states = reactive<Record<string, FileState>>({})

// 同一份产物的 artifact_id 在 revise / 重跑后仍是同名文件但内容会变：只用名字做
// 缓存键不够，加载代次挡住「旧请求晚到覆盖新正文」的竞态。
let loadGeneration = 0

async function loadTexts(): Promise<void> {
  const generation = ++loadGeneration
  const textFiles = [...intermediateFiles.value, ...deliverableFiles.value]
  await Promise.all(
    textFiles.map(async ({ name }) => {
      const artifact = byName.value.get(name)
      if (!artifact) {
        return
      }
      states[name] = { loading: true, text: '', error: null }
      try {
        const text = await getArtifactContent(artifact.artifact_id)
        if (generation === loadGeneration) {
          states[name] = { loading: false, text, error: null }
        }
      } catch {
        if (generation === loadGeneration) {
          states[name] = { loading: false, text: '', error: '读取失败。' }
        }
      }
    }),
  )
}

// 清单变了才重读（产物 id 是「内容可能变了」的信号，与审查面同一判据）。
watch(
  () => props.artifacts.map((artifact) => artifact.artifact_id).join('|'),
  () => void loadTexts(),
  { immediate: true },
)

const copiedName = ref<string | null>(null)
const copyError = ref<string | null>(null)
let copyTimer: ReturnType<typeof setTimeout> | undefined

function clearCopyTimer(): void {
  if (copyTimer !== undefined) {
    clearTimeout(copyTimer)
    copyTimer = undefined
  }
}

onBeforeUnmount(clearCopyTimer)

async function copyArtifact(name: string): Promise<void> {
  const state = states[name]
  // 正文还没读回来时按钮是禁用的；这里再挡一次竞态。
  if (!state || state.loading || state.error) {
    return
  }
  copyError.value = null
  try {
    await navigator.clipboard.writeText(state.text)
    copiedName.value = name
    clearCopyTimer()
    copyTimer = setTimeout(() => {
      if (copiedName.value === name) {
        copiedName.value = null
      }
    }, 2000)
  } catch {
    copyError.value = '复制失败，请手动选择文本后复制。'
  }
}

// 下载由浏览器接管（`<a download>`），前端拿不到完成事件；点击即给出「已开始下载」
// 的成功反馈，并记住是哪一件（与复制的反馈分开，互不覆盖）。
const downloadedName = ref<string | null>(null)

function noteDownload(name: string): void {
  downloadedName.value = name
}
</script>

<template>
  <section class="artifacts">
    <p v-if="!artifacts.length" class="muted">暂无产物。</p>

    <template v-else>
      <p v-if="copyError" role="alert" class="copy-error">{{ copyError }}</p>

      <section
        v-if="intermediateFiles.length"
        data-group="intermediate"
        aria-label="中间产物"
        class="group"
      >
        <h2>中间产物</h2>
        <article v-for="file in intermediateFiles" :key="file.name" class="artifact">
          <header class="artifact__head">
            <h3>
              {{ file.label }}<span class="file-name">{{ file.name }}</span>
            </h3>
            <span class="muted">{{ file.artifact.bytes }} 字节</span>
          </header>
          <p v-if="states[file.name]?.loading" role="status" class="muted">加载中…</p>
          <p v-else-if="states[file.name]?.error" class="file-error">
            {{ states[file.name]?.error }}
          </p>
          <pre v-else class="file-text">{{ states[file.name]?.text }}</pre>
          <div class="artifact__actions">
            <button
              type="button"
              class="copy"
              :data-copy="file.name"
              :disabled="states[file.name]?.loading || Boolean(states[file.name]?.error)"
              @click="copyArtifact(file.name)"
            >
              {{ copiedName === file.name ? '已复制' : '复制' }}
            </button>
            <a
              class="download"
              :href="artifactContentUrl(file.artifact.artifact_id)"
              :download="file.name"
              :data-download="file.name"
              @click="noteDownload(file.name)"
            >
              下载
            </a>
            <span v-if="downloadedName === file.name" role="status" class="muted">
              已开始下载
            </span>
          </div>
        </article>
      </section>

      <section
        v-if="deliverableFiles.length"
        data-group="deliverable"
        aria-label="交付文本"
        class="group"
      >
        <h2>交付文本</h2>
        <article
          v-for="file in deliverableFiles"
          :key="file.name"
          class="artifact"
          :class="{ 'artifact--primary': file.primary }"
        >
          <header class="artifact__head">
            <h3>
              {{ file.label }}<span class="file-name">{{ file.name }}</span>
            </h3>
            <span class="muted">{{ file.artifact.bytes }} 字节</span>
          </header>
          <p v-if="states[file.name]?.loading" role="status" class="muted">加载中…</p>
          <p v-else-if="states[file.name]?.error" class="file-error">
            {{ states[file.name]?.error }}
          </p>
          <pre v-else class="file-text">{{ states[file.name]?.text }}</pre>
          <div class="artifact__actions">
            <button
              type="button"
              class="copy"
              :class="{ 'copy--primary': file.primary }"
              :data-copy="file.name"
              :disabled="states[file.name]?.loading || Boolean(states[file.name]?.error)"
              @click="copyArtifact(file.name)"
            >
              {{ copiedName === file.name ? '已复制' : file.primary ? '复制最终提示词' : '复制' }}
            </button>
            <a
              class="download"
              :href="artifactContentUrl(file.artifact.artifact_id)"
              :download="file.name"
              :data-download="file.name"
              @click="noteDownload(file.name)"
            >
              下载
            </a>
            <span v-if="downloadedName === file.name" role="status" class="muted">
              已开始下载
            </span>
          </div>
        </article>
      </section>

      <section v-if="mediaArtifacts.length" data-group="media" aria-label="媒体产物" class="group">
        <h2>媒体产物</h2>
        <article v-for="artifact in mediaArtifacts" :key="artifact.artifact_id" class="artifact">
          <header class="artifact__head">
            <h3>{{ artifact.name }}</h3>
            <span class="muted">{{ artifact.bytes }} 字节</span>
          </header>
          <!-- 媒体元素由浏览器自己发请求、走 Cookie 鉴权；Range 由 D3 的 content 端点提供。 -->
          <video
            v-if="artifact.kind === 'video'"
            :src="artifactContentUrl(artifact.artifact_id)"
            controls
            preload="metadata"
          />
          <audio
            v-else-if="artifact.kind === 'audio'"
            :src="artifactContentUrl(artifact.artifact_id)"
            controls
            preload="metadata"
          />
          <!-- 归档（导演包）不是可播媒体：按 kind 给下载链接，不塞进 <video>。 -->
          <template v-else-if="isArchiveArtifact(artifact.kind)">
            <p class="muted">归档文件：下载后在 ComfyUI 导演台里导入。</p>
            <div class="artifact__actions">
              <a
                class="download"
                :href="artifactContentUrl(artifact.artifact_id)"
                :download="artifact.name.split('/').pop() ?? artifact.name"
                :data-download="artifact.name"
                @click="noteDownload(artifact.name)"
              >
                {{ archiveDownloadLabel(artifact.kind) }}
              </a>
              <span v-if="downloadedName === artifact.name" role="status" class="muted">
                已开始下载
              </span>
            </div>
          </template>
        </article>
      </section>
    </template>
  </section>
</template>

<style scoped>
.artifacts {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}
.group {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}
.group h2 {
  margin: 0;
  font-size: 1.125rem;
}
.artifact {
  padding: var(--space-3);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
}
.artifact--primary {
  border-color: var(--color-accent);
}
.artifact__head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--space-2);
}
.artifact__head h3 {
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
  max-height: 24rem;
  overflow-y: auto;
}
.artifact__actions {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin-top: var(--space-2);
}
.copy,
.download {
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
.copy--primary {
  padding: var(--space-2) var(--space-4);
  background: var(--color-accent);
  border-color: var(--color-accent);
  color: #fff;
  font-size: 1.0625rem;
  font-weight: 600;
}
.download {
  text-decoration: none;
}
.copy:focus-visible,
.download:focus-visible {
  outline: 2px solid var(--color-focus);
  outline-offset: 2px;
}
.file-error,
.copy-error {
  margin: 0;
  color: var(--color-error-text);
}
.muted {
  color: var(--color-text-muted);
}
</style>
