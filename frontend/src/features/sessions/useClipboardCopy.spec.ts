import { describe, expect, it, vi } from 'vitest'

import { useClipboardCopy } from './useClipboardCopy'

function stubClipboard(writeText: ReturnType<typeof vi.fn>): void {
  Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
}

describe('useClipboardCopy', () => {
  it('复制成功后按钮文案变「已复制」，两个实例互不影响', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    stubClipboard(writeText)
    const env = useClipboardCopy('复制路径')
    const address = useClipboardCopy('复制')

    expect(env.label.value).toBe('复制路径')
    await env.copy('C:/Users/me/AppData/Roaming/MiniMax-H3-prompt/.env')

    expect(writeText).toHaveBeenCalledWith('C:/Users/me/AppData/Roaming/MiniMax-H3-prompt/.env')
    expect(env.state.value).toBe('copied')
    expect(env.label.value).toBe('已复制')
    // 各自的 state/文案：点「复制路径」不该把地址按钮也改成「已复制」。
    expect(address.state.value).toBe('idle')
    expect(address.label.value).toBe('复制')
  })

  it('剪贴板被拒时进入 failed，文案保持原标题（界面据此提示手动复制）', async () => {
    stubClipboard(vi.fn().mockRejectedValue(new Error('denied')))
    const copy = useClipboardCopy('复制路径')

    await copy.copy('C:/x/.env')

    expect(copy.state.value).toBe('failed')
    expect(copy.label.value).toBe('复制路径')
  })
})
