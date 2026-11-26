import type { SessionDetail } from '@/api/client'

// 概览事实表的两行派生文案（#367 / spec #363 US8–US10）。
//
// **不在这里重算任何尺寸**（票面 AC：推导值与既有推导函数一致，用例直接调该函数
// 比对，不在前端另算一遍）。服务端已经把两条路线的推导结果与家族显示名都算好：
//   - `brief.canvas_lines`：画布行（服务端按 `IMAGE_FAMILY_LABELS` 渲染）；
//   - `film_size`：成片尺寸的**推导声明值**（导演台＝成片画幅；base/ref＝注入卡口径）。
// 本模块只负责「照实说」：措辞按路线切换、把「声明值不是实测值」说破、旧会话没有
// 答案时如实交代。纯函数——用例不需要挂载组件。

export interface FactRow {
  label: string
  value: string
  /** 行下的一句附注；空串不显示。 */
  note: string
}

/** 画布行：值与 `brief.canvases` 一致（服务端渲染的行逐条照抄）。 */
export function canvasFact(detail: SessionDetail): FactRow {
  const lines = detail.brief.canvas_lines ?? []
  return {
    label: '画布',
    // 没选时**写明**「未选，按各档默认」，留空白会让人以为漏了一条。
    value: lines.length ? lines.join('；') : '未选，按各档默认',
    note: '',
  }
}

/** 成片尺寸行：推导的声明值，不是实测值（CONTEXT「成片尺寸」「视频输出」）。 */
export function filmSizeFact(detail: SessionDetail): FactRow | null {
  const film = detail.film_size ?? null
  // 口径由服务端的 `film_size.source` 决定，前端不自己再判一次路线：两份推导
  // （导演台的成片画幅、base/ref 的注入卡视频输出）语义不同，由后端标注哪一份。
  // 只有 `film_size` 缺席时才退回落 `mode`——那时没有 source 可读，只能按路线说清
  // 「为什么这一行没有值」。
  const source = film?.source ?? (detail.mode === 'director' ? 'film_aspect' : 'video_output')
  if (source === 'film_aspect') {
    // 导演台：这一格是**成片画幅**（向导唯一问一次的答案），实际执行值以节点为准
    // （CONTEXT「视频输出」：包 output 与声明值都可能与节点不符）。
    return {
      label: '成片画幅',
      value: film
        ? `${film.width} × ${film.height}（推导的声明值，不是实测）`
        : '未声明（本会话没有成片画幅答案）',
      note: '成片画幅以节点 width/height 为准',
    }
  }
  if (detail.mode !== 'base' && detail.mode !== 'ref') {
    // 未知路线的 `film_size` 本该为 null；万一后端给了值也不猜口径，整行不出。
    return null
  }
  if (!film) {
    // base/ref 的声明值只有「推导不出来」这一种缺席（服务端降级，见 `_film_size`）。
    // 仍然出这一行：静默少一行会让人以为这条路线本就没有声明值。
    return { label: '成片尺寸', value: '推导失败，本次拿不到声明值', note: '' }
  }
  // base/ref：注入卡口径的「视频输出参数」——推导的声明值（CONTEXT 同一节）。
  return {
    label: '成片尺寸',
    value: `${film.width} × ${film.height}（推导的声明值，不是实测）`,
    note: filmNote(film.canvas ?? null, film.notes ?? []),
  }
}

function filmNote(canvas: number[] | null, notes: string[]): string {
  const parts: string[] = []
  if (canvas) {
    parts.push(`由画布 ${canvas[0]}×${canvas[1]} 推导`)
  }
  parts.push(...notes)
  return parts.join('；')
}
