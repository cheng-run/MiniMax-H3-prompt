<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'

import { ApiError, createSession, startSessionJob } from '@/api/client'
import Button from '@/components/ui/Button.vue'
import ErrorNotice from '@/components/ui/ErrorNotice.vue'

import { newIdempotencyKey } from './keys'

// 新建会话表单（spec §5）：字段与默认值在这里显式给出——服务端 SessionCreate 的缺省
// 仍是 duration=4.0 / variant=T2VA，前端必须自己发送 5 / FL2VA，不能依赖服务端缺省。
// 时长范围与服务端同一条产品规则（D6-AC1）：4–60 秒，越过 10 秒的会话走长片分段。
const DURATION_MIN = 4
const DURATION_MAX = 60
const DEFAULT_DURATION = 5
const DEFAULT_VARIANT = 'FL2VA'

const router = useRouter()

const form = reactive({
  plot: '',
  duration: DEFAULT_DURATION as number,
  variant: DEFAULT_VARIANT as 'T2VA' | 'FL2VA',
  style: 'Cinematic',
  language: 'Chinese',
  clarifications: '',
  draft: '',
})

const submitting = ref(false)
const plotMissing = ref(false)
const submitError = ref<string | null>(null)

// 时长两端都限；越界即时提示，不用等提交后才由服务端 422 回来。
const durationError = computed(() => {
  const value = Number(form.duration)
  if (!Number.isFinite(value)) {
    return '时长需为数字'
  }
  if (value < DURATION_MIN || value > DURATION_MAX) {
    return `时长需在 ${DURATION_MIN}–${DURATION_MAX} 秒之间`
  }
  return null
})

// 幂等键按「一次逻辑提交」复用：创建失败后重试仍带同一个键，服务端只会建出一条会话
// （双击防重的服务端半边）。创建成功即清空，下一次新建是新的键。
let creationKey: string | null = null

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    return error.problem?.detail ?? error.message
  }
  return '创建失败，请稍后重试。'
}

async function onSubmit(): Promise<void> {
  if (submitting.value) {
    return
  }
  submitError.value = null
  plotMissing.value = form.plot.trim().length === 0
  if (plotMissing.value || durationError.value !== null) {
    return
  }

  submitting.value = true
  creationKey ??= newIdempotencyKey()

  let sessionId: string
  try {
    const detail = await createSession(
      {
        mode: 'base',
        plot: form.plot.trim(),
        duration: Number(form.duration),
        variant: form.variant,
        style: form.style.trim() || 'Cinematic',
        language: form.language.trim() || 'Chinese',
        clarifications: form.clarifications,
        draft: form.draft,
      },
      creationKey,
    )
    sessionId = detail.session_id
  } catch (error) {
    // 会话没建出来：留在表单上，保留幂等键供重试。
    submitError.value = errorMessage(error)
    submitting.value = false
    return
  }

  // 从这里起会话已经存在：无论如何都进工作台，不能把用户留在表单上再建一条。
  creationKey = null
  try {
    await startSessionJob(sessionId, newIdempotencyKey())
  } catch (error) {
    // 起跑失败：会话停在 created，详情页有「开始」兜底，刷新不丢。
    console.warn('自动起跑失败，会话停在 created，可在工作台点「开始」', error)
  }

  submitting.value = false
  await router.push(`/sessions/${sessionId}`)
}
</script>

<template>
  <form class="create-form" novalidate @submit.prevent="onSubmit">
    <h2>新建会话</h2>

    <ErrorNotice v-if="submitError" :message="submitError" />
    <ErrorNotice v-if="plotMissing" message="请填写剧情。" />

    <div class="field">
      <label for="session-plot">剧情（必填）</label>
      <textarea
        id="session-plot"
        v-model="form.plot"
        name="plot"
        rows="4"
        :aria-invalid="plotMissing"
        placeholder="描述要生成的故事或场景"
      />
    </div>

    <div class="field-row">
      <div class="field">
        <label for="session-duration">时长（秒，4–10）</label>
        <input
          id="session-duration"
          v-model.number="form.duration"
          name="duration"
          type="number"
          :min="DURATION_MIN"
          :max="DURATION_MAX"
          step="1"
          :aria-invalid="durationError !== null"
          :aria-describedby="durationError ? 'session-duration-error' : undefined"
        />
        <p v-if="durationError" id="session-duration-error" class="field-error">
          {{ durationError }}
        </p>
      </div>

      <div class="field">
        <label for="session-variant">生成方式</label>
        <select id="session-variant" v-model="form.variant" name="variant">
          <option value="T2VA">T2VA（纯文字）</option>
          <option value="FL2VA">FL2VA（首尾帧）</option>
        </select>
      </div>
    </div>

    <div class="field-row">
      <div class="field">
        <label for="session-style">风格</label>
        <input id="session-style" v-model="form.style" name="style" type="text" />
      </div>

      <div class="field">
        <label for="session-language">语言</label>
        <input id="session-language" v-model="form.language" name="language" type="text" />
      </div>
    </div>

    <details class="advanced">
      <summary>高级选项</summary>
      <div class="field">
        <label for="session-clarifications">补充说明</label>
        <textarea
          id="session-clarifications"
          v-model="form.clarifications"
          name="clarifications"
          rows="2"
        />
      </div>
      <div class="field">
        <label for="session-draft">草稿</label>
        <textarea id="session-draft" v-model="form.draft" name="draft" rows="2" />
      </div>
    </details>

    <div class="actions">
      <Button type="submit" :disabled="submitting">
        {{ submitting ? '正在创建…' : '创建并开始' }}
      </Button>
    </div>
  </form>
</template>

<style scoped>
.create-form {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  max-width: 720px;
  padding: var(--space-3);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
}
.field {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}
.field-row {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-3);
}
.field-row .field {
  flex: 1 1 200px;
}
.field-error {
  margin: 0;
  color: var(--color-error-text);
  font-size: 0.875rem;
}
.advanced {
  border-top: 1px solid var(--color-border);
  padding-top: var(--space-2);
}
.advanced summary {
  cursor: pointer;
  color: var(--color-text-muted);
}
.advanced .field {
  margin-top: var(--space-2);
}
.actions {
  display: flex;
  justify-content: flex-end;
}
</style>
