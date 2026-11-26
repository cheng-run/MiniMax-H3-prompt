/**
 * 成片画幅的 32 网格对齐（#366）。
 *
 * 为什么前端也要这一份：导演台表单在用户选完尺寸时要**当场**说破「实际执行尺寸」
 * （1920×1080 → 1920×1088）。本票不改后端契约，创建响应里没有对齐后的值可展示，
 * 所以只能在前端算；算法与 `src/minimax_h3_prompt/director/film_canvas.py` 的
 * `snap_film_size` 逐字同源（向上补半格再截到网格、最小一格）。两侧漂移由
 * `canvas.spec.ts` 用与 `tests/test_director_film_canvas.py` 相同的样例钉住。
 *
 * 只用于界面提示：真正落库的对齐值由服务端 `_canvases_from_payload` 计算。
 */
const CANVAS_ALIGN = 32

/** 单边对齐：`_snap` 的镜像（四舍五入而非银行家舍入，见后端同名函数的说明）。 */
export function snapCanvasSide(value: number): number {
  const snapped = Math.floor((Math.trunc(value) + CANVAS_ALIGN / 2) / CANVAS_ALIGN) * CANVAS_ALIGN
  return Math.max(CANVAS_ALIGN, snapped)
}

/** 一组宽高的对齐结果（与 `snap_film_size` 的返回形态一致）。 */
export function snapCanvasSize(width: number, height: number): { width: number; height: number } {
  return { width: snapCanvasSide(width), height: snapCanvasSide(height) }
}
