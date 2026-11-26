/**
 * 成片画幅的插件网格对齐（#366）。
 *
 * 为什么前端也要算：导演台表单在用户选完尺寸时要**当场**说破「实际执行尺寸」
 * （1920×1080 → 1920×1088）。创建响应在提交之前拿不到，所以前端按同一规则算一遍。
 *
 * **网格值不在这里写死**：`align` 由 `GET /api/v1/canvas-presets` 下发（服务端从
 * `director.pack.CANVAS_ALIGN` 现取）。早先这里抄了一份字面量 32，后端改网格时它
 * 不会跟着变，界面说的与实际落库的各是一套（#363 复审 A4）。
 *
 * 算法本身与后端 `director.film_canvas.snap_film_size` 同源（向上补半格再截到网格、
 * 最小一格）；`canvas.spec.ts` 用与后端同一组数字钉住两侧不漂。
 *
 * 只用于界面提示：真正落库的对齐值由服务端 `_canvases_from_payload` 计算。
 */

/** 单边对齐：`_snap` 的镜像（四舍五入而非银行家舍入，见后端同名函数的说明）。 */
export function snapCanvasSide(value: number, align: number): number {
  const snapped = Math.floor((Math.trunc(value) + align / 2) / align) * align
  return Math.max(align, snapped)
}

/** 一组宽高的对齐结果（与 `snap_film_size` 的返回形态一致）。 */
export function snapCanvasSize(
  width: number,
  height: number,
  align: number,
): { width: number; height: number } {
  return { width: snapCanvasSide(width, align), height: snapCanvasSide(height, align) }
}
