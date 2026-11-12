import { expect, test } from '@playwright/test'
import type { Page } from '@playwright/test'

// D4-7（#234）：全闭环交付用例。T2VA 直通与 FL2VA 帧提交两条路径都走到
// `completed`，并在产物分区里真的复制到最终提示词、真的触发下载。
//
// 真实浏览器 + 真实 FastAPI + 确定性桩管线（serve-e2e.mjs 注入 MINIMAX_FAKE_PIPELINE=1）；
// 真实 LLM 闭环只在 `scripts/accept_d4.py` 手动验收里跑，不进 CI（D4-AC15）。
// SSE 断线重连用例在 `sessions.spec.ts`，与这里同一批 `playwright test` 一起跑。
test.use({ permissions: ['clipboard-read', 'clipboard-write'] })

const T2VA_PLOT = 'E2E T2VA 交付闭环'
const FL2VA_PLOT = 'E2E FL2VA 交付闭环'

// 一张 1x1 PNG：桩管线会把它当首帧素材（读图是假的，但素材上传与绑定是真的）。
const PNG_FIRST = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==',
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

/** 表单创建（自动起跑）：变体显式选，不依赖服务端缺省。 */
async function createSession(page: Page, plot: string, variant: 'T2VA' | 'FL2VA'): Promise<void> {
  await page.goto('/')
  await page.getByRole('button', { name: '新建会话' }).click()
  await page.locator('textarea[name="plot"]').fill(plot)
  await page.locator('select[name="variant"]').selectOption(variant)
  await page.getByRole('button', { name: '创建并开始' }).click()
  await expect(page).toHaveURL(/\/sessions\/.+/)
  await expect(page.getByText('等待人工').first()).toBeVisible({ timeout: 20_000 })
}

/** 打开产物分区并等最终提示词正文落地。 */
async function openArtifacts(page: Page): Promise<void> {
  await page.getByRole('tab', { name: '产物' }).click()
  await expect(page.getByRole('heading', { name: '交付文本' })).toBeVisible()
  // 只认交付文本组：中间产物里的分镜表也含 `[Shot 1]`，全局文本匹配会撞两条。
  const deliverable = page.locator('[data-group="deliverable"]')
  await expect(deliverable).toContainText('H3 视频提示词', {
    timeout: 20_000,
  })
  // 交付「三件套」缺一不可（D4-AC14）：summary 两件曾因阶段 2 不产出而从 CI 漏到
  // 复审（2026-11-12），这里咬住分组文本与两件的复制按钮。
  await expect(deliverable).toContainText('中文摘要')
  await expect(deliverable).toContainText('摘要数据')
  await expect(page.locator('[data-copy="summary-zh.md"]')).toBeVisible()
  await expect(page.locator('[data-copy="summary-zh.json"]')).toBeVisible()
}

/** 复制最终提示词：按钮给出成功反馈，剪贴板里拿到的正是正文。 */
async function copyFinalPrompt(page: Page): Promise<void> {
  const copyButton = page.locator('[data-copy="video-prompt.md"]')
  await expect(copyButton).toBeEnabled()
  await copyButton.click()
  await expect(copyButton).toHaveText('已复制')

  const clipboard = await page.evaluate(() => navigator.clipboard.readText())
  expect(clipboard).toContain('H3 视频提示词')
  expect(clipboard).toContain('[Shot 1] At 00:00.000')
}

test('T2VA 直通：创建 → 审查通过 → 阶段 2 完成 → 复制与下载最终提示词', async ({ page }) => {
  await createSession(page, T2VA_PLOT, 'T2VA')
  // T2VA 只有剧本与分镜表两件中间产物（没有帧词）。
  await page.getByRole('tab', { name: '审查与待办' }).click()
  await expect(page.getByText('分场剧本（桩管线第 1 稿）')).toBeVisible()
  await page.getByRole('button', { name: '通过' }).click()

  // 阶段 2 成功即 completed，没有交付确认闸门。
  await expect(page.getByText('已完成').first()).toBeVisible({ timeout: 30_000 })
  await expect(page.getByRole('button', { name: /确认交付|交付确认/ })).toHaveCount(0)

  await openArtifacts(page)

  // 下载 = content 端点原样落盘（文件名来自产物登记名）。
  const downloadPromise = page.waitForEvent('download')
  await page.locator('[data-download="video-prompt.md"]').click()
  const download = await downloadPromise
  expect(download.suggestedFilename()).toBe('video-prompt.md')

  await copyFinalPrompt(page)
})

test('FL2VA 帧提交：创建 → 审查通过 → 上传真实图片作首帧 → 阶段 2 完成 → 复制提示词', async ({
  page,
}) => {
  await createSession(page, FL2VA_PLOT, 'FL2VA')

  await page.getByRole('tab', { name: '审查与待办' }).click()
  await expect(page.getByText('分场剧本（桩管线第 1 稿）')).toBeVisible()
  await page.getByRole('button', { name: '通过' }).click()

  // 帧链 approve 后停在第二个人工待办；重载拿一份确定性的新快照。
  const sessionId = sessionIdFromUrl(page.url())
  expect(sessionId.length).toBeGreaterThan(0)
  await page.reload()

  await page.getByRole('tab', { name: '素材' }).click()
  await page.locator('#asset-upload').setInputFiles({
    name: 'delivery-first.png',
    mimeType: 'image/png',
    buffer: PNG_FIRST,
  })
  await expect(
    page.locator('.asset__meta strong').filter({ hasText: 'delivery-first.png' }),
  ).toBeVisible()

  await page.getByRole('tab', { name: '审查与待办' }).click()
  await expect(page.getByRole('heading', { name: '提交关键帧' })).toBeVisible()
  await page.locator('select[name="first-frame"]').selectOption({ label: 'delivery-first.png' })
  await page.getByRole('button', { name: '提交帧并继续' }).click()

  await expect(page.getByText('已完成').first()).toBeVisible({ timeout: 30_000 })
  await expect(page.getByRole('heading', { name: '提交关键帧' })).toHaveCount(0)

  await openArtifacts(page)
  await copyFinalPrompt(page)
})
