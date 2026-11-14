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
  // 导演台逐段操作简报（D8 §8.5 / #263）：用户照着抄进导演台，可复制文本。
  'director_briefing',
  // 导演台按需词（D8 §8.3 / #264）：切镜段首帧词与运镜/收尾段尾帧词，都是
  // 「复制去生图」的成品文本，归交付文本。
  'director_first_frame_prompt',
  'director_tailframe_prompt',
  // 交付记录（D8 §12.2 / #266）：成片路径/帧数/段数/宽高/时间的文本记录。
  'director_final_film',
  'injection_card',
  'concat_order_card',
  'summary_markdown',
  'summary_json',
])
// 归档不是一个播放形态：``director_pack`` 下载后交给插件导入，**不能**当 ``<video>``
// 播。分组归媒体（它和成片一样是二进制交付物），渲染按 kind 走下载链接。
// ``director_final_pack`` 是交付归档的最终包（D8 §12.2 / #266）：与初始包同一种读法，
// 但分开一个 kind——页面要能说清「哪一份是计划、哪一份是结论」。
const ARCHIVE_KINDS = new Set(['director_pack', 'director_final_pack'])
const MEDIA_KINDS = new Set(['video', 'audio', 'director_pack', 'director_final_pack'])
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
  'director_briefing',
  'director_first_frame_prompt',
  'director_tailframe_prompt',
  'director_final_film',
  'summary_markdown',
  'summary_json',
  'video',
  'audio',
  'director_pack',
  'director_final_pack',
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
  director_briefing: '导演台简报',
  director_first_frame_prompt: '导演台段首帧词',
  director_tailframe_prompt: '导演台尾帧词',
  director_final_film: '成片记录',
  director_pack: '导演包',
  director_final_pack: '最终包',
  summary_markdown: '中文摘要',
  summary_json: '摘要数据（JSON）',
}

/**
 * 该 kind 是否按**归档下载**渲染（而不是 `<video>` / `<audio>` 播放）。
 *
 * archive 与媒体共用 content 端点与 Range 语义，但浏览器没有内置播放器能打开
 * zip：给一个下载链接是本分区唯一正确的渲染。
 */
export function isArchiveArtifact(kind: string): boolean {
  return ARCHIVE_KINDS.has(kind)
}

/** 归档下载按钮的文案：初始包是计划、最终包是结论，两句话不能合成一句。 */
export function archiveDownloadLabel(kind: string): string {
  return kind === 'director_final_pack' ? '下载最终包' : '下载导演包'
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
 * 导演台按需词（D8 §8.3 / #264）：与后端 `director_first_frame_prompt_name` /
 * `director_tailframe_prompt_name` 同形，段号两位。两个名字只在这里拼一次——
 * 组件里再写一遍，登记表改名时它会静默取不到词。
 */
export function directorFirstFramePromptName(position: number): string {
  return `director/firstframe-${String(position).padStart(2, '0')}.md`
}

export function directorTailframePromptName(position: number): string {
  return `director/tailframe-${String(position).padStart(2, '0')}.md`
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
