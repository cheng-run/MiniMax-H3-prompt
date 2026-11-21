<script setup lang="ts">
import { computed, ref } from 'vue'

import {
  ApiError,
  resolveActionItem,
  uploadSessionAsset,
  type ActionItemOut,
  type Asset,
  type RefOut,
} from '@/api/client'
import Button from '@/components/ui/Button.vue'
import ErrorNotice from '@/components/ui/ErrorNotice.vue'

import { REF_KIND_LABELS } from './refCapacity'

// 素材视频/音频补交待办卡（#279 §补交口 / #284）。
//
// 它是素材循环之后、阶段 2 之前的**阻塞**决定点：交完了，或者明确「没有，就这样」。
// 两个答案都能点下去——「没有」不是跳过，是一个落定的决定，之后不再重复问。
//
// 文件走既有上传端点，但必须带**用户起的语义名**（设计师只给 Picture 命名）：
// 语义名 + 自动编号（`<Video N>` / `<Audio N>`，按 kind 分桶，不占 Picture 号段）
// 一起进台账；决议只指认最后交的那一件，不重抄一份清单。
const props = defineProps<{
  sessionId: string
  item: ActionItemOut
  refs: RefOut[]
}>()

const emit = defineEmits<{ submitted: []; uploaded: [] }>()

// 官方三型的顺序就是 `REF_KIND_LABELS` 的键序（picture < video < audio）：另写一张
// `{video: 0, audio: 1}` 只会在加第四型时漏一处。标签原文仍要英文（`<Video N>` 是
// 请求里的写法），由 kind 首字母大写得到——`REF_KIND_LABELS` 是给人看的中文名。
const KIND_ORDER = Object.keys(REF_KIND_LABELS)

function kindRank(kind: string): number {
  const index = KIND_ORDER.indexOf(kind)
  return index < 0 ? KIND_ORDER.length : index
}

function tagName(kind: string): string {
  return kind.charAt(0).toUpperCase() + kind.slice(1)
}

// 台账里已经登记过的补交件：刷新页面后仍然能看到，也让「完成补交」在重载后可用。
const settledExtras = computed(() =>
  props.refs
    .filter((ref) => ref.kind === 'video' || ref.kind === 'audio')
    .slice()
    .sort((a, b) => kindRank(a.kind) - kindRank(b.kind) || a.number - b.number),
)

const uploaded = ref<Asset[]>([])
const targetAssetId = computed(
  () => uploaded.value.at(-1)?.asset_id ?? settledExtras.value.at(-1)?.asset_id ?? '',
)
const extraLabel = (ref: RefOut): string => `<${tagName(ref.kind)} ${ref.number}>`

const semanticName = ref('')
const selectedFile = ref<File | null>(null)
const selectedFileName = ref('')
const uploading = ref(false)
const uploadError = ref<string | null>(null)
const submitting = ref(false)
const submitError = ref<string | null>(null)
const settled = ref(false)

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof ApiError ? (error.problem?.detail ?? error.message) : fallback
}

function onFileChange(event: Event): void {
  const input = event.target as HTMLInputElement
  selectedFile.value = input.files?.[0] ?? null
  selectedFileName.value = selectedFile.value?.name ?? ''
}

// 先起名、再上传：语义名与文件是同一次登记的两个字段，缺一个都不该发出请求。
async function addExtra(): Promise<void> {
  if (uploading.value) {
    return
  }
  const name = semanticName.value.trim()
  if (!selectedFile.value) {
    uploadError.value = '请选择一个视频或音频文件。'
    return
  }
  if (!name) {
    uploadError.value = '请给这份素材起一个语义名（例如「运镜参考」）。'
    return
  }
  uploading.value = true
  uploadError.value = null
  try {
    const asset = await uploadSessionAsset(props.sessionId, selectedFile.value, name)
    uploaded.value = [...uploaded.value, asset]
    semanticName.value = ''
    selectedFile.value = null
    selectedFileName.value = ''
    emit('uploaded')
  } catch (error) {
    uploadError.value = errorMessage(error, '上传失败，请稍后重试。')
  } finally {
    uploading.value = false
  }
}

async function submit(): Promise<void> {
  if (submitting.value) {
    return
  }
  if (!targetAssetId.value) {
    submitError.value = '还没有交回任何视频/音频：请先上传，或选择「没有，就这样」。'
    return
  }
  submitting.value = true
  submitError.value = null
  try {
    await resolveActionItem(props.sessionId, props.item.item_id, {
      decision: 'submit_extras',
      asset_id: targetAssetId.value,
    })
    settled.value = true
    emit('submitted')
  } catch (error) {
    // 容量超限也走这一支：detail 里点名了哪一档超了，原样展示。
    submitError.value = errorMessage(error, '提交失败，请稍后重试。')
  } finally {
    submitting.value = false
  }
}

async function skip(): Promise<void> {
  if (submitting.value) {
    return
  }
  submitting.value = true
  submitError.value = null
  try {
    await resolveActionItem(props.sessionId, props.item.item_id, {
      decision: 'no_extras',
    })
    settled.value = true
    emit('submitted')
  } catch (error) {
    submitError.value = errorMessage(error, '提交失败，请稍后重试。')
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <section class="extra-card">
    <h2>素材视频 / 音频补交</h2>
    <p class="muted">
      参考图已经定稿。如果这条片子还要带参考视频或音频，请在这里交回（上限 3 个视频 + 3
      个音频，与图片合计 12 个文件）；确实没有就选「没有，就这样」，不会再问第二次。
    </p>

    <ul v-if="settledExtras.length" class="extra-list" aria-label="已交回的素材">
      <li v-for="ref in settledExtras" :key="ref.asset_id">
        {{ extraLabel(ref) }}「{{ ref.name }}」
      </li>
    </ul>

    <form class="upload" @submit.prevent="addExtra">
      <label for="asset-extra-name">语义名</label>
      <input
        id="asset-extra-name"
        v-model="semanticName"
        name="asset-extra-name"
        type="text"
        placeholder="例如：运镜参考、红裙女孩的音色"
        :disabled="uploading || submitting"
      />
      <label for="asset-extra-file">视频或音频文件</label>
      <input
        id="asset-extra-file"
        name="asset-extra-file"
        type="file"
        accept="video/*,audio/*"
        :disabled="uploading || submitting"
        @change="onFileChange"
      />
      <span v-if="selectedFileName" class="file-name">{{ selectedFileName }}</span>
      <Button type="submit" variant="secondary" :disabled="uploading || submitting">
        {{ uploading ? '正在上传…' : '添加这一件' }}
      </Button>
    </form>

    <ErrorNotice v-if="uploadError" :message="uploadError" />
    <ErrorNotice v-if="submitError" :message="submitError" />

    <div class="actions">
      <Button :disabled="submitting || !targetAssetId" @click="submit">
        {{ submitting ? '正在提交…' : '交完这些，继续' }}
      </Button>
      <Button variant="secondary" :disabled="submitting" @click="skip"> 没有，就这样 </Button>
      <span v-if="settled" role="status" class="muted">已落定，正在继续。</span>
    </div>
  </section>
</template>

<style scoped>
/* 素材补交待办卡：与图卡同一语法——整块 tint 浮起，底部一条全宽钠黄基线点名
   「这一件在等你」，方角、不进白卡。不用单边色条，那只是盒子的装饰。 */
.extra-card {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  padding: var(--space-2) var(--space-3);
  background: var(--color-plate-raised);
  border-bottom: 1px solid var(--color-signal);
}
.extra-card h2 {
  margin: 0;
  font-size: var(--text-lead);
  font-weight: 500;
}
.extra-list {
  margin: 0;
  padding-left: var(--space-4);
  color: var(--color-ink-soft);
}
.upload {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  align-items: flex-start;
}
.actions {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-wrap: wrap;
}
.file-name {
  color: var(--color-ink-soft);
  font-family: var(--font-data);
  font-size: var(--text-micro);
}
.muted {
  margin: 0;
  color: var(--color-ink-soft);
}
</style>
