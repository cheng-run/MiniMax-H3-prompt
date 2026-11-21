import { ref, watch } from 'vue'

// 主题：跟随系统、可手动覆盖，选择记在 localStorage。
//
// 为什么不放 Pinia：主题是文档级属性（写在 <html data-theme>），不参与组件树状态，
// 而且在首帧之前就要落定；Pinia store 要到 app.use 之后才存在，那时已经晚了。

export type ThemeChoice = 'system' | 'dark' | 'light'
export type ResolvedTheme = 'dark' | 'light'

const STORAGE_KEY = 'h3.theme'

function readStored(): ThemeChoice {
  try {
    const value = localStorage.getItem(STORAGE_KEY)
    if (value === 'dark' || value === 'light' || value === 'system') {
      return value
    }
  } catch {
    // 隐私模式下 localStorage 会抛；跟随系统即可，不因为读不到偏好就白屏。
  }
  return 'system'
}

function systemTheme(): ResolvedTheme {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') {
    return 'dark'
  }
  return window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark'
}

const choice = ref<ThemeChoice>(readStored())
const resolved = ref<ResolvedTheme>('dark')

function apply(): ResolvedTheme {
  const next = choice.value === 'system' ? systemTheme() : choice.value
  resolved.value = next
  document.documentElement.dataset.theme = next
  return next
}

let mediaQuery: MediaQueryList | null = null

/** 首帧前调用一次；装好系统配色变化的监听。 */
export function initTheme(): void {
  apply()
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') {
    return
  }
  mediaQuery = window.matchMedia('(prefers-color-scheme: light)')
  mediaQuery.addEventListener('change', () => {
    if (choice.value === 'system') {
      apply()
    }
  })
  watch(choice, (value) => {
    try {
      if (value === 'system') {
        localStorage.removeItem(STORAGE_KEY)
      } else {
        localStorage.setItem(STORAGE_KEY, value)
      }
    } catch {
      // 写不进去不影响本次会话，只是下次要重新选。
    }
    apply()
  })
}

export function useTheme() {
  return { choice, resolved, setChoice: (value: ThemeChoice) => (choice.value = value) }
}
