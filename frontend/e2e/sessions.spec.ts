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
  // 事件流请求在进入工作台后才发：先挂监听，再点创建（自动起跑）。
  const eventsRequest = page.waitForRequest((request) => request.url().includes('/events'))
  await page.getByRole('button', { name: '创建并开始' }).click()

  // 两请求连做：POST /sessions 201 后立刻 POST /jobs，然后跳详情。
  await expect(page).toHaveURL(/\/sessions\/.+/)
  await expect(page.getByRole('heading', { level: 1 })).toContainText(AUTO_PLOT)

  // D4-AC5：SSE 走 fetch，令牌只在请求头——URL 里任何位置都没有 token。
  const events = await eventsRequest
  expect(events.url()).not.toContain('token')
  expect(events.headers()['x-workbench-token']).toBeTruthy()
  // 订阅真的建立了（不是 401/404 的短响应）：流式响应的头先到，response() 不必等流结束。
  const eventsResponse = await events.response()
  expect(eventsResponse?.status()).toBe(200)
  expect(eventsResponse?.headers()['content-type']).toContain('text/event-stream')

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

test('四分区：页内 tab 切换不进 URL，刷新回概览', async ({ page }) => {
  await page.goto('/')
  const sessionId = await createSessionWithoutJob(page, 'E2E 四分区校验')
  await page.goto(`/sessions/${encodePath(sessionId)}`)

  const url = page.url()
  const tabs = page.getByRole('tab')
  await expect(tabs).toHaveCount(4)
  await expect(page.getByRole('tab', { name: '概览与进度' })).toHaveAttribute(
    'aria-selected',
    'true',
  )

  await page.getByRole('tab', { name: '审查与待办' }).click()
  await expect(page.getByRole('tab', { name: '审查与待办' })).toHaveAttribute(
    'aria-selected',
    'true',
  )
  await expect(page.getByRole('tabpanel')).toContainText('暂无待办')
  // 分区是页内状态：URL 一个字符都没变（D4-AC4）。
  expect(page.url()).toBe(url)

  // 刷新 = 重新挂载 + 新的 Pinia：分区选择不落盘，回到概览，深链本身仍然有效。
  await page.reload()
  await expect(page).toHaveURL(url)
  await expect(page.getByRole('tab', { name: '概览与进度' })).toHaveAttribute(
    'aria-selected',
    'true',
  )
})

test('SSE 订阅失败会重试：可见提示，恢复后自动重订阅且状态不丢', async ({ page }) => {
  // 掐断事件流请求（真实浏览器里 `context.setOffline` 不会终止已建立的长连接，
  // 这里用路由拦截制造确定性的连接失败；流建立后的断线由组件测试的注入流覆盖）。
  await page.route('**/events', (route) => route.abort())
  await page.goto('/')
  const sessionId = await createSessionWithoutJob(page, 'E2E 断线重连')
  await page.goto(`/sessions/${encodePath(sessionId)}`)

  // 连接失败 → 订阅器按间隔重试并给出可见提示；会话状态仍来自快照，不丢。
  await expect(page.getByText('正在重连')).toBeVisible({ timeout: 10_000 })
  await expect(page.getByText('已创建')).toBeVisible()

  // 放行后下一次重试成功：提示消失（重新订阅，继续接收失效信号）。
  await page.unroute('**/events')
  await expect(page.getByText('正在重连')).toBeHidden({ timeout: 10_000 })
  await expect(page.getByText('已创建')).toBeVisible()
})
