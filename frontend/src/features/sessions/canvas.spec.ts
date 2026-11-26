import { describe, expect, it } from 'vitest'

import { snapCanvasSide, snapCanvasSize } from './canvas'

// 这组样例与后端 `tests/test_director_film_canvas.py` 里 `snap_film_size` 的那组
// 逐对相同（1920×1080、720×1280、640×1280、1×31）：前端只在 director 表单里当场
// 提示「实际执行尺寸」，规则必须与 `director.film_canvas.snap_film_size` 一致，
// 否则界面说的和落库值不同。
//
// 网格值来自服务端 `canvas-presets.align`（后端 `director.pack.CANVAS_ALIGN`）：
// 这里按 32 传入只是因为后端的默认网格就是 32，算法本身不认得这个数。
const ALIGN = 32

describe('snapCanvasSide 与后端 snap_film_size 同一组数字', () => {
  it.each([
    [1920, 1920],
    [1080, 1088],
    [720, 736],
    [1280, 1280],
    [640, 640],
    [1, 32],
    [31, 32],
    // 正好半格时向上补：银行家舍入会把它判成 64（偶数优先），这支算法给 96。
    [80, 96],
  ])('%i → %i', (input, expected) => {
    expect(snapCanvasSide(input, ALIGN)).toBe(expected)
  })

  it('一组宽高逐边对齐，与 snap_film_size 的返回形态一致', () => {
    expect(snapCanvasSize(1920, 1080, ALIGN)).toEqual({ width: 1920, height: 1088 })
    expect(snapCanvasSize(640, 1280, ALIGN)).toEqual({ width: 640, height: 1280 })
  })

  it('对齐值由调用方传入，不是函数里的字面量（#363 复审 A4）', () => {
    // 换一个网格，算法跟着换：值若是写死的 32，这条会红。
    expect(snapCanvasSide(100, 64)).toBe(128)
    expect(snapCanvasSide(100, 16)).toBe(96)
  })
})
