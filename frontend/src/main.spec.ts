import { describe, expect, it } from 'vitest'

// 覆盖启动入口 main.ts：装上 Pinia/路由/Vue Query 并把 App 挂到 #app。
describe('main 启动入口', () => {
  it('把应用挂载到 #app', async () => {
    document.body.innerHTML = '<div id="app"></div>'

    await import('./main')

    const root = document.querySelector('#app')
    expect(root).not.toBeNull()
    // AppShell 渲染 skip-link + header + main 多个根节点，挂在 #app 之下。
    expect(root!.childElementCount).toBeGreaterThan(0)
  })
})
