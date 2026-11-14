<script setup lang="ts">
import { computed, reactive, ref } from 'vue'

import {
  ApiError,
  assetMediaUrl,
  deleteSessionAsset,
  uploadSessionAsset,
  type Asset,
  type RefOut,
} from '@/api/client'
import ErrorNotice from '@/components/ui/ErrorNotice.vue'

import { REF_KIND_LABELS, capacityRows } from './refCapacity'

// 素材分区（D4 spec §9 / D7 spec §4.2 / D8-8 #268）：复用 D3 的上传/列表/预览。
//
// 两种形态共用这一块，但语义不同：
// - 不走台账（base）：帧/素材上传，只增不删，不带语义名，没有容量条（D4-AC13 原样保留）。
// - 走台账（ref 与导演台 r2v）：上传必须带语义名（会话内唯一），列表按**素材台账**
//   （`brief.refs`）显示语义名/类型/编号，容量按 9/3/3/12 即时判红，起跑前可删。
//   判据只看 `mode` 与 `director_mode`：导演台只有 r2v 用台账，t2v/i2v/fl2v 没有
//   参考素材，给它们开语义名输入会让后端 422 变成「界面先放行、提交才报错」。
//
// 列表来自父组件的会话快照：不走台账用 `assets`（目录现扫），走台账用 `refs`（台账），
// 缩略图/字节/时长仍从 `assets` 按 asset_id join。本组件不维护第二份列表。
const props = withDefaults(
  defineProps<{
    sessionId: string
    assets: Asset[]
    mode?: string
    directorMode?: string
    refs?: RefOut[]
    status?: string
  }>(),
  { mode: 'base', directorMode: '', refs: () => [], status: 'created' },
)

const emit = defineEmits<{ refresh: [] }>()

const usesLedger = computed(
  () => props.mode === 'ref' || (props.mode === 'director' && props.directorMode === 'r2v'),
)
// 起跑锁（D7 §4.3）：走台账的会话离开 created 后不能再增删参考素材。base 不受限——
// 帧提交上传发生在 awaiting_human，那是既有链路。
const locked = computed(() => usesLedger.value && props.status !== 'created')
const rows = computed(() => capacityRows(props.refs))
const overCapacity = computed(() => rows.value.some((row) => row.over))

const semanticName = ref('')
const uploading = ref(false)
const uploadError = ref<string | null>(null)
const deleting = ref<string | null>(null)
const deleteError = ref<string | null>(null)
// 视频/音频时长由元素自己的 loadedmetadata 给出；jsdom 里不触发，显示为「—」。
const durations = reactive<Record<string, number>>({})

interface ListedAsset {
  asset_id: string
  name: string
  /** 走台账（ref / 导演台 r2v）是官方三型（picture/video/audio），其余是嗅探类型（image/video/audio）。 */
  kind: string
  number?: number
}

const listed = computed<ListedAsset[]>(() => {
  if (usesLedger.value) {
    return props.refs.map((ref) => ({
      asset_id: ref.asset_id,
      name: ref.name,
      kind: ref.kind,
      number: ref.number,
    }))
  }
  return props.assets.map((asset) => ({
    asset_id: asset.asset_id,
    name: asset.name,
    kind: asset.kind,
  }))
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
</script>

<template>
  <section class="assets">
    <h2>素材</h2>
    <p v-if="usesLedger" class="muted">
      参考素材（ref 与导演台
      r2v）：语义名必填、会话内唯一；类型由内容判定，编号按同类上传序自动分配。
    </p>
    <p v-else class="muted">图片可用作首/尾帧；视频与音频作为本会话素材。素材只增不删。</p>

    <!-- 容量条（D7 §4.2 / §8）：任一档超限即红；数字镜像 ref_capacity.py。 -->
    <ul v-if="usesLedger" class="capacity" aria-label="参考素材容量">
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
    <p v-if="overCapacity" class="capacity-hint" role="status">
      已超过官方单次请求上限：精简要上传的素材，超出的那一件上传会被拒绝。
    </p>

    <div class="upload">
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

    <p v-if="locked" class="muted" data-testid="assets-locked">
      会话已起跑：参考素材不能再增删（已写好的资产图词基于当时的台账）。
    </p>

    <ErrorNotice v-if="uploadError" :message="uploadError" />
    <ErrorNotice v-if="deleteError" :message="deleteError" />

    <p v-if="!listed.length" class="muted">本会话还没有素材。</p>
    <ul v-else class="asset-list">
      <li v-for="item in listed" :key="item.asset_id" class="asset">
        <div class="asset__preview">
          <img
            v-if="mediaKind(item.kind) === 'image'"
            :src="assetMediaUrl(item.asset_id)"
            :alt="item.name"
          />
          <video
            v-else-if="mediaKind(item.kind) === 'video'"
            :src="assetMediaUrl(item.asset_id)"
            controls
            muted
            preload="metadata"
            @loadedmetadata="onDurationLoaded($event, item.asset_id)"
          />
          <audio
            v-else-if="mediaKind(item.kind) === 'audio'"
            :src="assetMediaUrl(item.asset_id)"
            controls
            preload="metadata"
            @loadedmetadata="onDurationLoaded($event, item.asset_id)"
          />
          <p v-else class="muted">无法预览的素材类型：{{ item.kind }}</p>
        </div>
        <div class="asset__meta">
          <strong>{{ item.name }}</strong>
          <span class="muted">
            <template v-if="usesLedger">
              {{ kindLabel(item.kind) }} · {{ item.kind }} {{ item.number }}
            </template>
            <template v-else>{{ item.kind }}</template>
            · {{ assetById.get(item.asset_id)?.bytes ?? 0 }} 字节
            <template v-if="mediaKind(item.kind) === 'video' || mediaKind(item.kind) === 'audio'">
              · 时长 {{ durations[item.asset_id] ?? '—'
              }}{{ durations[item.asset_id] ? ' 秒' : '' }}
            </template>
          </span>
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
  font-size: 1.125rem;
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
  font-size: 0.875rem;
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
.asset__preview {
  flex: 0 0 160px;
}
.asset__preview img,
.asset__preview video {
  width: 160px;
  max-height: 120px;
  object-fit: contain;
  background: #000;
}
.asset__meta {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: var(--space-1);
}
.asset__delete {
  cursor: pointer;
}
.muted {
  color: var(--color-text-muted);
}
</style>
