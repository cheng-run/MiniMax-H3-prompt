import { describe, expect, it } from 'vitest'

import { makeDetail } from '@/test/sessionFixtures'

import { canvasFact, filmSizeFact } from './facts'

// 概览事实表的两行派生文案（#367）。值全部来自服务端（详情响应的
// `brief.canvas_lines` 与 `film_size`）——这里只断言「拿到什么就说什么」，
// 不重算宽高，也不抄一张家族显示名对照表。

function detailWith(overrides: Parameters<typeof makeDetail>[0] = {}) {
  return makeDetail(overrides)
}

describe('canvasFact 画布行', () => {
  it('有画布时逐行照抄服务端渲染的显示行', () => {
    const detail = detailWith()
    detail.brief.canvas_lines = ['Qwen-Image 2.1：1080×1920', 'Z-Image Turbo：640×1280']

    expect(canvasFact(detail)).toEqual({
      label: '画布',
      value: 'Qwen-Image 2.1：1080×1920；Z-Image Turbo：640×1280',
      note: '',
    })
  })

  it('没选画布时写明「未选，按各档默认」，不留空白', () => {
    expect(canvasFact(detailWith()).value).toBe('未选，按各档默认')
  })
})

describe('filmSizeFact 成片尺寸行', () => {
  it('画布推导分支：值照抄服务端声明值，文案标明不是实测', () => {
    const detail = detailWith()
    detail.film_size = {
      width: 480,
      height: 864,
      source: 'video_output',
      canvas: [1080, 1920],
      notes: [],
    }

    const row = filmSizeFact(detail)!
    expect(row.label).toBe('成片尺寸')
    expect(row.value).toContain('480 × 864')
    expect(row.value).toContain('推导的声明值')
    expect(row.value).toContain('不是实测')
    // 推导源画布也点名：用户能对上自己选的画布。
    expect(row.note).toContain('由画布 1080×1920 推导')
  })

  it('显式声明三元组时没有推导源画布，附注留空', () => {
    const detail = detailWith()
    detail.film_size = {
      width: 480,
      height: 864,
      source: 'video_output',
      canvas: null,
      notes: [],
    }

    expect(filmSizeFact(detail)!.note).toBe('')
  })

  it('推导注记逐条带上（比例回退、面积夹档不静默）', () => {
    const detail = detailWith()
    detail.film_size = {
      width: 480,
      height: 864,
      source: 'video_output',
      canvas: [1000, 1000],
      notes: ['画布比例 1:1 无对应比例档，回退最近档 1:1 (Square)'],
    }

    expect(filmSizeFact(detail)!.note).toContain('回退最近档')
  })

  it('导演台：标签是成片画幅，并说明实际值以节点 width/height 为准', () => {
    const detail = detailWith({ mode: 'director' })
    detail.film_size = { width: 1920, height: 1088, source: 'film_aspect', canvas: null, notes: [] }

    const row = filmSizeFact(detail)!
    expect(row.label).toBe('成片画幅')
    expect(row.value).toContain('1920 × 1088')
    expect(row.value).toContain('不是实测')
    expect(row.note).toBe('成片画幅以节点 width/height 为准')
  })

  it('base/ref 拿不到声明值时也出这一行：静默少一行会像「本就没有」', () => {
    const detail = detailWith()
    detail.film_size = null

    const row = filmSizeFact(detail)!
    expect(row.label).toBe('成片尺寸')
    expect(row.value).toContain('推导失败')
  })

  it('导演台旧会话没有成片画幅答案：照实说，不拿配置兜底值冒充答案', () => {
    const detail = detailWith({ mode: 'director' })
    detail.film_size = null

    const row = filmSizeFact(detail)!
    expect(row.label).toBe('成片画幅')
    expect(row.value).toBe('未声明（本会话没有成片画幅答案）')
    expect(row.note).toBe('成片画幅以节点 width/height 为准')
  })

  it('口径听 `film_size.source`，不按 mode 自己再判一次路线（#363 复审）', () => {
    // 服务端标了 film_aspect 就按成片画幅的口径说——即便 mode 是 ref，也不该被前端
    // 改叫「成片尺寸」。这条钉住「source 有消费者」：删掉对 source 的读取、退回只看
    // mode，本用例变红。
    const detail = detailWith({ mode: 'ref' })
    detail.film_size = {
      width: 1920,
      height: 1088,
      source: 'film_aspect',
      canvas: null,
      notes: [],
    }

    const row = filmSizeFact(detail)!
    expect(row.label).toBe('成片画幅')
    expect(row.note).toBe('成片画幅以节点 width/height 为准')
  })
})
