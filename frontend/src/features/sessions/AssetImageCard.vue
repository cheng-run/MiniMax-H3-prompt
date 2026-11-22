<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import {
  ApiError,
  getArtifactContent,
  getSession,
  resolveActionItem,
  uploadSessionAsset,
  type ActionItemOut,
  type ArtifactInfo,
} from '@/api/client'
import type { RefOut } from '@/api/client'
import Button from '@/components/ui/Button.vue'
import ErrorNotice from '@/components/ui/ErrorNotice.vue'
import MarkdownBlock from '@/components/ui/MarkdownBlock.vue'

import { assetPromptName } from './artifactKinds'
import { newIdempotencyKey } from './keys'
import { useClipboardCopy } from './useClipboardCopy'

// 素材待办卡（D7 spec §4.4 / #256 / #286 / #289）：第 N 件 → 词全文（可复制）→ 上传
// 生成结果。校对不通过时给**四条**出路（两掷两定），任何一条都让这一件往前走；
// 「放弃这一件」是另一个动作，不属于这四选。
// 一次只显示一件（系统严格串行），卡片随快照换件。
const props = defineProps<{
  sessionId: string
  item: ActionItemOut
  refs: RefOut[]
  artifacts: ArtifactInfo[]
}>()

const emit = defineEmits<{ submitted: [] }>()

const number = computed(() => props.item.asset_index ?? 0)
const pictureRefs = computed(() =>
  props.refs.filter((ref) => ref.kind === 'picture').sort((a, b) => a.number - b.number),
)
const semanticName = computed(
  () => pictureRefs.value.find((ref) => ref.number === number.value)?.name ?? '未命名',
)
// 本件的生图档位（#292）：显示名由服务端按**全仓唯一一份映射**渲染（`image_model_label`），
// 这里只负责显示，不另写一份名字表。旧台账行没有这个字段时显示「未知」——不编造一档，
// 用户才敢照着它去挑 ComfyUI 节点。
const imageModelLabel = computed(
  () => pictureRefs.value.find((ref) => ref.number === number.value)?.image_model_label || '未知',
)
// 按状态数，不按编号比：有了 `abandoned` 之后，「编号小于当前」里可能夹着放弃件，
// 把放弃件念成「已定稿」等于替用户宣布一张不存在的图已经交了。
const passedRefs = computed(() => pictureRefs.value.filter((ref) => ref.status === 'passed'))
const abandonedRefs = computed(() => pictureRefs.value.filter((ref) => ref.status === 'abandoned'))

const artifactName = computed(() => assetPromptName(number.value))
const promptArtifact = computed(
  () => props.artifacts.find((artifact) => artifact.name === artifactName.value) ?? null,
)

const prompt = ref<string | null>(null)
const promptError = ref<string | null>(null)
const { label: copyLabel, copy } = useClipboardCopy('复制词')

let loadGeneration = 0
async function loadPrompt(): Promise<void> {
  const generation = ++loadGeneration
  promptError.value = null
  const artifact = promptArtifact.value
  if (!artifact) {
    prompt.value = null
    return
  }
  try {
    const text = await getArtifactContent(artifact.artifact_id)
    if (generation === loadGeneration) {
      prompt.value = text
    }
  } catch {
    if (generation === loadGeneration) {
      prompt.value = null
      promptError.value = '词读取失败，请刷新重试。'
    }
  }
}

// 产物 id 不变、正文会变（不通过时改写覆盖同一份文件），所以按 created_at（mtime）
// 与待办 id 一起判「内容可能变了」。
watch(
  () => [promptArtifact.value?.created_at ?? '', props.item.item_id],
  () => void loadPrompt(),
  { immediate: true },
)

const selectedFile = ref<File | null>(null)
const submitting = ref(false)
const submitError = ref<string | null>(null)
const submitted = ref(false)

// 校对不通过后的四条出路（#288 前两条 / #289 后两条）。前两条是**重掷**：这一件退回
// 未交图、同一件号重开卡片；后两条是**定稿**：这一件当场落定，进下一件。
//
// 默认仍是「改词强化原意再掷」——它只做加法，方向是让设计被画对。四条都不带
// ``asset_id``：选的是「下一轮怎么来」或「这一件怎么定」，不是又交了一张图。
type AssetExit = 'reword_reroll' | 'reroll' | 'accept_image_facts' | 'keep_description'

const rerolling = ref<AssetExit | null>(null)
const rerollError = ref<string | null>(null)
// 本次校对的补漏条数（#289）：卡片要在人做选择之前说清「选这两条会并入几条」，
// 因为并入发生在定稿那一刻，而定稿之后这张卡就不再出现。
const omissionCount = computed(() => props.item.proofread?.omissions?.length ?? 0)

async function chooseExit(decision: AssetExit): Promise<void> {
  if (rerolling.value) {
    return
  }
  rerolling.value = decision
  rerollError.value = null
  try {
    // 不带 asset_id：这次决议是「下一轮怎么来」，不是又交了一张图。改词在后台作业里跑，
    // 请求立即返回；快照随后会把这张卡换成同件号的新卡（或重开同一张）。
    await resolveActionItem(props.sessionId, props.item.item_id, { decision }, newIdempotencyKey())
    emit('submitted')
  } catch (error) {
    rerollError.value = errorMessage(error, '提交失败，请稍后重试。')
  } finally {
    rerolling.value = null
  }
}

// 恢复阶段 1 原文（#291）：被旧逻辑改坏的词可以从阶段 1 原文救回来。服务端只在
// 「未定稿 ＋ 盘上词与原文不一致 ＋ 轮次账里没有这一件的记录」时给这个动作——
// 有账＝合法改写，不该一键抹掉。它不属于校对四选：四选回答「下一轮怎么来」，
// 它回答「这一版词本来就不该被改」。
const restoring = ref(false)
const restoreError = ref<string | null>(null)

async function restore(): Promise<void> {
  if (restoring.value) {
    return
  }
  restoring.value = true
  restoreError.value = null
  try {
    // 不带 asset_id：它只把盘上的词改回阶段 1 那一份，不认图。覆盖写盘在后台作业里，
    // 完成后这张卡会重开（旧的差异报告属于被改坏的那版词，不再适用）。
    await resolveActionItem(
      props.sessionId,
      props.item.item_id,
      { decision: 'restore_prompt' },
      newIdempotencyKey(),
    )
    emit('submitted')
  } catch (error) {
    restoreError.value = errorMessage(error, '恢复失败，请稍后重试。')
  } finally {
    restoring.value = false
  }
}

// 放弃这一件（#279 US9 / #282）：模型怎么都画不出来时的出口。代价只有一句，但必须
// 在点下去之前看得到——所以先展开确认，不点确认不发请求。
const confirmingAbandon = ref(false)
const abandoning = ref(false)
const abandonError = ref<string | null>(null)
const abandonNote = ref<string | null>(null)

// 与服务端 ``_abandon_asset_note`` 同一句代价（服务端把它写进已决议待办的 reason
// 与事件流）：放弃的是图，不是那些文字。确认前用现在时说明「会发生什么」，
// 成功后的回执才用服务端那句完成时。
const abandonCost = computed(
  () =>
    `放弃的是「${semanticName.value}」这一件图，不是那些文字：分镜表与段词里对` +
    `「${semanticName.value}」的指称会变成无图指称（照抄接线时这一格没有参考图可填）。` +
    '已经定稿的文本不会被自动改写。',
)
// 决议响应是 ``JobSummary``，不带这句话；回执优先取服务端写进已决议待办的 ``reason``
// （与事件流里那句同源），取不到才退回下面这份镜像——两份各写一遍时，改了一处就会
// 让「界面上说的代价」与「事件里记的代价」慢慢分家。
const abandonReceipt = computed(
  () =>
    `已放弃「${semanticName.value}」这一件：分镜表与段词里对「${semanticName.value}」` +
    '的指称会变成无图指称（照抄接线时这一格没有参考图可填）。' +
    '已经定稿的文本不会被自动改写。',
)

/** 已决议待办上的服务端原句；读不到（网络/刷新失败）时返回 null，由调用方兜底。 */
async function serverAbandonReason(): Promise<string | null> {
  try {
    const detail = await getSession(props.sessionId)
    const resolved = (detail.action_items ?? []).find(
      (entry) => entry.item_id === props.item.item_id,
    )
    return resolved?.reason?.trim() || null
  } catch {
    return null
  }
}

// 卡片随快照换件：上一件的回执与确认态不能带到下一件上。
watch(
  () => props.item.item_id,
  () => {
    confirmingAbandon.value = false
    abandonNote.value = null
    abandonError.value = null
  },
)

function onFileChange(event: Event): void {
  const input = event.target as HTMLInputElement
  selectedFile.value = input.files?.[0] ?? null
}

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof ApiError ? (error.problem?.detail ?? error.message) : fallback
}

async function submit(): Promise<void> {
  const file = selectedFile.value
  if (!file || submitting.value) {
    return
  }
  submitting.value = true
  submitError.value = null
  submitted.value = false
  try {
    // 先上传（不带语义名：生成结果不是参考素材，不进台账），再把返回的素材标识
    // 当作这次的决议载荷——待办的唯一决议就是「交哪张图」。
    const asset = await uploadSessionAsset(props.sessionId, file)
    await resolveActionItem(
      props.sessionId,
      props.item.item_id,
      { asset_id: asset.asset_id },
      newIdempotencyKey(),
    )
    submitted.value = true
    emit('submitted')
  } catch (error) {
    submitError.value = errorMessage(error, '提交失败，请稍后重试。')
  } finally {
    submitting.value = false
  }
}

async function confirmAbandon(): Promise<void> {
  if (abandoning.value) {
    return
  }
  abandoning.value = true
  abandonError.value = null
  try {
    // 不带 asset_id：放弃与交图是互斥的两种决议（服务端也会拒两者同时提交）。
    await resolveActionItem(
      props.sessionId,
      props.item.item_id,
      { decision: 'abandon_asset' },
      newIdempotencyKey(),
    )
    abandonNote.value = (await serverAbandonReason()) ?? abandonReceipt.value
    confirmingAbandon.value = false
    emit('submitted')
  } catch (error) {
    abandonError.value = errorMessage(error, '放弃失败，请稍后重试。')
  } finally {
    abandoning.value = false
  }
}
</script>

<template>
  <section class="asset-card">
    <h2>第 {{ number }} 件素材图 · {{ semanticName }}</h2>
    <!-- 生图档位（#292）：这条词按哪一档的纪律写成，用户据此知道贴进哪个 ComfyUI 节点。 -->
    <p class="muted" data-testid="asset-image-model">生图档：{{ imageModelLabel }}</p>
    <p class="muted">
      已定稿 {{ passedRefs.length }} / 共 {{ pictureRefs.length }} 件<template
        v-if="abandonedRefs.length"
        >（其中已放弃 {{ abandonedRefs.length }} 件）</template
      >。
      <template v-if="passedRefs.length">
        已定稿：{{ passedRefs.map((ref) => ref.name).join('、') }}。
      </template>
    </p>
    <!-- 同一件号重开的两种情况：第一轮还没交（没有差异）与校对没过、等你重交。
         后者在这一格说清楚，用户不会以为「刚交的那张已经算数」。 -->
    <p v-if="item.reason" class="muted">
      这一件上一轮没通过校对：在下面的出路上选一条，再按卡片上的词重新生成并交回。
    </p>

    <div class="prompt">
      <div class="prompt__head">
        <h3>
          资产图词<span class="file-name">{{ artifactName }}</span>
        </h3>
        <Button
          variant="secondary"
          :disabled="prompt === null"
          @click="prompt !== null && copy(prompt)"
        >
          {{ copyLabel }}
        </Button>
      </div>
      <p v-if="promptError" class="field-error">{{ promptError }}</p>
      <pre v-else-if="prompt === null" class="muted">词加载中…</pre>
      <MarkdownBlock v-else class="prompt__text" :text="prompt" />
    </div>

    <!-- 恢复阶段 1 原文（#291）：只在「无轮次账记录的差异」上出现，所以放在校对四选
         之外——它不是一条出路，是把被改坏的词救回去。diff 由服务端算好带下来。 -->
    <section v-if="item.restore_available" class="restore" aria-label="恢复阶段 1 原文">
      <h3>盘上的生图词与阶段 1 原文不一致，且没有改写记录</h3>
      <p class="muted">
        这一版词不是任何一次改写留下的（资产轮次账里没有这一件的记录），可能是旧逻辑
        改坏的。可以把它整篇恢复成阶段 1 写好的那一份——台账状态不变，也不花模型调用。
      </p>
      <pre class="diff__text">{{ item.restore_diff }}</pre>
      <div class="actions">
        <Button
          type="button"
          variant="secondary"
          data-testid="asset-restore"
          :disabled="restoring || submitting || abandoning"
          @click="restore"
        >
          {{ restoring ? '正在恢复…' : '恢复阶段 1 原文' }}
        </Button>
      </div>
      <ErrorNotice v-if="restoreError" :message="restoreError" />
    </section>

    <section v-if="item.reason" class="diff" aria-label="校对差异">
      <h3>校对结论：不通过</h3>
      <pre class="diff__text">{{ item.reason }}</pre>
      <!-- 本次读图是哪个视觉模型给的结论（#292）：换过档之后，用户要判得了这条结论该不该信。 -->
      <p class="muted" data-testid="proofread-vision-model">
        读图模型：{{ item.vision_model || '未知' }}
      </p>
      <!-- 两条出路并列可见（#288）：默认是改词强化原意——它只做加法，让设计被画对；
           原词再掷是「字一个都不动」的那一档。 -->
      <div class="exits" aria-label="校对不通过后的出路">
        <p class="muted">下一轮怎么来：</p>
        <Button
          type="button"
          :disabled="rerolling !== null || submitting || abandoning"
          @click="chooseExit('reword_reroll')"
        >
          {{ rerolling === 'reword_reroll' ? '正在按偏差改词…' : '改词强化原意再掷（默认）' }}
        </Button>
        <p class="muted exits__hint">
          只做加法：在原词上把没画出来的细节写得更显式，锁死清单里的每一条都逐字保留。
          改词在后台作业里跑，完成后这张卡会带着新词重开。
        </p>
        <Button
          type="button"
          variant="secondary"
          :disabled="rerolling !== null || submitting || abandoning"
          @click="chooseExit('reroll')"
        >
          {{ rerolling === 'reroll' ? '正在重开…' : '原词再掷' }}
        </Button>
        <p class="muted exits__hint">
          原词再掷：生图词与描述一个字都不动，只把这一件的交图卡重开，用同一份词再生成一张。
        </p>
        <!-- 定稿的两条（#289）：文本落点是**素材画面事实**（写段词时读的那份描述），
             生图词逐字不动——词是指令、图是既成事实。 -->
        <Button
          type="button"
          variant="secondary"
          :disabled="rerolling !== null || submitting || abandoning"
          @click="chooseExit('accept_image_facts')"
        >
          {{ rerolling === 'accept_image_facts' ? '正在按图改文案…' : '按图改文案（只改描述）' }}
        </Button>
        <p class="muted exits__hint">
          按图改文案：把描述里与图不符的那几处改成图里的实见，补漏一并并入素材画面事实；
          <strong>生图词逐字不动</strong>，这一件当场定稿。
        </p>
        <Button
          type="button"
          variant="secondary"
          :disabled="rerolling !== null || submitting || abandoning"
          @click="chooseExit('keep_description')"
        >
          {{ rerolling === 'keep_description' ? '正在保留…' : '明知故犯保留原描述' }}
        </Button>
        <p class="muted exits__hint">
          保留原描述：偏差一处不改，只把这轮的补漏并入素材画面事实；生图词逐字不动，
          这一件当场定稿。
        </p>
        <p v-if="omissionCount" class="muted exits__hint" data-testid="asset-facts-count">
          本次校对的 {{ omissionCount }} 条补漏会在定稿时按图补入描述（同一句不会重复并入）。
        </p>
        <ErrorNotice v-if="rerollError" :message="rerollError" />
      </div>
    </section>

    <form class="upload" @submit.prevent="submit">
      <label for="asset-image-file">上传这一件生成的图</label>
      <input
        id="asset-image-file"
        name="asset-image-file"
        type="file"
        accept="image/*"
        :disabled="submitting"
        @change="onFileChange"
      />
      <ErrorNotice v-if="submitError" :message="submitError" />
      <div class="actions">
        <Button type="submit" :disabled="submitting || !selectedFile">
          {{ submitting ? '正在提交…' : '提交并校对' }}
        </Button>
      </div>
    </form>

    <p v-if="submitted" role="status" class="muted">已提交，正在读图校对…</p>

    <!-- 放弃这一件（#279 US9）：模型怎么都画不出来时的出口。代价放在确认里，
         点「放弃这一件」之前不铺开，但确认按钮旁边一定看得见。 -->
    <section class="abandon" aria-label="放弃这一件">
      <Button
        v-if="!confirmingAbandon"
        type="button"
        variant="secondary"
        :disabled="submitting || abandoning"
        @click="confirmingAbandon = true"
      >
        放弃这一件
      </Button>
      <div v-else class="abandon__confirm">
        <p class="abandon__cost">{{ abandonCost }}</p>
        <div class="actions">
          <Button type="button" :disabled="abandoning" @click="confirmAbandon">
            {{ abandoning ? '正在放弃…' : '确认放弃' }}
          </Button>
          <Button
            type="button"
            variant="secondary"
            :disabled="abandoning"
            @click="confirmingAbandon = false"
          >
            取消
          </Button>
        </div>
      </div>
      <ErrorNotice v-if="abandonError" :message="abandonError" />
      <p v-if="abandonNote" role="status" class="muted abandon__note">{{ abandonNote }}</p>
    </section>
  </section>
</template>

<style scoped>
/* 素材待办卡不再套白卡：整块 tint 让它从板面上浮起，底部一条全宽钠黄基线点名
   「这一件在等你」，方角、贴画布底。信号本该是一条基线，不是盒子一侧的色条。 */
.asset-card {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  padding: var(--space-2) var(--space-3);
  background: var(--color-plate-raised);
  border-bottom: 1px solid var(--color-signal);
}
.asset-card h2 {
  margin: 0;
  font-size: var(--text-lead);
  font-weight: 500;
}
.asset-card h3 {
  margin: 0 0 var(--space-1);
  font-size: var(--text-body);
  font-weight: 500;
}
.prompt__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
}
.file-name {
  margin-left: var(--space-2);
  color: var(--color-ink-soft);
  font-family: var(--font-data);
  font-size: var(--text-micro);
  font-weight: 400;
}
/* 提示词正文是「照抄的接线清单」：发光线围出的一块面，等宽小字。 */
/* 提示词正文用正文字体与字号；等宽只留给文件名这类纯技术标识（见 .file-name）。
   等宽正文在 `ui-monospace` 回落时会变成系统衬线，读起来像另一套设计（2026-11-21 目检）。 */
/* 资产图词按 markdown 排版（住在 MarkdownBlock），这里只给外框；
   diff__text 仍是原样的文本块，保留自己的字体声明。 */
.prompt__text {
  margin: 0;
  padding: var(--space-3);
  border: 1px solid var(--color-rule);
  background: var(--color-plate);
}
.diff__text {
  margin: 0;
  padding: var(--space-3);
  border: 1px solid var(--color-rule);
  background: var(--color-plate);
  font-family: var(--font-sans);
  font-size: var(--text-body);
  line-height: 1.75;
  white-space: pre-wrap;
  word-break: break-word;
}
/* 差异块用错误色描一道线，仍是线不是色块。 */
.diff {
  padding: var(--space-3);
  border: 1px solid var(--color-error);
}
.restore {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-3);
  border: 1px solid var(--color-rule);
  background: var(--color-plate);
}
.exits {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: var(--space-2);
}
.exits__hint {
  margin: 0;
  color: var(--color-ink-soft);
  font-size: var(--text-micro);
}
.upload {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}
.actions {
  display: flex;
  justify-content: flex-end;
  gap: var(--space-2);
}
/* 放弃入口：与正文一道发光线隔开。 */
.abandon {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding-top: var(--space-3);
  border-top: 1px solid var(--color-rule);
}
.abandon__confirm {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}
.abandon__cost {
  margin: 0;
  color: var(--color-ink-soft);
  font-size: var(--text-micro);
}
.abandon__note {
  margin: 0;
}
.field-error {
  margin: 0;
  color: var(--color-error);
}
.muted {
  margin: 0;
  color: var(--color-ink-soft);
}
</style>
