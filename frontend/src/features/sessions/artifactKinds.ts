import type { ArtifactInfo } from '@/api/client'

// 产物分组与中文标签的**唯一来源**（ADR 0034 / workbench.md 不变量）。
//
// 分组按契约里的 `ArtifactInfo.kind` 派生（值见 `workbench_store.artifact_spec`），
// 不在组件里抄文件名白名单：后端登记表改名或新增产物时，前端按 kind 自动跟上；
// 按名字枚举的旧写法会在改名后静默少显示一件。
export type ArtifactGroup = 'intermediate' | 'deliverable' | 'media'

// `asset_prompt` 是素材循环逐件写出的资产图词、`ref_shot_table` 是多参四列分镜表
// （D7 §7 / §11）：两件都是阶段 1 之后、交付之前的中间产物，漏登记会让它们整件
// 从产物分区消失（用户拿不到要复制的词与段内素材编号的对照表）。
const INTERMEDIATE_KINDS = new Set([
  'script',
  'shot_table',
  'ref_shot_table',
  'frame_prompt',
  'asset_prompt',
])
// `segment_prompt` 是逐段写出来的提示词（D6 §7）：用户要拿单段去跑，跟最终提示词
// 一样是可拿走的成品，所以归交付文本；漏登记会让它整件从界面上消失。
// `injection_card` 是整片接线清单（D7 §9 / #257）：素材循环结束后自动产出，用户照卡
// 进 ComfyUI 接线——同样放进交付文本，只读可复制。
// `concat_order_card` 是拼接顺序卡（D7 §11）：交付前必须照它确认段序，性质与注入卡
// 相同（给人照做的只读清单），所以也放交付文本，而不是跟着段词住中间产物。
const DELIVERABLE_KINDS = new Set([
  'video_prompt',
  'segment_prompt',
  // 导演台段词（D8 §8.2 / #262）：逐段 ``director/segment-NN.md``。与 base/ref 的
  // ``segment_prompt`` 分开一个 kind：同一条会话目录里两种段词不会互相冒充。
  'director_segment_prompt',
  'injection_card',
  'concat_order_card',
  'summary_markdown',
  'summary_json',
])
const MEDIA_KINDS = new Set(['video', 'audio'])
// 段级注入卡只住「分段」分区、与段词同屏（D7 §9）：它不属于产物分区的任何一组，
// 这是**有意**不显示，不是漏登记。
const SEGMENT_LOCAL_KINDS = new Set(['segment_injection_card'])

export function artifactGroup(artifact: ArtifactInfo): ArtifactGroup | null {
  if (SEGMENT_LOCAL_KINDS.has(artifact.kind)) {
    return null
  }
  if (INTERMEDIATE_KINDS.has(artifact.kind)) {
    return 'intermediate'
  }
  if (DELIVERABLE_KINDS.has(artifact.kind)) {
    return 'deliverable'
  }
  if (MEDIA_KINDS.has(artifact.kind)) {
    return 'media'
  }
  return null
}

// 阅读顺序＝ kind 的顺序（中间产物在前），同类内按文件名稳定排序。
const KIND_ORDER = [
  'script',
  'shot_table',
  'ref_shot_table',
  'frame_prompt',
  'asset_prompt',
  'injection_card',
  'concat_order_card',
  'video_prompt',
  'segment_prompt',
  'director_segment_prompt',
  'summary_markdown',
  'summary_json',
  'video',
  'audio',
]

export function sortArtifacts(artifacts: ArtifactInfo[]): ArtifactInfo[] {
  return [...artifacts].sort((left, right) => {
    const delta = KIND_ORDER.indexOf(left.kind) - KIND_ORDER.indexOf(right.kind)
    return delta !== 0 ? delta : left.name.localeCompare(right.name)
  })
}

const KIND_LABELS: Record<string, string> = {
  script: '剧本',
  shot_table: '分镜表',
  ref_shot_table: '多参分镜表',
  frame_prompt: '帧提示词',
  asset_prompt: '资产图词',
  injection_card: '注入卡',
  concat_order_card: '拼接顺序卡',
  video_prompt: '最终提示词',
  segment_prompt: '段提示词',
  director_segment_prompt: '导演台段词',
  summary_markdown: '中文摘要',
  summary_json: '摘要数据（JSON）',
}

/**
 * 段级注入卡的产物名（D7 §9 / #257）：与后端 `segment_injection_card_name` 同形，
 * 段号两位。分段分区按它到产物清单里取本段的卡——名字拼在组件里等于第二份契约。
 */
export function segmentInjectionCardName(position: number): string {
  return `segments/injection-card-${String(position).padStart(2, '0')}.md`
}

/**
 * 资产图词（D7 §7）：与后端登记表同一形状 `assets/picture-NN.md`，件号两位。
 * 素材卡与产物分区都从这一处取名——组件里再拼一份，登记表改名时它会静默少取一件。
 */
export function assetPromptName(number: number): string {
  return `assets/picture-${String(number).padStart(2, '0')}.md`
}

/**
 * 帧提示词的**角色绑定**，不是显示白名单（D4 spec §7 / §9）。
 *
 * 首/尾两个文件在登记表里同 kind `frame_prompt`，文件名是唯一能区分角色的标识：
 * 帧提交卡要把「首帧槽 / 尾帧槽」各自绑到对应提示词。映射集中在这里一处，
 * 登记表改名时只会影响这一处，不会散落在多个组件里各抄一遍。
 */
export const FRAME_PROMPT_FILES = {
  first: 'first-frame-prompt.md',
  last: 'last-frame-prompt.md',
} as const

export function artifactLabel(artifact: ArtifactInfo): string {
  if (artifact.kind === 'frame_prompt') {
    // 同 kind 两个文件：按角色绑定细分标签，其余仍按 kind 走。
    if (artifact.name === FRAME_PROMPT_FILES.first) {
      return '首帧提示词'
    }
    if (artifact.name === FRAME_PROMPT_FILES.last) {
      return '尾帧提示词'
    }
  }
  return KIND_LABELS[artifact.kind] ?? artifact.name
}
