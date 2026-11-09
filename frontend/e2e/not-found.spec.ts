import { expect, test } from '@playwright/test'

test('未知客户端路径显示 404 视图', async ({ page }) => {
  await page.goto('/this-path-does-not-exist')

  await expect(page.getByRole('heading', { name: '页面不存在' })).toBeVisible()
  // 导航回已知路径可恢复。
  await page.getByRole('link', { name: '返回首页' }).click()
  await expect(page).toHaveURL(/\/$/)
})
