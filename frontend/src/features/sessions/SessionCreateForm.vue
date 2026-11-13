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

// 导演台生成模式（D8 §4.1）。服务端 `brief_parser.DIRECTOR_MODES` 是唯一真源，
// 前端只是同一份四选一的界面镜像；v2v / rv2v / mixed 已取消（#170），不留墓碑。
const DIRECTOR_MODES = [
  { value: 't2v', label: '文生视频（t2v）' },
  { value: 'i2v', label: '图生视频（i2v）' },
  { value: 'fl2v', label: '首尾帧生视频（fl2v）' },
  { value: 'r2v', label: '参考素材生视频（r2v）' },
] as const
type DirectorMode = (typeof DIRECTOR_MODES)[number]['value']
// 段间引导模式的取值就是包内 `continuityMode`（`director/pack.py` 的
// `CONTINUITY_MODES` / `CONTINUITY_DEFAULT`）：省略时服务端落 guide。
const CONTINUITY_DEFAULT = 'guide'
const CONTINUITY_MODES = ['guide', 'continue'] as const
type ContinuityMode = (typeof CONTINUITY_MODES)[number]

const router = useRouter()

const form = reactive({
  plot: '',
  duration: DEFAULT_DURATION as number,
  // 路线：base = 首尾帧链，ref = 多参，director = 导演台。创建时定死，中途不切换。
  mode: 'base' as 'base' | 'ref' | 'director',
  variant: DEFAULT_VARIANT as 'T2VA' | 'FL2VA',
  style: 'Cinematic',
  language: 'Chinese',
  clarifications: '',
  draft: '',
  // 导演台三个打包参数（D8 §4.2）：只在 director 路线提交。生成模式无默认（四择一）。
  directorMode: '' as DirectorMode | '',
  directorNodeId: '',
  directorCacheName: '',
  directorContinuityMode: CONTINUITY_DEFAULT as ContinuityMode,
})

// ref 与 director 都不走帧链（ADR 0011 / 0024）：选它们时变体下拉整格隐藏。
// director 的 variant 由服务端固定落 T2VA 占位，ref 同理。
const isRef = computed(() => form.mode === 'ref')
const isDirector = computed(() => form.mode === 'director')
const hidesVariant = computed(() => isRef.value || isDirector.value)

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

// 导演台的两个必填项在客户端先拦：服务端给的是 422（director_mode_invalid /
// director_param_missing），但让人填完表再被打回来没有必要。
const directorError = computed(() => {
  if (!isDirector.value) {
    return null
  }
  if (!form.directorMode) {
    return '导演台请选择生成方式（t2v / i2v / fl2v / r2v）'
  }
  if (!form.directorNodeId.trim()) {
    return '导演台节点 id 必填：段缓存目录名由 <缓存名>_<节点id> 决定'
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
  if (plotMissing.value || durationError.value !== null || directorError.value !== null) {
    return
  }

  submitting.value = true
  creationKey ??= newIdempotencyKey()

  let sessionId: string
  try {
    const detail = await createSession(
      {
        mode: form.mode,
        plot: form.plot.trim(),
        duration: Number(form.duration),
        variant: hidesVariant.value ? 'T2VA' : form.variant,
        style: form.style.trim() || 'Cinematic',
        language: form.language.trim() || 'Chinese',
        clarifications: form.clarifications,
        draft: form.draft,
        // 非 director 路线一个导演台参数都不带：服务端会按 director_param_missing
        // 拒掉「带了却用不上」的那一份。
        ...(isDirector.value
          ? {
              director_mode: form.directorMode,
              director_node_id: form.directorNodeId.trim(),
              director_cache_name: form.directorCacheName.trim(),
              director_continuity_mode: form.directorContinuityMode,
            }
          : {}),
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

    <div class="field">
      <span class="field-label">路线</span>
      <div class="mode-options" role="radiogroup" aria-label="路线">
        <label>
          <input v-model="form.mode" type="radio" name="mode" value="base" />
          首尾帧（base）
        </label>
        <label>
          <input v-model="form.mode" type="radio" name="mode" value="ref" />
          多参参考素材（ref）
        </label>
        <label>
          <input v-model="form.mode" type="radio" name="mode" value="director" />
          导演台（director）
        </label>
      </div>
      <p v-if="isRef" class="field-hint">
        多参会话不选帧变体；建好后在素材分区上传参考素材（语义名必填）才能开始。
      </p>
      <p v-if="isDirector" class="field-hint">
        导演台走段循环；阶段 1 审查通过后先交开场首帧，再规划与打包。
      </p>
    </div>

    <div v-if="isDirector" class="field director-settings">
      <div class="field-row">
        <div class="field">
          <label for="session-director-mode">导演台生成方式</label>
          <select id="session-director-mode" v-model="form.directorMode" name="director_mode">
            <option value="" disabled>请选择（t2v / i2v / fl2v / r2v）</option>
            <option v-for="mode in DIRECTOR_MODES" :key="mode.value" :value="mode.value">
              {{ mode.label }}
            </option>
          </select>
        </div>

        <div class="field">
          <label for="session-director-node-id">导演台节点 id</label>
          <input
            id="session-director-node-id"
            v-model="form.directorNodeId"
            name="director_node_id"
            type="text"
            placeholder="导演台节点 ID"
          />
        </div>
      </div>

      <div class="field-row">
        <div class="field">
          <label for="session-director-cache">段缓存名（可空）</label>
          <input
            id="session-director-cache"
            v-model="form.directorCacheName"
            name="director_cache_name"
            type="text"
            placeholder="留空则只用节点 id"
          />
        </div>

        <div class="field">
          <label for="session-director-continuity">段间引导模式</label>
          <select
            id="session-director-continuity"
            v-model="form.directorContinuityMode"
            name="director_continuity_mode"
          >
            <option v-for="mode in CONTINUITY_MODES" :key="mode" :value="mode">
              {{ mode }}
            </option>
          </select>
        </div>
      </div>

      <p v-if="directorError" class="field-error">{{ directorError }}</p>
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

      <!-- ref / director 都不走帧链：变体是帧形态，对这一格没有意义。 -->
      <div v-if="!hidesVariant" class="field">
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
.field-label {
  font-weight: 600;
}
.mode-options {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-3);
}
.mode-options label {
  display: flex;
  align-items: center;
  gap: var(--space-1);
}
.field-hint {
  margin: 0;
  color: var(--color-text-muted);
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
