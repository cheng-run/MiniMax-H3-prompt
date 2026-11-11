<script setup lang="ts">
import { ref } from 'vue'

import { ApiError, assetMediaUrl, uploadSessionAsset, type Asset } from '@/api/client'
import ErrorNotice from '@/components/ui/ErrorNotice.vue'

// 素材分区（D4 spec §9、D4-AC13）：复用 D3 的上传/列表/预览，不提供删除。
//
// 列表来自父组件的会话快照（`SessionDetail.assets`，与 GET /assets 同一份来源）；
// 上传成功后只发 `refresh` 让父组件重拉快照，本组件不维护第二份列表。
const props = defineProps<{
  sessionId: string
  assets: Asset[]
}>()

const emit = defineEmits<{ refresh: [] }>()

const uploading = ref(false)
const uploadError = ref<string | null>(null)

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    return error.problem?.detail ?? error.message
  }
  return '上传失败，请稍后重试。'
}

async function onFileChange(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file || uploading.value) {
    return
  }
  uploading.value = true
  uploadError.value = null
  try {
    await uploadSessionAsset(props.sessionId, file)
    emit('refresh')
  } catch (error) {
    uploadError.value = errorMessage(error)
  } finally {
    uploading.value = false
    // 清空选择：同一份文件再次选中时也会触发 change（重试不被吞掉）。
    input.value = ''
  }
}
</script>

<template>
  <section class="assets">
    <h2>素材</h2>
    <p class="muted">图片可用作首/尾帧；视频与音频作为本会话素材。素材只增不删。</p>

    <div class="upload">
      <label class="upload__label" for="asset-upload">上传素材</label>
      <input
        id="asset-upload"
        type="file"
        accept="image/*,video/*,audio/*"
        :disabled="uploading"
        @change="onFileChange"
      />
      <span v-if="uploading" role="status" class="muted">正在上传…</span>
    </div>

    <ErrorNotice v-if="uploadError" :message="uploadError" />

    <p v-if="!assets.length" class="muted">本会话还没有素材。</p>
    <ul v-else class="asset-list">
      <li v-for="asset in assets" :key="asset.asset_id" class="asset">
        <div class="asset__preview">
          <img
            v-if="asset.kind === 'image'"
            :src="assetMediaUrl(asset.asset_id)"
            :alt="asset.name"
          />
          <video
            v-else-if="asset.kind === 'video'"
            :src="assetMediaUrl(asset.asset_id)"
            controls
            muted
            preload="metadata"
          />
          <audio
            v-else-if="asset.kind === 'audio'"
            :src="assetMediaUrl(asset.asset_id)"
            controls
            preload="metadata"
          />
          <p v-else class="muted">无法预览的素材类型：{{ asset.kind }}</p>
        </div>
        <div class="asset__meta">
          <strong>{{ asset.name }}</strong>
          <span class="muted">{{ asset.kind }} · {{ asset.bytes }} 字节</span>
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
  gap: var(--space-1);
}
.muted {
  color: var(--color-text-muted);
}
</style>
