import { execFileSync } from 'node:child_process'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { expect, test } from '@playwright/test'

// D7-6（#259）：ref 多参路线的浏览器全闭环证据。
//
// 建 ref 会话 → 传两件素材（含容量闸）→ 起跑 → 阶段 1 → 素材循环（第 1 件被拒后同一
// 位置重开、第 2 件一次通过）→ 整片注入卡 → 段 ×2 → 逐段交视频 → 拼接 → completed →
// 换一段回到待办。全程无终端操作。
//
// 真实浏览器 + 真实 FastAPI + 确定性桩管线（serve-e2e.mjs 注入 MINIMAX_FAKE_PIPELINE=1）。
// 要交的 mp4 由桩管线自己用 ffmpeg 现造（不在这份 spec 里另写造件逻辑）；素材图是
// 1×1 PNG，桩不读图，但上传/归属/容量每一环都是真的。
const here = dirname(fileURLToPath(import.meta.url))
const repoRoot = resolve(here, '..', '..')
const python = resolve(
  repoRoot,
  '.venv',
  process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python',
)

const PLOT = 'E2E 多参参考素材闭环'
const REF_SECONDS = 20 // >15 秒拆两段；每段 10 秒（H3 单段 4–15 秒档内）
const SEGMENT_SECONDS = 10

// 1×1 透明 PNG：服务端按魔数嗅探成 Picture，内容对桩没有意义（桩不读图）。
const PNG = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==',
  'base64',
)

// ref 台账按内容判重：每次上传追加一个唯一尾字节（嗅探只看魔数），否则第 2 件
// 会以 asset_duplicate 被拒——那是「同一内容登记两次」，不是被测的容量闸。
function pngFile(seed: number, filename: string) {
  return {
    name: filename,
    mimeType: 'image/png',
    buffer: Buffer.concat([PNG, Buffer.from([seed % 256])]),
  }
}

/** 让桩管线现造一段本段时长的多参合规 mp4（帧数按 24fps 折算）。 */
function writeStubRefSegmentVideo(out: string, seconds: number): void {
  const code = [
    'import sys',
    'from minimax_h3_prompt.api.fake_pipeline import write_ref_segment_video',
    'write_ref_segment_video(sys.argv[1], float(sys.argv[2]))',
  ].join('; ')
  execFileSync(python, ['-c', code, out, String(seconds)], {
    cwd: repoRoot,
    env: { ...process.env, PYTHONPATH: resolve(repoRoot, 'src') },
  })
}

async function createRefSession(page: import('@playwright/test').Page, plot: string) {
  await page.goto('/')
  await page.getByRole('button', { name: '新建会话' }).click()
  await page.locator('textarea[name="plot"]').fill(plot)
  // 选 ref：变体下拉整格隐藏（D7 §4.1，帧形态对多参没有意义）。
  await page.getByRole('radio', { name: /多参参考素材/ }).check()
  await expect(page.locator('select[name="variant"]')).toHaveCount(0)
  await page.locator('input[name="duration"]').fill(String(REF_SECONDS))
  await page.getByRole('button', { name: '创建并开始' }).click()
  await expect(page).toHaveURL(/\/sessions\/.+/)
  return page.url()
}

async function uploadReference(
  page: import('@playwright/test').Page,
  name: string,
  filename: string,
  seed: number,
): Promise<void> {
  await page.locator('#asset-semantic-name').fill(name)
  await page.locator('#asset-upload').setInputFiles(pngFile(seed, filename))
}

test('多参 20 秒：容量闸 → 素材循环（被拒重开）→ 注入卡 → 两段拼接 → 换一段', async ({
  page,
}, testInfo) => {
  await page.context().grantPermissions(['clipboard-read', 'clipboard-write'])

  // -- 容量闸（D7 §8）：9 图是硬上限，第 10 张同步 422，不等到跑起来才炸 --------
  await createRefSession(page, 'E2E 容量闸门')
  await page.getByRole('tab', { name: '素材' }).click()
  for (let index = 1; index <= 9; index += 1) {
    await uploadReference(page, `容量图${index}`, `cap-${index}.png`, index)
    await expect(page.locator('.asset-list .asset')).toHaveCount(index)
  }
  await expect(page.locator('[aria-label="参考素材容量"] li[data-kind="picture"]')).toContainText(
    '图 9/9',
  )
  await uploadReference(page, '容量图10', 'cap-10.png', 10)
  await expect(page.getByRole('alert').filter({ hasText: '上限' })).toBeVisible()
  await expect(page.locator('.asset-list .asset')).toHaveCount(9)

  // -- 正式会话：两件素材、阶段 1、素材循环 ------------------------------------
  await createRefSession(page, PLOT)
  // 零素材可以建会话但起不来（D7 §4.3）：详情页停在 created，等「开始」。
  await expect(page.getByRole('button', { name: '开始' })).toBeVisible({ timeout: 20_000 })

  await page.getByRole('tab', { name: '素材' }).click()
  await uploadReference(page, '橘猫', 'cat.png', 101)
  await expect(page.locator('.asset-list .asset')).toHaveCount(1)
  await uploadReference(page, '台灯', 'lamp.png', 102)
  await expect(page.locator('.asset-list .asset')).toHaveCount(2)
  await expect(page.locator('.asset-list')).toContainText('橘猫')
  await expect(page.locator('.asset-list')).toContainText('picture 1')
  await expect(page.locator('.asset-list')).toContainText('picture 2')

  await page.getByRole('tab', { name: '概览与进度' }).click()
  await page.getByRole('button', { name: '开始' }).click()
  await expect(page.getByText('等待人工').first()).toBeVisible({ timeout: 30_000 })

  await page.getByRole('tab', { name: '审查与待办' }).click()
  await expect(page.getByText('分场剧本（桩管线第 1 稿）')).toBeVisible()
  await page.getByRole('button', { name: '通过' }).click()

  // 第 1 件：出词卡可见、词可复制。
  await expect(page.getByRole('heading', { name: /第 1 件素材图 · 橘猫/ })).toBeVisible({
    timeout: 30_000,
  })
  await expect(page.getByText('桩管线固定词')).toBeVisible()
  await page.getByRole('button', { name: '复制词' }).click()
  await expect(page.getByRole('button', { name: '已复制' })).toBeVisible()

  // 交图 → 校对不通过：同一位置重开，差异清单出现（没有放行键）。
  await page.locator('#asset-image-file').setInputFiles(pngFile(201, 'cat-try-1.png'))
  await page.getByRole('button', { name: '提交并校对' }).click()
  await expect(page.getByRole('heading', { name: '校对结论：不通过' })).toBeVisible({
    timeout: 30_000,
  })
  await expect(page.locator('[aria-label="校对差异"]')).toContainText('差异清单')
  await expect(page.locator('[aria-label="校对差异"]')).toContainText('伞')
  await expect(page.getByRole('heading', { name: /第 1 件素材图 · 橘猫/ })).toBeVisible()

  // 重交 → 通过 → 第 2 件自动出现。
  await page.locator('#asset-image-file').setInputFiles(pngFile(202, 'cat-try-2.png'))
  await page.getByRole('button', { name: '提交并校对' }).click()
  await expect(page.getByRole('heading', { name: /第 2 件素材图 · 台灯/ })).toBeVisible({
    timeout: 30_000,
  })

  // 第 2 件一次通过 → 素材循环收尾 → 阶段 2 → 两段。
  await page.locator('#asset-image-file').setInputFiles(pngFile(203, 'lamp-try-1.png'))
  await page.getByRole('button', { name: '提交并校对' }).click()
  await expect(page.getByRole('heading', { name: /第 2 件素材图 · 台灯/ })).toHaveCount(0, {
    timeout: 30_000,
  })

  // 整片注入卡进产物分区：只读、可复制、没有编辑入口。
  await page.getByRole('tab', { name: '产物' }).click()
  const deliverable = page.locator('section[aria-label="交付文本"]')
  await expect(deliverable).toContainText('injection-card.md', { timeout: 30_000 })
  await expect(deliverable).toContainText('橘猫')
  await expect(deliverable).toContainText('台灯')
  await expect(deliverable.locator('textarea, input')).toHaveCount(0)
  await expect(
    deliverable.locator('article', { hasText: 'injection-card.md' }).getByRole('button'),
  ).toHaveText('复制')

  // 段分区：两段、段内参考素材只读、第 1 段等交视频。
  await page.getByRole('tab', { name: '分段' }).click()
  await expect(page.getByText('已通过 0 / 总数 2')).toBeVisible({ timeout: 30_000 })
  await expect(page.locator('[data-segment-index]')).toHaveCount(2)
  await expect(page.locator('[data-segment-index="1"]')).toContainText('等待交视频')
  await expect(page.locator('[data-segment-index="1"] [data-segment-assets]')).toContainText(
    '本段参考素材',
  )
  await expect(page.locator('[data-segment-index="1"] [data-segment-assets]')).toContainText('橘猫')

  // -- 逐段交视频（桩管线现造的 10 秒段）→ 拼接 -------------------------------
  for (const number of [1, 2]) {
    const video = testInfo.outputPath(`ref-segment-${number}.mp4`)
    writeStubRefSegmentVideo(video, SEGMENT_SECONDS)
    await page.locator(`#segment-video-upload-${number}`).setInputFiles(video)
    const picker = page.locator('select[name="segment-video"]')
    await expect(picker).not.toHaveValue('')
    await page.getByRole('button', { name: '提交视频' }).click()
    await expect(page.getByText(`已通过 ${number} / 总数 2`)).toBeVisible({ timeout: 30_000 })
  }

  // 成片可读：媒体产物里有 final.mp4，content 端点给出真 mp4（ftyp 容器）。
  await expect(page.getByText('已完成').first()).toBeVisible({ timeout: 30_000 })
  await page.getByRole('tab', { name: '产物' }).click()
  await expect(page.locator('section[aria-label="媒体产物"] video')).toHaveCount(1)
  const sessionId = page.url().split('/sessions/')[1]!
  const film = await page.request.get(`/api/v1/artifacts/${sessionId}/final.mp4/content`)
  expect(film.ok()).toBe(true)
  expect(film.headers()['content-type']).toContain('video/mp4')
  expect(
    Buffer.from(await film.body())
      .subarray(4, 8)
      .toString('latin1'),
  ).toBe('ftyp')

  // 换一段（D7 §5.5）：只换视频，段状态回到待办；子集只读行原样还在。
  await page.getByRole('tab', { name: '分段' }).click()
  await page.locator('[data-segment-index="1"]').getByRole('button', { name: '换一段' }).click()
  await expect(page.locator('[data-segment-index="1"]')).toContainText('等待交视频', {
    timeout: 30_000,
  })
  await expect(page.locator('[data-segment-index="1"] [data-segment-assets]')).toContainText('橘猫')
})
