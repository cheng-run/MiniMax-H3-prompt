import { describe, expect, it } from 'vitest'

import { SESSION_STATUSES, stepLabel, statusLabel, statusTone } from './status'

// 步骤标签（job.progress 的 stage）是**展示层翻译**，不是会话状态：七态不动，
// 新增的是 `asset_loop` 这一档的文案（D7 §5.2）。
describe('status 步骤标签', () => {
  it('四个已知阶段各有中文名，未知与缺席一律 null（不猜）', () => {
    expect(stepLabel('stage1')).toBe('阶段 1')
    expect(stepLabel('asset_loop')).toBe('素材循环')
    expect(stepLabel('stage2')).toBe('阶段 2')
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
})
