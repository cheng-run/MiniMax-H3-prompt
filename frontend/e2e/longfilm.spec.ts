import { execFileSync } from 'node:child_process'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { expect, test } from '@playwright/test'

// D6-7（#250 / #273）：长片路径的浏览器证据。18 秒 → 3 段 × 6 秒（H3 只有 4/5/6 秒档），
// 走「建会话 → 审查通过 → 段列表 → 三段全部交完 → 自动拼接 → completed」，
// 并联合断言语义提示只提示不阻断、步骤带的分段循环 x/y、final.mp4 产物与会话完成态。
//
// 真实浏览器 + 真实 FastAPI + 确定性桩管线（serve-e2e.mjs 注入 MINIMAX_FAKE_PIPELINE=1）。
// 要交的 mp4 由桩管线自己用 ffmpeg 现造（不在这份 spec 里另写一套造件逻辑），这样
// 「用户去 ComfyUI 跑」这一步在 CI 里被替身掉，上传/提交/校验/推进每一环都是真的。
const here = dirname(fileURLToPath(import.meta.url))
const repoRoot = resolve(here, '..', '..')
const python = resolve(
  repoRoot,
  '.venv',
  process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python',
)

const PLOT = 'E2E 18 秒长片分段闭环'

/** 让桩管线现造一段本段时长的合规 mp4（帧数/尺寸从仓内常量派生）。 */
function writeStubSegmentVideo(out: string, seconds: number): void {
  const code = [
    'import sys',
    'from minimax_h3_prompt.api.fake_pipeline import write_segment_video',
    'write_segment_video(sys.argv[1], float(sys.argv[2]))',
  ].join('; ')
  execFileSync(python, ['-c', code, out, String(seconds)], {
    cwd: repoRoot,
    env: { ...process.env, PYTHONPATH: resolve(repoRoot, 'src') },
  })
}

test('长片 18 秒：建会话 → 段列表 → 三段全部交完 → 拼接 completed', async ({ page }, testInfo) => {
  // 每段 6 秒（18 ÷ 3，H3 的 6 秒档 = 158 帧）；造件一次约 0.2s。
  // 同一份文件可以交给不同段：服务端按内容哈希去重，不会写第二份素材。
  const segmentVideo = testInfo.outputPath('segment-06.mp4')
  writeStubSegmentVideo(segmentVideo, 6)

  await page.goto('/')
  await page.getByRole('button', { name: '新建会话' }).click()
  await page.locator('textarea[name="plot"]').fill(PLOT)
  await page.locator('select[name="variant"]').selectOption('T2VA')
  await page.locator('input[name="duration"]').fill('18')
  await page.getByRole('button', { name: '创建并开始' }).click()
  await expect(page).toHaveURL(/\/sessions\/.+/)
  await expect(page.getByText('等待人工').first()).toBeVisible({ timeout: 20_000 })

  // 审查通过 → 阶段 2 → 分段规划 → 写第 1 段；长片这时才多出「分段」分区。
  await page.getByRole('tab', { name: '审查与待办' }).click()
  await expect(page.getByText('分场剧本（桩管线第 1 稿）')).toBeVisible()
  await page.getByRole('button', { name: '通过' }).click()

  const segmentsTab = page.getByRole('tab', { name: '分段' })
  await expect(segmentsTab).toBeVisible({ timeout: 30_000 })
  await segmentsTab.click()

  // 段列表如实显示总段数，进度用「已通过 x / 总数 y」。
  await expect(page.getByText('已通过 0 / 总数 3')).toBeVisible()
  await expect(page.locator('[data-segment-index]')).toHaveCount(3)
  await expect(page.locator('[data-segment-index="1"]')).toContainText('等待交视频')

  // #272：概览顶部的步骤带按 base 长片列出全部步骤；分段循环只显示 x/y，
  // 不按段数展开（这里 3 段也只有一行）。
  await page.getByRole('tab', { name: '概览与进度' }).click()
  const band = page.getByRole('navigation', { name: '步骤带' })
  await expect(band).toBeVisible()
  await expect(band).toContainText('阶段 1')
  await expect(band).toContainText('分段规划')
  await expect(band).toContainText('拼接')
  await expect(band.locator('[data-step]')).toHaveCount(7)
  await expect(band.locator('[data-step="segment_loop"]')).toHaveAttribute('data-state', 'current')
  await expect(band.locator('[data-step="segment_loop"]')).toContainText('0/3')

  await segmentsTab.click()

  // 三段全部交完：上传桩造的 mp4（上传即选中）→ 提交，等进度前进到下一段。
  for (const number of [1, 2, 3]) {
    await expect(page.locator(`[data-segment-index="${number}"]`)).toContainText('等待交视频')
    await page.locator(`#segment-video-upload-${number}`).setInputFiles(segmentVideo)
    const picker = page.locator('select[name="segment-video"]')
    await expect(picker).not.toHaveValue('')
    await page.getByRole('button', { name: '提交视频' }).click()
    await expect(page.getByText(`已通过 ${number} / 总数 3`)).toBeVisible({ timeout: 30_000 })

    if (number === 1) {
      // 语义检查只提示、不阻断（#270 / ADR 0039）：桩判据对第 1 段固定产出一条差异说明，
      // 段照样通过，提示仍挂在段卡片上。
      await expect(page.locator('[data-segment-index="1"] [data-semantic-warning]')).toContainText(
        '语义检查提示',
      )

      // 步骤带的分段循环跟着前进到 1/3。
      await page.getByRole('tab', { name: '概览与进度' }).click()
      await expect(band.locator('[data-step="segment_loop"]')).toContainText('1/3')
      await segmentsTab.click()
    }
  }

  // 最后一段通过 → 服务端自动拼接 → 会话 completed，步骤带停在 3/3 且全部 done。
  await expect(page.getByText('已完成').first()).toBeVisible({ timeout: 30_000 })
  await expect(page.locator('[data-segment-index="3"]')).toContainText('已通过')

  await page.getByRole('tab', { name: '概览与进度' }).click()
  await expect(band.locator('[data-step="segment_loop"]')).toContainText('3/3')
  await expect(band.locator('[data-step="segment_loop"]')).toHaveAttribute('data-state', 'done')
  await expect(band.locator('[data-step="complete"]')).toHaveAttribute('data-state', 'done')

  // final.mp4 真的产出了：产物分区可见，content 端点给出真 mp4（ftyp 容器）。
  await page.getByRole('tab', { name: '产物' }).click()
  const media = page.locator('section[aria-label="媒体产物"]')
  await expect(media).toContainText('final.mp4')
  await expect(media.locator('video')).toHaveCount(1)
  const sessionId = page.url().split('/sessions/')[1]!
  const film = await page.request.get(`/api/v1/artifacts/${sessionId}/final.mp4/content`)
  expect(film.ok()).toBe(true)
  expect(film.headers()['content-type']).toContain('video/mp4')
  expect(
    Buffer.from(await film.body())
      .subarray(4, 8)
      .toString('latin1'),
  ).toBe('ftyp')
})
