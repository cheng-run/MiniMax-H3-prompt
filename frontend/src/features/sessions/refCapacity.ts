// 参考素材的官方容量上限（D7 spec §8 / #255）。9+3+3 = 15 > 12，所以「12 文件」是
// **独立**上限，不是前三者之和。
//
// 数字的唯一真源是 Python 侧的 `src/minimax_h3_prompt/ref_capacity.py`
// （MAX_PICTURES / MAX_VIDEOS / MAX_AUDIOS / MAX_FILES，外部契约、不是本仓调参）。
// 跨语言没有可 import 的常量，这里按原值镜像一份：改那边必须同时改这里——前端只负责
// 「即时红」，服务端仍会在超限时回 422 `ref_capacity_exceeded`。
export const REF_CAPACITY = {
  picture: 9,
  video: 3,
  audio: 3,
  files: 12,
} as const

// 官方三型的显示名；key 与契约里的 `brief.refs[].kind` 同词表。
export const REF_KIND_LABELS = {
  picture: '图',
  video: '视频',
  audio: '音频',
} as const

export type RefKind = keyof typeof REF_KIND_LABELS

export interface CapacityRow {
  kind: RefKind | 'files'
  label: string
  used: number
  cap: number
  over: boolean
}

/** 按台账算四档用量；任一档超过上限即 `over`（素材分区据此变红）。 */
export function capacityRows(refs: ReadonlyArray<{ kind: string }>): CapacityRow[] {
  const kinds: RefKind[] = ['picture', 'video', 'audio']
  const rows: CapacityRow[] = kinds.map((kind) => {
    const used = refs.filter((ref) => ref.kind === kind).length
    const cap = REF_CAPACITY[kind]
    return { kind, label: REF_KIND_LABELS[kind], used, cap, over: used > cap }
  })
  const total = refs.length
  rows.push({
    kind: 'files',
    label: '文件',
    used: total,
    cap: REF_CAPACITY.files,
    over: total > REF_CAPACITY.files,
  })
  return rows
}
