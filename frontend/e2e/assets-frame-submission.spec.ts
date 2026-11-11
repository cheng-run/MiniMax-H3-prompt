import { expect, test } from '@playwright/test'
import type { Page } from '@playwright/test'

// D4-6（#233）：真实浏览器 + 真实 FastAPI，管线走 serve-e2e.mjs 注入的确定性桩。
// 覆盖「上传素材 → 选首/尾帧槽 → 提交 → 阶段 2 完成」。
//
// 审查通过（#232 的待办卡）不在本票范围，这里用页面内 API 推进到 frame_submission
// 待办；之后一切交互都走业务界面。

const PLOT = 'E2E 素材与帧提交'

// 两张 1x1 PNG（内容不同，避免被素材按内容哈希去重成同一份）。
const PNG_FIRST = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==',
  'base64',
)
const PNG_LAST = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==',
  'base64',
)

/** 从工作台深链取回会话标识（路由里是逐段百分号编码的文本，先解回来）。 */
function sessionIdFromUrl(rawUrl: string): string {
  const path = new URL(rawUrl).pathname
  return path
    .replace(/^\/sessions\//, '')
    .split('/')
    .map(decodeURIComponent)
    .join('/')
}

/** 用页面内 API 通过阶段 1 审查：本票只接帧提交卡，审查卡归 #232。 */
async function approveStage1(page: Page, sessionId: string): Promise<void> {
  await page.evaluate(async (sid) => {
    const encode = (value: string) => value.split('/').map(encodeURIComponent).join('/')
    const boot = await fetch('/api/v1/auth/token', { cache: 'no-store' })
    if (!boot.ok) {
      throw new Error(`引导令牌失败：${boot.status}`)
    }
    const { token } = (await boot.json()) as { token: string }
    const headers = { 'X-Workbench-Token': token }
    const detail = (await fetch(`/api/v1/sessions/${encode(sid)}`, { headers }).then((r) =>
      r.json(),
    )) as {
      action_items?: { item_id: string; kind: string; status: string }[]
    }
    const item = detail.action_items?.find(
      (entry) => entry.kind === 'stage1_review' && entry.status === 'pending',
    )
    if (!item) {
      throw new Error('没有待审查的 stage1_review 待办')
    }
    const resolved = await fetch(
      `/api/v1/sessions/${encode(sid)}/action-items/${encodeURIComponent(item.item_id)}/resolve`,
      {
        method: 'POST',
        headers: { ...headers, 'Content-Type': 'application/json' },
        body: JSON.stringify({ decision: 'approve' }),
      },
    )
    if (!resolved.ok) {
      throw new Error(`审查通过失败：${resolved.status}`)
    }
  }, sessionId)
}

test('素材上传 → 首尾帧选择 → 提交 → 阶段 2 完成', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('button', { name: '新建会话' }).click()
  await page.locator('textarea[name="plot"]').fill(PLOT)
  await expect(page.locator('select[name="variant"]')).toHaveValue('FL2VA')
  await page.getByRole('button', { name: '创建并开始' }).click()

  await expect(page).toHaveURL(/\/sessions\/.+/)
  await expect(page.getByText('等待人工')).toBeVisible({ timeout: 20_000 })

  const sessionId = sessionIdFromUrl(page.url())
  await approveStage1(page, sessionId)

  // 帧提交待办由快照驱动；重载拿一份确定性的新快照（SSE 也会推同一次失效）。
  await page.reload()
  await page.getByRole('tab', { name: '素材' }).click()

  // 上传两份图片素材（D3 端点）：文件选择走真实 <input type=file>。
  const upload = page.locator('#asset-upload')
  await upload.setInputFiles({ name: 'frame-first.png', mimeType: 'image/png', buffer: PNG_FIRST })
  await expect(
    page.locator('.asset__meta strong').filter({ hasText: 'frame-first.png' }),
  ).toBeVisible()
  await upload.setInputFiles({ name: 'frame-last.png', mimeType: 'image/png', buffer: PNG_LAST })
  await expect(
    page.locator('.asset__meta strong').filter({ hasText: 'frame-last.png' }),
  ).toBeVisible()

  // 媒体元素由浏览器自己请求，只带 Cookie；能解码出自然尺寸才算预览真的通了。
  const firstPreview = page.locator('img[alt="frame-first.png"]')
  await expect(firstPreview).toBeVisible()
  await expect
    .poll(() => firstPreview.evaluate((element: HTMLImageElement) => element.naturalWidth))
    .toBeGreaterThan(0)
  await expect(page.locator('img[alt="frame-last.png"]')).toBeVisible()

  // 帧提交卡：展示首/尾帧提示词，帧槽只列本会话素材。
  await page.getByRole('tab', { name: '审查与待办' }).click()
  await expect(page.getByRole('heading', { name: '提交关键帧' })).toBeVisible()
  await expect(page.getByRole('heading', { name: '首帧提示词' })).toBeVisible()
  await expect(page.getByRole('heading', { name: '尾帧提示词' })).toBeVisible()

  await page.locator('select[name="first-frame"]').selectOption({ label: 'frame-first.png' })
  await page.locator('select[name="last-frame"]').selectOption({ label: 'frame-last.png' })
  await page.getByRole('button', { name: '提交帧并继续' }).click()

  // 提交后不再有本地轮询：状态由实时通道（SSE 失效信号）推着走到终点。
  await expect(page.getByText('已完成')).toBeVisible({ timeout: 30_000 })
  await expect(page.getByRole('heading', { name: '提交关键帧' })).toHaveCount(0)
})
