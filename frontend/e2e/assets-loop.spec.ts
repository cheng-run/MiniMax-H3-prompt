import { expect, test } from '@playwright/test'

// 素材分区的三态（D7 修订 #283 / #279「素材分区」）：起跑前只给引导文案，循环里列出
// 全部 N 行并指出「就是这一件在等你」，循环走完后是 N 行 + 补交视频/音频 + 现算容量条。
// 全程只读：没有上传框、没有语义名输入、没有改名改号入口。
//
// 真实浏览器 + 真实 FastAPI + 确定性桩管线（serve-e2e.mjs 注入 MINIMAX_FAKE_PIPELINE=1）。
// 桩默认派生 2 件（MINIMAX_FAKE_ASSET_COUNT），第 1 件的第一轮校对必不通过——件号与
// 轮次是确定性的，不数调用次数。
const PLOT = 'E2E 素材分区三态'
const ASSET_COUNT = 2
// 1×1 透明 PNG：服务端按魔数嗅探成 Picture，内容对桩没有意义（桩不读图）。
const PNG = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==',
  'base64',
)

// 台账按内容判重：追加一个唯一尾字节（嗅探只看魔数），否则重交会以 asset_duplicate 被拒。
function pngFile(seed: number, filename: string) {
  return {
    name: filename,
    mimeType: 'image/png',
    buffer: Buffer.concat([PNG, Buffer.from([seed % 256])]),
  }
}

async function createRefSession(page: import('@playwright/test').Page, plot: string) {
  await page.goto('/')
  await page.getByRole('button', { name: '新建会话' }).click()
  await page.locator('textarea[name="plot"]').fill(plot)
  await page.getByRole('radio', { name: /多参参考素材/ }).check()
  await page.locator('input[name="duration"]').fill('10')
  // 「创建并开始」：ref 零素材直接起跑（#279 删掉了 asset_required 门槛）。
  await page.getByRole('button', { name: '创建并开始' }).click()
  await expect(page).toHaveURL(/\/sessions\/.+/)
}

/** 建会话但**不起跑**：created 态只在「建好还没点开始」时存在，界面入口一建就起跑。 */
async function createIdleRefSession(
  page: import('@playwright/test').Page,
  plot: string,
): Promise<string> {
  // 先打开一次首页：鉴权是 Cookie 认证，page.request 与页面共用一个 context，没走过
  // 页面就没有令牌。
  await page.goto('/')
  const response = await page.request.post('/api/v1/sessions', {
    data: {
      mode: 'ref',
      plot,
      duration: 10,
      variant: 'T2VA',
      style: 'Cinematic',
      language: 'Chinese',
    },
    headers: { 'Idempotency-Key': `e2e-idle-${Date.now()}` },
  })
  expect(response.ok(), await response.text()).toBe(true)
  const detail = (await response.json()) as { session_id: string }
  return detail.session_id
}

/** 直接调 API 放弃当前这件：放弃的按钮不在这张票的范围里（它属于待办卡），这里只验显示。 */
async function abandonCurrentAsset(page: import('@playwright/test').Page): Promise<void> {
  const sessionId = page.url().split('/sessions/')[1]!
  const detail = await page.request.get(`/api/v1/sessions/${sessionId}`)
  expect(detail.ok()).toBe(true)
  const body = (await detail.json()) as {
    action_items: { item_id: string; kind: string; status: string }[]
  }
  const item = body.action_items.find(
    (entry) => entry.kind === 'asset_image' && entry.status === 'pending',
  )
  expect(item, '素材循环里应该有一张在等交图的待办卡').toBeTruthy()
  const response = await page.request.post(
    `/api/v1/sessions/${sessionId}/action-items/${item!.item_id}/resolve`,
    {
      data: { decision: 'abandon_asset' },
      headers: { 'Idempotency-Key': `e2e-abandon-${item!.item_id}` },
    },
  )
  expect(response.ok()).toBe(true)
}

test('created：起跑前只给引导文案，没有上传框（#279 起跑不收图）', async ({ page }) => {
  const sessionId = await createIdleRefSession(page, 'E2E 素材分区引导态')
  await page.goto(`/sessions/${sessionId}`)
  await page.getByRole('tab', { name: '素材' }).click()

  await expect(page.getByTestId('assets-guidance')).toContainText(
    '阶段 1 完成后这里会列出本片需要的',
  )
  await expect(page.locator('#asset-upload')).toHaveCount(0)
  await expect(page.locator('#asset-semantic-name')).toHaveCount(0)
  await expect(page.locator('[aria-label="参考素材容量"]')).toHaveCount(0)
})

test('素材分区：引导态 → 循环中逐件 → 循环后（含放弃件与容量条）', async ({ page }) => {
  // -- 创建即起跑：零素材，阶段 1 自己定件数 ----------------------------------
  await createRefSession(page, PLOT)
  await expect(page.getByText('等待人工').first()).toBeVisible({ timeout: 30_000 })

  // 清单已派生、审查还没通过：N 行都在，但没有任何一件「在等你」（卡片还没挂出来）。
  await page.getByRole('tab', { name: '素材' }).click()
  await expect(page.locator('.asset-list .asset')).toHaveCount(ASSET_COUNT, { timeout: 20_000 })
  await expect(page.getByTestId('assets-summary')).toContainText('本片共 2 件参考素材')
  await expect(page.locator('[data-current="true"]')).toHaveCount(0)
  await expect(page.locator('.asset__goto')).toHaveCount(0)

  // -- 审查通过 → 第 1 件的待办卡出现，素材分区指向它 --------------------------
  await page.getByRole('tab', { name: '审查与待办' }).click()
  await page.getByRole('button', { name: '通过' }).click()
  await expect(page.getByRole('heading', { name: /第 1 件素材图/ })).toBeVisible({
    timeout: 30_000,
  })

  await page.getByRole('tab', { name: '素材' }).click()
  const first = page.locator('.asset[data-number="1"]')
  await expect(first).toHaveAttribute('data-current', 'true')
  await expect(first).toContainText('正在等你交图')
  await expect(page.locator('.asset[data-number="2"]')).toContainText('还没轮到')
  // 只读：分区里没有任何输入框（上传与交图都在待办卡上）。
  await expect(page.getByRole('tabpanel').locator('input')).toHaveCount(0)

  // 「去交这一件」把人送回待办卡。
  await first.getByRole('button', { name: '去交这一件' }).click()
  await expect(page.getByRole('heading', { name: /第 1 件素材图/ })).toBeVisible()

  // -- 第 1 件：桩管线第一轮必不通过，重交后通过 → 第 2 件的卡 ----------------
  await page.locator('#asset-image-file').setInputFiles(pngFile(101, 'girl-1.png'))
  await page.getByRole('button', { name: '提交并校对' }).click()
  await expect(page.getByRole('heading', { name: '校对结论：不通过' })).toBeVisible({
    timeout: 30_000,
  })
  await page.locator('#asset-image-file').setInputFiles(pngFile(102, 'girl-2.png'))
  await page.getByRole('button', { name: '提交并校对' }).click()
  await expect(page.getByRole('heading', { name: /第 2 件素材图/ })).toBeVisible({
    timeout: 30_000,
  })

  // 已定稿的那件带缩略图与「已定稿」，指针移到第 2 件。
  await page.getByRole('tab', { name: '素材' }).click()
  await expect(page.locator('.asset[data-number="1"]')).toHaveAttribute('data-status', 'passed')
  await expect(page.locator('.asset[data-number="1"]')).toContainText('已定稿')
  await expect(page.locator('.asset[data-number="1"] img')).toHaveCount(1)
  await expect(page.locator('.asset[data-number="2"]')).toHaveAttribute('data-current', 'true')

  // -- 放弃第 2 件 → 循环走完：已放弃行没有图，容量只数真正交过的 --------------
  await abandonCurrentAsset(page)
  await page.reload()
  await page.getByRole('tab', { name: '素材' }).click()

  const second = page.locator('.asset[data-number="2"]')
  await expect(second).toHaveAttribute('data-status', 'abandoned', { timeout: 20_000 })
  await expect(second).toContainText('已放弃')
  await expect(second.locator('img')).toHaveCount(0)
  await expect(page.locator('[data-current="true"]')).toHaveCount(0)
  await expect(page.getByTestId('assets-summary')).toContainText('已定稿 1 件')
  await expect(page.getByTestId('assets-summary')).toContainText('已放弃 1 件')

  // 循环后：容量条现身，且放弃件不进用量（1 件图，不是 2 件）。
  const capacity = page.locator('[aria-label="参考素材容量"]')
  await expect(capacity).toBeVisible()
  await expect(capacity.locator('li[data-kind="picture"]')).toContainText('图 1/9')
  await expect(capacity.locator('li[data-kind="video"]')).toContainText('视频 0/3')
  await expect(capacity.locator('li[data-kind="audio"]')).toContainText('音频 0/3')
  await expect(capacity.locator('li[data-kind="files"]')).toContainText('文件 1/12')
})
