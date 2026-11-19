import { expect, test } from '@playwright/test'

// 起步澄清问答面板（#339 / spec #332 E1）的浏览器证据：真实构建产物 + 真实 FastAPI，
// 桩管线（serve-e2e.mjs 注入 MINIMAX_FAKE_PIPELINE=1）也接管了澄清端点——问句是固定的，
// 但「点创建先拉问题 → 面板展开 → 作答 → 随会话提交 → 落进澄清块」整条接线是真的。
//
// 断言落在**会话产物**上（详情响应里的 ``brief.clarifications``），不看页面回显：
// 页面能显示问答对不代表后端收到了它。

const PLOT = 'E2E 起步澄清面板'

test('点创建先拉问题：面板逐条作答后随会话提交，澄清块由后端渲染', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('button', { name: '新建会话' }).click()
  await page.locator('textarea[name="plot"]').fill(PLOT)

  // 第一下「创建并开始」只拉问题：不跳转，面板内联展开。
  await page.getByRole('button', { name: '创建并开始' }).click()

  const panel = page.getByRole('region', { name: '起步澄清' })
  await expect(panel).toBeVisible()
  await expect(page.locator('textarea[name="clarification-answer-0"]')).toBeVisible()
  await expect(panel).toContainText('左／右／前／后')

  await page.locator('textarea[name="clarification-answer-0"]').fill('窗在人物右侧')

  await page.getByRole('button', { name: '继续创建' }).click()
  await expect(page).toHaveURL(/\/sessions\/.+/)

  // 会话创建出来了，澄清块由后端渲染（编号 + 「问题 → 回答」），前端只交了问答对。
  const sessionId = page.url().replace(/^.*\/sessions\//, '')
  await expect
    .poll(
      async () => {
        const resp = await page.request.get(`/api/v1/sessions/${decodeURIComponent(sessionId)}`)
        if (!resp.ok()) {
          return ''
        }
        const body = (await resp.json()) as { brief: { clarifications: string } }
        return body.brief.clarifications
      },
      { timeout: 10_000 },
    )
    .toContain('窗在人物右侧')

  const resp = await page.request.get(`/api/v1/sessions/${decodeURIComponent(sessionId)}`)
  const body = (await resp.json()) as { brief: { clarifications: string } }
  // 渲染归后端：题面原文与编号都在澄清块里，且不是前端拼的那一条。
  expect(body.brief.clarifications).toMatch(/^1\. .*→ 窗在人物右侧$/m)
})

test('跳过：不提交任何问答对，会话照常建出来且澄清块为空', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('button', { name: '新建会话' }).click()
  await page.locator('textarea[name="plot"]').fill(`${PLOT} 跳过`)

  await page.getByRole('button', { name: '创建并开始' }).click()
  const panel = page.getByRole('region', { name: '起步澄清' })
  await expect(panel).toBeVisible()
  await page.locator('textarea[name="clarification-answer-0"]').fill('答了也带走不了')

  await page.getByRole('button', { name: '跳过', exact: true }).click()
  await expect(page).toHaveURL(/\/sessions\/.+/)

  const sessionId = decodeURIComponent(page.url().replace(/^.*\/sessions\//, ''))
  const resp = await page.request.get(`/api/v1/sessions/${sessionId}`)
  expect(resp.ok()).toBe(true)
  const body = (await resp.json()) as { brief: { clarifications: string } }
  expect(body.brief.clarifications).toBe('')
})
