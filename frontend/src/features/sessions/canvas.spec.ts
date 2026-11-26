import { describe, expect, it } from 'vitest'

import { snapCanvasSide, snapCanvasSize } from './canvas'

// 这组样例与后端 `tests/test_director_film_canvas.py::test_snap_film_size` 逐对相同：
// 前端只在 director 表单里当场提示「实际执行尺寸」，规则必须与
// `director.film_canvas.snap_film_size` 一致，否则界面说的和落库值不同。
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
    expect(snapCanvasSide(input)).toBe(expected)
  })

  it('一组宽高逐边对齐，与 snap_film_size 的返回形态一致', () => {
    expect(snapCanvasSize(1920, 1080)).toEqual({ width: 1920, height: 1088 })
    expect(snapCanvasSize(640, 1280)).toEqual({ width: 640, height: 1280 })
  })
})
