import { execFileSync } from 'node:child_process'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { expect, test } from '@playwright/test'

// D6-7（#250）：长片路径的浏览器证据。24 秒 → 3 段，走「建会话 → 审查通过 →
// 段列表 → 交第 1 段 → 段进度前进」。
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

const PLOT = 'E2E 24 秒长片分段闭环'

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

test('长片 24 秒：建会话 → 段列表 → 交第 1 段 → 段进度前进', async ({ page }, testInfo) => {
  // 第一段 8 秒（24 ÷ 3）；造件一次约 0.2s，放在用例开头，失败点更靠近浏览器。
  const segmentVideo = testInfo.outputPath('segment-01.mp4')
  writeStubSegmentVideo(segmentVideo, 8)

  await page.goto('/')
  await page.getByRole('button', { name: '新建会话' }).click()
  await page.locator('textarea[name="plot"]').fill(PLOT)
  await page.locator('select[name="variant"]').selectOption('T2VA')
  await page.locator('input[name="duration"]').fill('24')
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

  // 交第 1 段：上传桩造的 mp4（上传即选中）→ 提交。
  await page.locator('#segment-video-upload-1').setInputFiles(segmentVideo)
  const picker = page.locator('select[name="segment-video"]')
  await expect(picker).not.toHaveValue('')
  await page.getByRole('button', { name: '提交视频' }).click()

  // 校验通过后进度前进：1/3 已通过，第 2 段挂出待交视频。
  await expect(page.getByText('已通过 1 / 总数 3')).toBeVisible({ timeout: 30_000 })
  await expect(page.locator('[data-segment-index="2"]')).toContainText('等待交视频')
  await expect(page.locator('[data-segment-index="1"]')).toContainText('已通过')
})
