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
})
