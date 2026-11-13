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

// 导演台开场首帧交接卡（D8 §4.4 / §7）：阶段 1 审查通过后出现的待办。
//
// **一个待办收两张图**：开场首帧必填、全片尾帧可选——前者是第 1 段的源图（i2v 的
// 每一段都必须有源图），后者是全片终点画面（落 `director/assets/film-end.*`，不是
// 第 1 段的终点）。两张都必须是本会话已上传的图片素材，不接受本地路径。
//
// 画幅不符**不在这里自动放行**：服务端 409 `aspect_mismatch_unconfirmed` 会带出
// 「实际比例 vs 画布比例」的原文，用户勾选确认后才重发 `confirm_aspect_mismatch: true`。
// 默认带 true 等于把这道闸门删掉。

const props = defineProps<{
  sessionId: string
  item: ActionItemOut
  assets: Asset[]
}>()

const emit = defineEmits<{ submitted: []; uploaded: [] }>()

// 图槽只接受图片素材（服务端 resolve_image_asset 会拒绝视频/音频），选择器同理。
// 卡内刚上传的素材还没进父组件的快照列表，先并进来，否则选中值没有对应选项。
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

const startAsset = ref('')
const endAsset = ref('')
const uploading = ref<'' | 'start' | 'end'>('')
const uploadError = ref<string | null>(null)

const submitting = ref(false)
const submitError = ref<string | null>(null)
// 首帧必填的客户端提示：不让人点完按钮才被服务端 422 打回来。
const missingStart = ref(false)
// 409 之后才出现的画幅警告与确认勾选。
const aspectWarning = ref<string | null>(null)
const aspectConfirmed = ref(false)
const submitted = ref(false)

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    return error.problem?.detail ?? error.message
  }
  return '提交失败，请稍后重试。'
}

async function onUpload(slot: 'start' | 'end', event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file || uploading.value) {
    return
  }
  uploading.value = slot
  uploadError.value = null
  try {
    // 不带语义名：语义名是 ref 的台账登记口，导演台会话带它会被 422（asset_name_invalid）。
    const asset = await uploadSessionAsset(props.sessionId, file)
    extraAssets.value = [...extraAssets.value, asset]
    if (slot === 'start') {
      startAsset.value = asset.asset_id
    } else {
      endAsset.value = asset.asset_id
    }
    emit('uploaded')
  } catch (error) {
    uploadError.value = errorMessage(error)
  } finally {
    uploading.value = ''
  }
}

function requestSubmit(): void {
  missingStart.value = false
  if (!startAsset.value) {
    missingStart.value = true
    return
  }
  void submit(false)
}

async function confirmAspect(): Promise<void> {
  if (!aspectConfirmed.value) {
    return
  }
  await submit(true)
}

async function submit(confirmAspectMismatch: boolean): Promise<void> {
  if (submitting.value) {
    return
  }
  submitting.value = true
  submitError.value = null
  try {
    await resolveActionItem(props.sessionId, props.item.item_id, {
      decision: 'submit_start_image',
      start_asset_id: startAsset.value,
      // 尾帧可选：不选就**不出现**这个键，服务端据此不写 film-end。
      ...(endAsset.value ? { end_asset_id: endAsset.value } : {}),
      // 只有用户在 409 之后勾选过才带 true。
      ...(confirmAspectMismatch ? { confirm_aspect_mismatch: true } : {}),
    })
    submitted.value = true
    aspectWarning.value = null
    emit('submitted')
  } catch (error) {
    if (error instanceof ApiError && error.problem?.code === 'aspect_mismatch_unconfirmed') {
      aspectWarning.value = error.problem.detail
      aspectConfirmed.value = false
    } else {
      submitError.value = errorMessage(error)
    }
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <section class="start-card">
    <h2>提交开场首帧</h2>
    <p class="muted">
      审查已通过。请为导演台选好开场首帧（必填）与全片尾帧（可选）；两张都取自本会话的图片素材。
    </p>

    <div class="slots">
      <div class="slot">
        <label for="director-start-asset">开场首帧（必填）</label>
        <select
          id="director-start-asset"
          v-model="startAsset"
          name="director-start-asset"
          :disabled="submitting"
        >
          <option value="">（不选）</option>
          <option v-for="asset in imageAssets" :key="asset.asset_id" :value="asset.asset_id">
            {{ asset.name }}
          </option>
        </select>
        <label class="upload-label" for="director-start-upload">上传首帧</label>
        <input
          id="director-start-upload"
          name="start-frame-upload"
          type="file"
          accept="image/*"
          :disabled="uploading !== '' || submitting"
          @change="onUpload('start', $event)"
        />
      </div>

      <div class="slot">
        <label for="director-film-end-asset">全片尾帧（可选）</label>
        <select
          id="director-film-end-asset"
          v-model="endAsset"
          name="director-film-end-asset"
          :disabled="submitting"
        >
          <option value="">（不选）</option>
          <option v-for="asset in imageAssets" :key="asset.asset_id" :value="asset.asset_id">
            {{ asset.name }}
          </option>
        </select>
        <label class="upload-label" for="director-film-end-upload">上传尾帧</label>
        <input
          id="director-film-end-upload"
          name="film-end-upload"
          type="file"
          accept="image/*"
          :disabled="uploading !== '' || submitting"
          @change="onUpload('end', $event)"
        />
      </div>
    </div>

    <p v-if="uploading" role="status" class="muted">正在上传…</p>
    <p v-if="!imageAssets.length" class="muted">
      本会话还没有图片素材，可直接在上方上传，或到「素材」分区上传。
    </p>
    <ErrorNotice v-if="uploadError" :message="uploadError" />
    <ErrorNotice
      v-if="missingStart"
      message="请先选择或上传开场首帧：i2v / fl2v 没有源图跑不了。"
    />
    <ErrorNotice v-if="submitError" :message="submitError" />

    <div v-if="aspectWarning" class="aspect">
      <h3>画幅与成片不一致</h3>
      <pre>{{ aspectWarning }}</pre>
      <label class="aspect__confirm">
        <input v-model="aspectConfirmed" name="confirm-aspect-mismatch" type="checkbox" />
        我确认用这个比例继续（锚点可能被裁切）
      </label>
      <div class="actions">
        <Button :disabled="submitting || !aspectConfirmed" @click="confirmAspect">
          {{ submitting ? '正在提交…' : '确认比例并提交' }}
        </Button>
      </div>
    </div>
    <div v-else class="actions">
      <Button :disabled="submitting" @click="requestSubmit">
        {{ submitting ? '正在提交…' : '提交开场首帧并继续' }}
      </Button>
    </div>

    <p v-if="submitted" role="status" class="muted">已提交，正在进入导演台规划…</p>
  </section>
</template>

<style scoped>
.start-card {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  padding: var(--space-3);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-surface);
}
.start-card h2 {
  margin: 0;
  font-size: 1.125rem;
}
.start-card h3 {
  margin: 0;
  font-size: 0.95rem;
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
  min-width: 220px;
}
.upload-label {
  color: var(--color-text-muted);
  font-size: 0.875rem;
}
.aspect {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-2);
  border: 1px solid var(--color-error-text);
  border-radius: var(--radius-md);
  background: var(--color-error-bg);
}
.aspect pre {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 0.875rem;
}
.aspect__confirm {
  display: flex;
  align-items: center;
  gap: var(--space-1);
}
.actions {
  display: flex;
  gap: var(--space-2);
}
.muted {
  color: var(--color-text-muted);
}
</style>
