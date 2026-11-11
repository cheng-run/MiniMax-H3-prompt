// 修订层次（CONTEXT.md「用户修订」）：层次＝重跑起点，四档。屏幕标签逐字取自
// ``user_revisions.LAYER_LABELS``，别在页面上另造一套写法——用户与后人会拿它对术语表。
// 提交时给**中文别名**，服务端 `sessions.py` 负责归一成内部四档（契约已收中文）。
export interface ReviewLayer {
  /** 提交给 resolve 的层次（中文别名，服务端归一）。 */
  value: string
  /** 内部四档名，用于把已决议待办回显的 layer 翻译回屏幕标签。 */
  internal: string
  /** 屏幕上那句「回灌什么、重跑什么」——用户凭它选层次。 */
  note: string
}

export const REVIEW_LAYERS: ReviewLayer[] = [
  { value: '只改画面', internal: 'frame', note: '只重出画面与帧提示词' },
  { value: '人物设定', internal: 'character', note: '回灌人物形象设计，并重跑其下游' },
  { value: '美术场景道具', internal: 'art', note: '回灌背景与道具设计，并重跑其下游' },
  { value: '剧情分镜', internal: 'story', note: '回灌分场剧本与分镜表，并重跑其下游' },
]

const LABEL_BY_INTERNAL = new Map(REVIEW_LAYERS.map((layer) => [layer.internal, layer.value]))

/** 内部四档名 → 屏幕标签；服务端回显的历史用的是内部值。 */
export function revisionLayerLabel(internal: string): string {
  return LABEL_BY_INTERNAL.get(internal) ?? internal
}
