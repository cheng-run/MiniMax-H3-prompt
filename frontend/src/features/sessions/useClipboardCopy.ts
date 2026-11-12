import { computed, ref } from 'vue'

/** 复制按钮的三态：``idle`` → ``copied`` / ``failed``（失败时界面提示手动复制）。 */
export type CopyState = 'idle' | 'copied' | 'failed'

/**
 * 「复制到剪贴板」按钮的最小共享状态（#243 复审）。
 *
 * 会话页有两处复制（密钥文件路径、实际地址）。原先各写一套 state / computed / 函数，
 * 两份唯一的差别是 idle 文案——复制失败提示还各写一遍。抽成一个组合式函数，
 * 每次调用是**独立**的一份状态（互不影响），调用方只提供标题文案。
 */
export function useClipboardCopy(idleLabel: string) {
  const state = ref<CopyState>('idle')
  const label = computed(() => (state.value === 'copied' ? '已复制' : idleLabel))

  async function copy(text: string): Promise<void> {
    try {
      await navigator.clipboard.writeText(text)
      state.value = 'copied'
    } catch {
      // 剪贴板被拒（非安全上下文、权限策略）时不能静默：交给界面提示手动复制。
      state.value = 'failed'
    }
  }

  return { state, label, copy }
}
