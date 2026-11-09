import { expect, test } from '@playwright/test'

test('首页加载', async ({ page }) => {
  await page.goto('/')

  await expect(page.getByRole('heading', { level: 1 })).toContainText('MiniMax H3 工作台')
  await expect(page.getByRole('navigation', { name: '主导航' })).toBeVisible()
  // 跳转主内容链接存在且指向 main。
  await expect(page.getByRole('link', { name: '跳到主内容' })).toHaveAttribute(
    'href',
    '#main-content',
  )
})
