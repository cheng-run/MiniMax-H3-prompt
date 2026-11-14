<script setup lang="ts">
import { computed, ref } from 'vue'

import {
  ApiError,
  resolveActionItem,
  uploadSessionAsset,
  type ActionItemOut,
  type Asset,
} from '@/api/client'
import Button from '@/components/ui/Button.vue'
import ErrorNotice from '@/components/ui/ErrorNotice.vue'

// 切镜段的首帧图交接卡（D8 §5.6 / #264）：与开场首帧不是同一件事——开场首帧是
// 全片第一张、只收一次；这张是**逐段**的图槽，轮到的段才出现。只收本会话已上传的
// 图片素材，不接受本地路径（与所有图槽同一条不变式）。
const props = defineProps<{
  sessionId: string
  item: ActionItemOut
  assets: Asset[]
}>()

const emit = defineEmits<{ submitted: []; uploaded: [] }>()

// 卡内刚上传的素材还没进父组件快照，先并进来，否则选中值没有对应选项。
const extraAssets = ref<Asset[]>([])
const imageAssets = computed(() => {
  const seen = new Set<string>()
  return [...props.assets, ...extraAssets.value].filter((asset) => {
    if (asset.kind !== 'image' || seen.has(asset.asset_id)) {
      return false
    }
    seen.add(asset.asset_id)
    return true
  })
})

const selected = ref('')
const uploading = ref(false)
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

function segmentLabel(): string {
  return props.item.segment_index ? `第 ${props.item.segment_index} 段` : '本段'
}

async function onUpload(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file || uploading.value) {
    return
  }
  uploading.value = true
  uploadError.value = null
  try {
    const asset = await uploadSessionAsset(props.sessionId, file)
    extraAssets.value = [...extraAssets.value, asset]
    selected.value = asset.asset_id
    emit('uploaded')
  } catch (error) {
    uploadError.value = errorMessage(error)
  } finally {
    uploading.value = false
  }
}

async function submit(): Promise<void> {
  if (submitting.value || !selected.value) {
    return
  }
  submitting.value = true
  submitError.value = null
  try {
    await resolveActionItem(props.sessionId, props.item.item_id, {
      decision: 'submit_segment_image',
      asset_id: selected.value,
    })
    submitted.value = true
    emit('submitted')
  } catch (error) {
    submitError.value = errorMessage(error)
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <section class="segment-image-card">
    <h2>{{ segmentLabel() }}的段首帧</h2>
    <p class="muted">
      本段是切镜段，需要一张自己的首帧。把按需生成的图片作为本会话图片素材交回；尾帧词会以它为参考图。
    </p>

    <label for="director-segment-image">本会话的图片素材</label>
    <select
      id="director-segment-image"
      v-model="selected"
      name="director-segment-image"
      :disabled="submitting"
    >
      <option value="">（请选择）</option>
      <option v-for="asset in imageAssets" :key="asset.asset_id" :value="asset.asset_id">
        {{ asset.name }}
      </option>
    </select>
    <p v-if="!imageAssets.length" class="muted">本会话还没有图片素材，可直接在下方上传。</p>

    <div class="upload">
      <label for="director-segment-image-upload">上传首帧图</label>
      <input
        id="director-segment-image-upload"
        name="director-segment-image-upload"
        type="file"
        accept="image/*"
        :disabled="uploading || submitting"
        @change="onUpload"
      />
      <span v-if="uploading" role="status" class="muted">正在上传…</span>
    </div>

    <ErrorNotice v-if="uploadError" :message="uploadError" />
    <ErrorNotice v-if="submitError" :message="submitError" />

    <div class="actions">
      <Button :disabled="submitting || !selected" @click="submit">
        {{ submitting ? '正在提交…' : '提交段首帧并继续' }}
      </Button>
      <span v-if="submitted" role="status" class="muted">已提交，正在写本段尾帧词…</span>
    </div>
  </section>
</template>

<style scoped>
.segment-image-card {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  padding: var(--space-3);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-surface);
}
.segment-image-card h2 {
  margin: 0;
  font-size: 1rem;
}
.upload {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--space-2);
}
.actions {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}
.muted {
  margin: 0;
  color: var(--color-text-muted);
}
</style>
