import { expect, test } from '@playwright/test'

test('未知客户端路径显示 404 视图', async ({ page }) => {
  await page.goto('/this-path-does-not-exist')

  // 2026-11-21 重做：404 换成「这个地址没有对应的页面」，断言跟着改。
  await expect(page.getByRole('heading', { name: '这个地址没有对应的页面' })).toBeVisible()
  // 导航回已知路径可恢复。
  await page.getByRole('link', { name: '返回会话列表' }).click()
  await expect(page).toHaveURL(/\/$/)
})
