<script setup lang="ts">
import { computed, ref } from 'vue'

import {
  ApiError,
  artifactContentUrl,
  assetMediaUrl,
  resolveActionItem,
  uploadSessionAsset,
  type ActionItemOut,
  type ArtifactInfo,
  type JobSummary,
  type SegmentOut,
} from '@/api/client'
import Button from '@/components/ui/Button.vue'
import ErrorNotice from '@/components/ui/ErrorNotice.vue'

// 「交付」分区（D8 §4.7 / §12 / #266）：导演台会话恒有这一格。
//
// 系统**不拼片**：成片由用户在导演台合并导出、最终包由导演台导出，两者都作为本会话
// 素材交回。交付闸门只有一个人工待办（director_delivery），两个槽都必填。
//
// 页面必须明写「初始包＝计划，最终包＝结论」：改词后初始包不重建（spec §9.4），
// 不写清楚的话用户会把「包不是最新的」当成缺陷。
const props = defineProps<{
  sessionId: string
  status: string
  segments: SegmentOut[]
  actionItems: ActionItemOut[]
  artifacts: ArtifactInfo[]
  jobs: JobSummary[]
}>()

const emit = defineEmits<{ refresh: [] }>()

const pendingItem = computed(
  () =>
    props.actionItems.find(
      (item) => item.kind === 'director_delivery' && item.status === 'pending',
    ) ?? null,
)

const filmAsset = ref('')
const packAsset = ref('')
const uploading = ref<'' | 'film' | 'pack'>('')
const uploadError = ref<string | null>(null)
const submitting = ref(false)
const submitError = ref<string | null>(null)
const submitted = ref(false)

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    return error.problem?.detail ?? error.message
  }
  return '提交失败，请稍后重试。'
}

async function onUpload(slot: 'film' | 'pack', event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file || uploading.value) {
    return
  }
  uploading.value = slot
  uploadError.value = null
  try {
    const asset = await uploadSessionAsset(props.sessionId, file)
    if (slot === 'film') {
      filmAsset.value = asset.asset_id
    } else {
      packAsset.value = asset.asset_id
    }
    emit('refresh')
  } catch (error) {
    uploadError.value = errorMessage(error)
  } finally {
    uploading.value = ''
  }
}

async function submit(): Promise<void> {
  if (!pendingItem.value || submitting.value) {
    return
  }
  if (!filmAsset.value || !packAsset.value) {
    submitError.value = '成片与最终包都要先上传：两个槽都必填。'
    return
  }
  submitting.value = true
  submitError.value = null
  try {
    await resolveActionItem(props.sessionId, pendingItem.value.item_id, {
      decision: 'submit_delivery',
      video_asset_id: filmAsset.value,
      pack_asset_id: packAsset.value,
    })
    submitted.value = true
    emit('refresh')
  } catch (error) {
    // 422（包段数/宽高）后会话仍停在该待办：保留选择，用户换一份再交。
    submitError.value = errorMessage(error)
  } finally {
    submitting.value = false
  }
}

const finalPack = computed(
  () => props.artifacts.find((item) => item.kind === 'director_final_pack') ?? null,
)
const finalFilmRecord = computed(
  () => props.artifacts.find((item) => item.kind === 'director_final_film') ?? null,
)
const finalVideo = computed(
  () => props.artifacts.find((item) => item.kind === 'video' && item.name === 'final.mp4') ?? null,
)

// 最近一次交付 job：detail 里就是归档结论与改写条数（交付 job 之外的 kind 没有它）。
const deliveryJob = computed(() => {
  const jobs = props.jobs.filter((job) => job.kind === 'delivery')
  return jobs.length ? jobs[jobs.length - 1] : null
})

const allSegmentsPassed = computed(
  () => props.segments.length > 0 && props.segments.every((segment) => segment.status === 'passed'),
)
</script>

<template>
  <section class="delivery" aria-label="交付">
    <h2>交付归档</h2>
    <p class="muted">
      初始包＝计划，最终包＝结论：改过段词之后初始包不会被重建，两边的差异在交付时补记。
      系统不拼接导演台成片——请在导演台合并导出成片与最终包，再在这里交回。
    </p>

    <p v-if="segments.length && !allSegmentsPassed" class="muted">
      还有段没有通过：全部段通过之后才会出现交付入口。
    </p>

    <!-- 人工待办：两个槽都必填 -->
    <section v-if="pendingItem" class="pending" aria-label="提交交付">
      <h3>提交成片与最终包</h3>
      <div class="slots">
        <div class="slot">
          <label for="delivery-film-upload">成片（mp4）</label>
          <input
            id="delivery-film-upload"
            name="delivery-film-upload"
            type="file"
            accept="video/mp4"
            :disabled="uploading !== '' || submitting"
            @change="onUpload('film', $event)"
          />
          <span v-if="filmAsset" class="picked" role="status">已选：{{ filmAsset }}</span>
        </div>
        <div class="slot">
          <label for="delivery-pack-upload">最终包（zip）</label>
          <input
            id="delivery-pack-upload"
            name="delivery-pack-upload"
            type="file"
            accept=".zip,application/zip"
            :disabled="uploading !== '' || submitting"
            @change="onUpload('pack', $event)"
          />
          <span v-if="packAsset" class="picked" role="status">已选：{{ packAsset }}</span>
        </div>
      </div>

      <p v-if="uploading" role="status" class="muted">正在上传…</p>
      <ErrorNotice v-if="uploadError" :message="uploadError" />
      <ErrorNotice v-if="submitError" :message="submitError" />

      <div class="actions">
        <Button :disabled="submitting || !filmAsset || !packAsset" @click="submit">
          {{ submitting ? '正在提交…' : '提交成片与最终包并归档' }}
        </Button>
        <span v-if="submitted" role="status" class="muted"> 已提交，正在归档并补记改写行… </span>
      </div>
    </section>

    <!-- 归档结果：三件齐备时显示 -->
    <section
      v-if="finalPack || finalFilmRecord || finalVideo"
      class="result"
      aria-label="归档结果"
      data-delivery-result
    >
      <h3 v-if="status === 'completed'">已交付</h3>
      <h3 v-else>归档进行中</h3>

      <ul class="facts">
        <li v-if="finalVideo">
          成片：
          <a :href="assetMediaUrl(finalVideo.name)" download="final.mp4">final.mp4</a>
          <video :src="artifactContentUrl(finalVideo.artifact_id)" controls preload="metadata" />
        </li>
        <li v-if="finalPack">
          最终包：
          <a
            :href="artifactContentUrl(finalPack.artifact_id)"
            :download="finalPack.name.split('/').pop() ?? finalPack.name"
            :data-download="finalPack.name"
          >
            下载最终包
          </a>
        </li>
        <li v-if="finalFilmRecord">
          成片记录：
          <a
            :href="artifactContentUrl(finalFilmRecord.artifact_id)"
            :download="finalFilmRecord.name.split('/').pop() ?? finalFilmRecord.name"
            :data-download="finalFilmRecord.name"
          >
            {{ finalFilmRecord.name }}
          </a>
        </li>
      </ul>

      <pre v-if="deliveryJob?.detail" class="detail" data-delivery-detail>{{
        deliveryJob.detail
      }}</pre>
    </section>
  </section>
</template>

<style scoped>
.delivery {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}
.delivery h2 {
  margin: 0;
  font-size: 1.125rem;
}
.delivery h3 {
  margin: 0;
  font-size: 1rem;
}
.pending,
.result {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-3);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-surface);
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
  min-width: 240px;
}
.picked {
  color: var(--color-text-muted);
  font-size: 0.8125rem;
  word-break: break-all;
}
.actions {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}
.facts {
  margin: 0;
  padding-left: 1.25rem;
}
.facts video {
  display: block;
  width: 100%;
  max-height: 20rem;
  margin-top: var(--space-1);
  background: #000;
}
.detail {
  margin: 0;
  padding: var(--space-2);
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  white-space: pre-wrap;
  word-break: break-word;
  overflow-x: auto;
}
.muted {
  margin: 0;
  color: var(--color-text-muted);
}
</style>
