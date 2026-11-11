import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it } from 'vitest'

import { useUiStore } from './ui'

describe('ui store', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('诊断详情默认收起，toggle 双向切换', () => {
    const ui = useUiStore()
    expect(ui.diagnosticsExpanded).toBe(false)
    ui.toggleDiagnostics()
    expect(ui.diagnosticsExpanded).toBe(true)
    ui.toggleDiagnostics()
    expect(ui.diagnosticsExpanded).toBe(false)
  })

  it('工作台分区选择只是内存 UI 状态：默认概览，刷新（新 store）即回概览', () => {
    const ui = useUiStore()
    expect(ui.workbenchTab).toBe('overview')
    ui.selectWorkbenchTab('review')
    expect(ui.workbenchTab).toBe('review')

    // 分区不进 URL、不落盘：刷新等于一份新的 Pinia，自然回到概览（D4-AC4）。
    setActivePinia(createPinia())
    expect(useUiStore().workbenchTab).toBe('overview')
  })
})
