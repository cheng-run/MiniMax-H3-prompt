<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'

import { ApiError, createSession, listClarificationQuestions, startSessionJob } from '@/api/client'
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

// 起步澄清（#339 / spec #332 E1）：提问发生在**建会话之前**——Brief 只有创建时那一处
// 构造口，建完再问要么新开存储、要么绕开起跑前的副本变换。所以点「创建」先拉问题，
// 在表单内联展开问答面板，答案随创建请求以结构化问答对提交。
//
// 面板状态机：idle（还没问）→ asking（问题在眼前，等用户答或跳过）／failed（生成失败，
// 换一条降级出路：直接建会话）；拉取期由 submitting 表达，不另设一档。
type ClarificationStage = 'idle' | 'asking' | 'failed'
const clarificationStage = ref<ClarificationStage>('idle')
const clarificationQuestions = ref<string[]>([])
// 回答与问题一一对应（下标即问题序号）：按序号渲染输入框，别用问题文本当键。
const clarificationAnswers = ref<string[]>([])
// 生成失败是**不阻断**的一行提示：它不是错误，用户下一步照常能建会话。
const clarificationNotice = ref<string | null>(null)
// 创建进行中的阶段只影响按钮文案：拉问题与建会话是两次等待，不能让用户以为卡住了。
const createStage = ref<'questions' | 'creating'>('creating')

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

  // 面板已经问过（答或跳过）就直接提交：再次点「创建」不该把问题重问一遍，也不该
  // 把用户已经填好的回答冲掉。
  if (clarificationStage.value === 'asking') {
    await createWithClarifications(collectPairs())
    return
  }
  // 生成失败过一次：不再重试拉问题（用户可能本来就想无问题开始），直接建会话。
  if (clarificationStage.value === 'failed') {
    await createWithClarifications([])
    return
  }

  submitting.value = true
  createStage.value = 'questions'
  let questions: string[]
  try {
    questions = await listClarificationQuestions(form.plot.trim())
  } catch (error) {
    // 生成失败不阻断创建（spec US4）：显示一行提示，把出口留在同一个按钮上。
    // 只有 502 那一类算「这一次拿不到问题」；其余错误（鉴权、网络）仍按提交失败说话。
    const status = error instanceof ApiError ? error.status : 0
    if (status === 502 || status === 0) {
      clarificationStage.value = 'failed'
      clarificationNotice.value = '澄清问题生成失败，可直接开始'
      submitting.value = false
      return
    }
    submitError.value = errorMessage(error)
    submitting.value = false
    return
  }

  // 拉取这一段结束了：无论下一步是展开面板还是直接建会话，都要先放开 loading，
  // 否则「创建」那条自身的重入守卫会把紧接着的建会话挡回去。
  submitting.value = false

  if (questions.length === 0) {
    // 没有问题＝这个主题不需要澄清（端点 0 条上限时也一样）：不展开面板，直接建。
    clarificationStage.value = 'idle'
    await createWithClarifications([])
    return
  }

  // 拉到问题了：展开面板，交出「答或跳过」的决定权。此时**还不建会话**。
  clarificationQuestions.value = questions
  clarificationAnswers.value = questions.map(() => '')
  clarificationStage.value = 'asking'
}

/** 面板上的答案按「有回答的条目」收成结构化问答对；空回答＝没澄清，服务端也会滤掉。 */
function collectPairs(): { question: string; answer: string }[] {
  return clarificationQuestions.value
    .map((question, index) => ({
      question,
      answer: (clarificationAnswers.value[index] ?? '').trim(),
    }))
    .filter((pair) => pair.answer.length > 0)
}

// 跳过＝一条问答对都不交。答过的内容也不带走（「跳过」就是全部跳过，不是「提交部分」）。
function skipClarifications(): void {
  if (submitting.value) {
    return
  }
  void createWithClarifications([])
}

async function submitClarifications(): Promise<void> {
  if (submitting.value) {
    return
  }
  const pairs = collectPairs()
  // 自由文本与问答对同时有内容，服务端会 422 `clarification_conflict`。前端先在本地
  // 拦下并说清楚：用户不必等一次往返才知道「二选一」。
  if (pairs.length > 0 && form.clarifications.trim().length > 0) {
    submitError.value = '「补充说明」与逐条作答不能同时提交：两者都写进会话的澄清块，请二选一。'
    return
  }
  await createWithClarifications(pairs)
}

async function createWithClarifications(
  pairs: { question: string; answer: string }[],
): Promise<void> {
  if (submitting.value) {
    return
  }
  submitting.value = true
  createStage.value = 'creating'
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
        // 结构化问答对（#338）：渲染成澄清块是**后端**的事，前端不拼「1. Q → A」——
        // 免得同一格式两处实现。没有问答对时整个键都不带（空表与省略同义）。
        ...(pairs.length > 0 ? { clarification_pairs: pairs } : {}),
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
        多参会话不选帧变体；零素材就能开始——阶段 1 会列出本片需要的参考素材，建好后在
        素材循环的待办卡上逐件交回（系统读图校对，通过才定稿）。
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

    <!-- 起步澄清面板（#339）：拉到问题后内联展开，逐条作答或一键跳过。放在提交按钮
         之前——视觉顺序与动作顺序一致：先答（或跳），再创建。 -->
    <section
      v-if="clarificationStage === 'asking'"
      class="clarification"
      data-panel="clarification"
      aria-label="起步澄清"
    >
      <h3>起步澄清（{{ clarificationQuestions.length }} 条）</h3>
      <p class="field-hint">逐条作答会随会话一起提交；没想好的可以留空，或直接跳过全部。</p>
      <div
        v-for="(question, index) in clarificationQuestions"
        :key="index"
        class="field clarification-item"
      >
        <label :for="`clarification-answer-${index}`">{{ question }}</label>
        <textarea
          :id="`clarification-answer-${index}`"
          v-model="clarificationAnswers[index]"
          :name="`clarification-answer-${index}`"
          rows="2"
        />
      </div>
      <div class="clarification-actions">
        <Button
          variant="secondary"
          data-action="clarification-skip"
          :disabled="submitting"
          @click="skipClarifications"
        >
          跳过
        </Button>
        <Button
          data-action="clarification-submit"
          :disabled="submitting"
          @click="submitClarifications"
        >
          继续创建
        </Button>
      </div>
    </section>
    <!-- 生成失败／跳过后的出口仍挂在同一个提交按钮上；这一行只是让用户知道
         「没有问到问题」不是他漏做了什么。 -->
    <p v-else-if="clarificationNotice" class="field-hint" data-notice="clarification">
      {{ clarificationNotice }}
    </p>

    <div class="actions">
      <Button type="submit" :disabled="submitting">
        {{
          submitting
            ? createStage === 'questions'
              ? '正在获取澄清问题…'
              : '正在创建…'
            : '创建并开始'
        }}
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
/* 澄清面板纵向堆叠：每条问题一行输入，窄屏不换列、不引入横向滚动。内层不进
   .field-row（那是给并排字段的 flex-wrap）。 */
.clarification {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-3);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-surface);
}
.clarification h3 {
  font-size: 1rem;
}
.clarification-item label {
  font-weight: 600;
  overflow-wrap: anywhere;
}
.clarification-actions {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
  justify-content: flex-end;
}
.clarification-actions > * {
  flex: 0 1 auto;
}
</style>
