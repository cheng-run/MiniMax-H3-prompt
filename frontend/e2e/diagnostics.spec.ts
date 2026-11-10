import { expect, test } from '@playwright/test'

test('通过导航进入 /diagnostics 显示真实 health', async ({ page }) => {
  await page.goto('/')
  // 限定在主导航内点击，避免与首页“查看服务诊断”链接的模糊匹配撞车。
  await page.getByRole('navigation', { name: '主导航' }).getByRole('link', { name: '诊断' }).click()

  await expect(page).toHaveURL(/\/diagnostics$/)
  await expect(page.getByText('minimax-h3-prompt')).toBeVisible()
  await expect(page.getByText('服务正常')).toBeVisible()
})

test('直接打开深链 /diagnostics 成功（SPA fallback）', async ({ page }) => {
  await page.goto('/diagnostics')

  await expect(page.getByText('minimax-h3-prompt')).toBeVisible()
  await expect(page.getByText('服务正常')).toBeVisible()
})

test('键盘 Enter 展开诊断详情', async ({ page }) => {
  await page.goto('/diagnostics')

  const toggle = page.getByRole('button', { name: '展开详情' })
  await toggle.focus()
  await page.keyboard.press('Enter')

  await expect(page.getByRole('button', { name: '收起详情' })).toBeVisible()
})

test('页面自动取到启动令牌并写入 SameSite=Strict 的会话 Cookie', async ({ page }) => {
  // 真实浏览器 + 真实 FastAPI：API 调用自动带令牌头，引导端点同时下发 Cookie——
  // 后者是 <video>/<img> 唯一能用上的凭据（D3-AC3）。
  await page.goto('/diagnostics')
  await expect(page.getByText('服务正常')).toBeVisible()

  const session = (await page.context().cookies()).find((c) => c.name === 'wb_session')

  expect(session).toBeDefined()
  expect(session?.httpOnly).toBe(true)
  expect(session?.sameSite).toBe('Strict')
  expect(session?.path).toBe('/api/v1')
})
