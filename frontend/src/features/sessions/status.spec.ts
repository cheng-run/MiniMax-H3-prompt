import { describe, expect, it } from 'vitest'

import { SESSION_STATUSES, stepLabel, statusLabel, statusTone } from './status'

// 步骤标签（job.progress 的 stage）是**展示层翻译**，不是会话状态：七态不动，
// 新增的是 `asset_loop` 这一档的文案（D7 §5.2）。
describe('status 步骤标签', () => {
  it('五个已知阶段各有中文名，未知与缺席一律 null（不猜）', () => {
    expect(stepLabel('stage1')).toBe('阶段 1')
    expect(stepLabel('asset_loop')).toBe('素材循环')
    expect(stepLabel('stage2')).toBe('阶段 2')
    expect(stepLabel('packing')).toBe('打包')
    expect(stepLabel('segmenting')).toBe('分段循环')
    expect(stepLabel('nope')).toBeNull()
    expect(stepLabel(null)).toBeNull()
    expect(stepLabel(undefined)).toBeNull()
  })

  it('会话状态仍是七态（素材循环不新增状态）', () => {
    expect(SESSION_STATUSES).toHaveLength(7)
    expect(SESSION_STATUSES).not.toContain('asset_loop')
    for (const status of SESSION_STATUSES) {
      expect(statusLabel(status)).toBeTruthy()
      expect(statusTone(status)).toBeTruthy()
    }
  })

  // 2026-11-21 critique：等待人工是本产品的常态，也是唯一必须由人动手的状态，
  // 此前与「已创建」「已取消」同为 neutral，20 行列表里扫视像没状态。
  it('等待人工有专属 tone，不与已创建/已取消同貌', () => {
    expect(statusTone('awaiting_human')).toBe('attention')
    expect(statusTone('created')).toBe('neutral')
    expect(statusTone('cancelled')).toBe('neutral')
  })
})
