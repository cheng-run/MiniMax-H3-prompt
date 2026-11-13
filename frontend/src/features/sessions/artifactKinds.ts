import type { ArtifactInfo } from '@/api/client'

// 产物分组与中文标签的**唯一来源**（ADR 0034 / workbench.md 不变量）。
//
// 分组按契约里的 `ArtifactInfo.kind` 派生（值见 `workbench_store.artifact_spec`），
// 不在组件里抄文件名白名单：后端登记表改名或新增产物时，前端按 kind 自动跟上；
// 按名字枚举的旧写法会在改名后静默少显示一件。
export type ArtifactGroup = 'intermediate' | 'deliverable' | 'media'

const INTERMEDIATE_KINDS = new Set(['script', 'shot_table', 'frame_prompt'])
// `segment_prompt` 是逐段写出来的提示词（D6 §7）：用户要拿单段去跑，跟最终提示词
// 一样是可拿走的成品，所以归交付文本；漏登记会让它整件从界面上消失。
const DELIVERABLE_KINDS = new Set([
  'video_prompt',
  'segment_prompt',
  'summary_markdown',
  'summary_json',
])
const MEDIA_KINDS = new Set(['video', 'audio'])

export function artifactGroup(artifact: ArtifactInfo): ArtifactGroup | null {
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
  'frame_prompt',
  'video_prompt',
  'segment_prompt',
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
  frame_prompt: '帧提示词',
  video_prompt: '最终提示词',
  segment_prompt: '段提示词',
  summary_markdown: '中文摘要',
  summary_json: '摘要数据（JSON）',
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
