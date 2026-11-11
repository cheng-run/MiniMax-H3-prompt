import { expect, test } from '@playwright/test'

// D4-5（#232）：真实浏览器 + 真实 FastAPI，管线走 serve-e2e.mjs 注入的确定性桩
// （MINIMAX_FAKE_PIPELINE=1）。覆盖「审查 → 修订一轮 → 再审查」的完整回路：
// 中间产物可见、单条意见提交、重跑后新待办回到同一闸门、历史累积。

const PLOT = 'E2E 审查修订回路'

test('审查 → 修订一轮 → 新待办回闸门，历史累积', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('button', { name: '新建会话' }).click()
  await page.locator('textarea[name="plot"]').fill(PLOT)
  await page.getByRole('button', { name: '创建并开始' }).click()

  await expect(page).toHaveURL(/\/sessions\/.+/)
  // 桩阶段 1 跑完即停在人工闸门。
  await expect(page.getByText('等待人工').first()).toBeVisible({ timeout: 15_000 })

  await page.getByRole('tab', { name: '审查与待办' }).click()

  // 中间产物分文件可见：剧本、分镜表与两帧词都来自 content 端点。
  await expect(page.getByText('分场剧本（桩管线第 1 稿）')).toBeVisible()
  await expect(page.getByText('分镜镜头表（桩管线第 1 稿）')).toBeVisible()
  await expect(page.getByText('桩管线第 1 稿的开场画面', { exact: false })).toBeVisible()
  await expect(page.getByText('桩管线第 1 稿的收束画面', { exact: false })).toBeVisible()

  // 提意见：四档层次 + 一条文本，单条提交（对齐向导一轮一条）。
  await page.locator('select[name="revision-layer"]').selectOption('剧情分镜')
  await page.locator('textarea[name="revision-text"]').fill('结局需要更克制的收束')
  await page.getByRole('button', { name: '提交意见' }).click()

  // 重跑完成：新待办（新 item_id）回到同一闸门，产物更新到第 2 稿。
  await expect(page.getByText('分场剧本（桩管线第 2 稿）')).toBeVisible({ timeout: 20_000 })
  await expect(page.getByRole('button', { name: '通过' })).toBeVisible()

  // 修订历史从已决议待办重建：层次按中文展示，意见原文在。
  const history = page.getByRole('region', { name: '修订历史' })
  await expect(history).toContainText('剧情分镜')
  await expect(history).toContainText('结局需要更克制的收束')

  // 再审查：通过后阶段 1 闸门关闭；帧链会话转入帧提交待办（不属于本分区），
  // 修订历史保留。
  await page.getByRole('button', { name: '通过' }).click()
  await expect(page.getByText('当前没有待审查的待办。')).toBeVisible({ timeout: 20_000 })
  await expect(page.getByRole('region', { name: '修订历史' })).toContainText('结局需要更克制的收束')
})

test('空意见即时提示且不发请求', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('button', { name: '新建会话' }).click()
  await page.locator('textarea[name="plot"]').fill(`${PLOT} 空意见`)
  await page.getByRole('button', { name: '创建并开始' }).click()

  await expect(page.getByText('等待人工').first()).toBeVisible({ timeout: 15_000 })
  await page.getByRole('tab', { name: '审查与待办' }).click()
  await expect(page.getByText('分场剧本（桩管线第 1 稿）')).toBeVisible()

  // 不填意见直接提交：前端即时提示，会话仍停在同一个闸门（第 1 稿没变）。
  await page.getByRole('button', { name: '提交意见' }).click()
  await expect(page.getByText('修订内容不能为空。')).toBeVisible()
  await expect(page.getByText('分场剧本（桩管线第 1 稿）')).toBeVisible()
  await expect(page.getByText('等待人工').first()).toBeVisible()
})
