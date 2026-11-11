import { expect, test } from '@playwright/test'
import type { Page } from '@playwright/test'

// D4-3（#230）：真实浏览器 + 真实 FastAPI，但管线走 serve-e2e.mjs 注入的确定性桩
// （MINIMAX_FAKE_PIPELINE=1）。覆盖「创建 → 自动起跑 → 刷新恢复」与 created 兜底。

const AUTO_PLOT = 'E2E 自动起跑校验'
const CREATED_PLOT = 'E2E created 兜底'

// 会话标识含斜杠（<slug>/GEN001）：逐段编码，与 API 里 {session_id:path} 的约定一致。
function encodePath(value: string): string {
  return value.split('/').map(encodeURIComponent).join('/')
}

// 绕过表单直接建一个会话、但**不起跑**：用来制造卡在 created 的会话，验证兜底按钮。
// 令牌在头里、幂等键唯一；与 media.spec 的页面内请求同一套路。
async function createSessionWithoutJob(page: Page, plot: string): Promise<string> {
  return page.evaluate(async (plotText) => {
    const boot = await fetch('/api/v1/auth/token', { cache: 'no-store' })
    if (!boot.ok) {
      throw new Error(`引导令牌失败：${boot.status}`)
    }
    const { token } = (await boot.json()) as { token: string }
    const created = await fetch('/api/v1/sessions', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Workbench-Token': token,
        'Idempotency-Key': crypto.randomUUID(),
      },
      body: JSON.stringify({
        mode: 'base',
        plot: plotText,
        duration: 5,
        variant: 'FL2VA',
        style: 'Cinematic',
        language: 'Chinese',
      }),
    })
    if (!created.ok) {
      throw new Error(`建会话失败：${created.status}`)
    }
    const { session_id: sessionId } = (await created.json()) as { session_id: string }
    return sessionId
  }, plot)
}

test('表单创建即自动起跑，跳转工作台且刷新可恢复', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('button', { name: '新建会话' }).click()
  await page.locator('textarea[name="plot"]').fill(AUTO_PLOT)
  await page.getByRole('button', { name: '创建并开始' }).click()

  // 两请求连做：POST /sessions 201 后立刻 POST /jobs，然后跳详情。
  await expect(page).toHaveURL(/\/sessions\/.+/)
  await expect(page.getByRole('heading', { level: 1 })).toContainText(AUTO_PLOT)

  // 自动起跑确实发生：状态离开「已创建」，否则会看到兜底「开始」按钮。
  await expect(page.getByText(/运行中|等待人工|已完成/)).toBeVisible()
  await expect(page.getByText('已创建')).toHaveCount(0)

  // 深链刷新恢复：同一 URL 重载后仍按 session id 重拉快照（不是回列表）。
  const url = page.url()
  await page.reload()
  await expect(page).toHaveURL(url)
  await expect(page.getByRole('heading', { level: 1 })).toContainText(AUTO_PLOT)
  await expect(page.getByText(/运行中|等待人工|已完成/)).toBeVisible()
})

test('created 兜底：刷新不丢，「开始」能把它推起来', async ({ page }) => {
  await page.goto('/')
  const sessionId = await createSessionWithoutJob(page, CREATED_PLOT)
  await page.goto(`/sessions/${encodePath(sessionId)}`)

  await expect(page.getByRole('heading', { level: 1 })).toContainText(CREATED_PLOT)
  await expect(page.getByText('已创建')).toBeVisible()
  await expect(page.getByRole('button', { name: '开始' })).toBeVisible()

  // 刷新后会话仍是 created，兜底按钮还在（不会卡死、也不会丢）。
  await page.reload()
  await expect(page.getByText('已创建')).toBeVisible()
  await expect(page.getByRole('button', { name: '开始' })).toBeVisible()

  await page.getByRole('button', { name: '开始' }).click()
  await expect(page.getByText('运行中').or(page.getByText('等待人工'))).toBeVisible({
    timeout: 15_000,
  })
})
