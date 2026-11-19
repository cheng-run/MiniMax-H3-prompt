import { execFileSync } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { expect, test } from '@playwright/test'
import { skipClarifications } from './clarification'

// D7 修订（#279 / #285）：ref 多参路线的浏览器全闭环证据。
//
// 零素材建会话 → 起跑 → 阶段 1 派生 N 件 → 素材循环（第 1 件校对不通过后**改词再掷**、
// 用改过的词重交定稿，再「换一张图」走一遍；第 2 件放弃）→ 补交口选「没有，就这样」→
// 注入卡 → 段 ×2 → 逐段交视频 → 拼接 → completed → 换一段。
//
// 与修订前的差别：起跑前不再上传任何素材（素材集合是阶段 1 的产出），容量条改在
// 补交口落定后按台账现算；「放弃这一件」「换一张图」都走 #283 的真界面按钮。
//
// 真实浏览器 + 真实 FastAPI + 确定性桩管线（serve-e2e.mjs 注入 MINIMAX_FAKE_PIPELINE=1）。
// 桩默认派生 2 件（MINIMAX_FAKE_ASSET_COUNT），第 1 件的第一轮校对必不通过——件号与
// 轮次是确定性的。要交的 mp4 由桩管线自己用 ffmpeg 现造；素材图是 1×1 PNG，桩不读图，
// 但上传/归属/容量每一环都是真的。
const here = dirname(fileURLToPath(import.meta.url))
const repoRoot = resolve(here, '..', '..')
const python = resolve(
  repoRoot,
  '.venv',
  process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python',
)

const PLOT = 'E2E 多参参考素材闭环'
const REF_SECONDS = 20 // >15 秒拆两段；每段 10 秒（H3 单段 4–15 秒档内）
const ASSET_COUNT = 2
const ASSET_ONE = '桩素材1'
const ASSET_TWO = '桩素材2'

// 1×1 透明 PNG：服务端按魔数嗅探成 Picture，内容对桩没有意义（桩不读图）。
const PNG = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==',
  'base64',
)

// 上传按内容判重：每次追加一个唯一尾字节（嗅探只看魔数），否则重交会以
// asset_duplicate 被拒——那是「同一内容登记两次」，不是被测的循环。
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
  // 「创建并开始」：ref 零素材直接起跑（#279 删掉了 asset_required 门槛）。
  await page.getByRole('button', { name: '创建并开始' }).click()
  // 起步澄清（#339）：桩管线也会问，面板展开后跳过，创建照旧一步到位。
  await skipClarifications(page)
  await expect(page).toHaveURL(/\/sessions\/.+/)
  return page.url()
}

/** 交一张生成结果并等结果落定：不通过时卡片留在同一件，通过时换下一件。 */
async function submitAssetImage(
  page: import('@playwright/test').Page,
  seed: number,
  filename: string,
): Promise<void> {
  await page.locator('#asset-image-file').setInputFiles(pngFile(seed, filename))
  await page.getByRole('button', { name: '提交并校对' }).click()
}

/** 会话目录：数据根（``H3_DATA_ROOT``，未设则仓库根）下的 ``output/sessions/<标识>``。 */
function sessionDirectory(sessionId: string): string {
  const root = process.env.H3_DATA_ROOT ? resolve(process.env.H3_DATA_ROOT) : repoRoot
  return join(root, 'output', 'sessions', sessionId)
}

test('多参 20 秒：零素材起跑 → 派生两件 → 循环（拒→改词再掷→重交→换图→放弃）→ 补交口 → 注入卡 → 两段拼接 → 换一段', async ({
  page,
}, testInfo) => {
  await page.context().grantPermissions(['clipboard-read', 'clipboard-write'])

  // -- 零素材建会话并起跑：阶段 1 自己定「这片要哪几件」 --------------------------
  await createRefSession(page, PLOT)
  await expect(page.getByText('等待人工').first()).toBeVisible({ timeout: 30_000 })

  // 清单已派生（#279）：起跑前一张图都没交过，素材分区已经有 N 行，且没有上传框。
  await page.getByRole('tab', { name: '素材' }).click()
  await expect(page.locator('.asset-list .asset')).toHaveCount(ASSET_COUNT, { timeout: 20_000 })
  await expect(page.getByTestId('assets-summary')).toContainText(`本片共 ${ASSET_COUNT} 件参考素材`)
  await expect(page.locator('.asset[data-number="1"]')).toContainText(ASSET_ONE)
  await expect(page.locator('.asset[data-number="2"]')).toContainText(ASSET_TWO)
  await expect(page.getByRole('tabpanel').locator('input')).toHaveCount(0)

  // -- 审查通过 → 第 1 件的待办卡 ------------------------------------------------
  await page.getByRole('tab', { name: '审查与待办' }).click()
  await expect(page.getByText('分场剧本（桩管线第 1 稿）')).toBeVisible()
  await page.getByRole('button', { name: '通过' }).click()

  // 第 1 件：出词卡可见、词可复制。
  await expect(
    page.getByRole('heading', { name: new RegExp(`第 1 件素材图 · ${ASSET_ONE}`) }),
  ).toBeVisible({ timeout: 30_000 })
  await expect(page.getByText('桩管线固定词')).toBeVisible()
  await page.getByRole('button', { name: '复制词' }).click()
  await expect(page.getByRole('button', { name: '已复制' })).toBeVisible()

  // 交图 → 校对不通过：同一位置重开，差异清单与四条出路都出现。
  await submitAssetImage(page, 201, 'asset-1-try-1.png')
  await expect(page.getByRole('heading', { name: '校对结论：不通过' })).toBeVisible({
    timeout: 30_000,
  })
  await expect(page.locator('[aria-label="校对差异"]')).toContainText('差异清单')
  await expect(page.locator('[aria-label="校对差异"]')).toContainText('伞')
  await expect(
    page.getByRole('heading', { name: new RegExp(`第 1 件素材图 · ${ASSET_ONE}`) }),
  ).toBeVisible()

  // 四选齐备（#289）：两掷两定，任何一条都能让这一件往前走。
  const exits = page.locator('[aria-label="校对不通过后的出路"]')
  await expect(exits.getByRole('button', { name: '改词强化原意再掷（默认）' })).toBeVisible()
  await expect(exits.getByRole('button', { name: '原词再掷' })).toBeVisible()
  await expect(exits.getByRole('button', { name: '按图改文案（只改描述）' })).toBeVisible()
  await expect(exits.getByRole('button', { name: '明知故犯保留原描述' })).toBeVisible()

  // 默认出路：按偏差清单做加法改词（后台作业），卡片带着新词重开。
  await page.getByRole('button', { name: '改词强化原意再掷（默认）' }).click()
  await expect(page.getByRole('heading', { name: '校对结论：不通过' })).toHaveCount(0, {
    timeout: 30_000,
  })
  await expect(page.getByText('（桩管线改词）')).toBeVisible({ timeout: 30_000 })
  await expect(
    page.getByRole('heading', { name: new RegExp(`第 1 件素材图 · ${ASSET_ONE}`) }),
  ).toBeVisible()

  // 改写留痕（spec #286 US21/US28）：会话下真的有一本资产轮次账，且记的就是这一轮。
  // 账与产物是同一次作业里前后两笔写的，界面可能先看到新词——所以按「账出现」轮询，
  // 而不是拿一次读盘的结果当结论。URL 里的会话标识是百分号编码的，落盘路径要用
  // 解码后的形态（中文语义名与会话主题都在里面）。
  const sessionId = page.url().split('/sessions/')[1]!
  const roundsPath = join(sessionDirectory(decodeURIComponent(sessionId)), 'asset-rounds.jsonl')
  const readRounds = (): { name: string; trigger: string; prompt_after: string }[] => {
    try {
      return readFileSync(roundsPath, 'utf-8')
        .split('\n')
        .filter((line) => line.trim())
        .map((line) => JSON.parse(line) as { name: string; trigger: string; prompt_after: string })
    } catch {
      return []
    }
  }
  await expect.poll(() => readRounds().length, { timeout: 30_000 }).toBeGreaterThan(0)
  const rounds = readRounds()
  expect(rounds.some((row) => row.name === ASSET_ONE)).toBe(true)
  expect(rounds[rounds.length - 1]!.trigger).toBe('regen_ai_reword')
  expect(rounds[rounds.length - 1]!.prompt_after).toContain('（桩管线改词）')

  // 用改过的词重交 → 通过 → 第 2 件自动出现。
  await submitAssetImage(page, 202, 'asset-1-try-2.png')
  await expect(
    page.getByRole('heading', { name: new RegExp(`第 2 件素材图 · ${ASSET_TWO}`) }),
  ).toBeVisible({ timeout: 30_000 })

  // -- 换一张图（循环内、同一件重开）：第 1 件退回待交，重走一次校对 ---------------
  await page.getByRole('tab', { name: '素材' }).click()
  const firstRow = page.locator('.asset[data-number="1"]')
  await expect(firstRow).toHaveAttribute('data-status', 'passed')
  await expect(firstRow).toContainText('已定稿')
  await firstRow.getByRole('button', { name: '换一张图' }).click()
  // 重开后第 1 件重新成为当前件（第 2 件的卡片被收走）。
  await expect(firstRow).toHaveAttribute('data-status', 'pending', { timeout: 20_000 })
  await expect(firstRow).toHaveAttribute('data-current', 'true')

  await page.getByRole('tab', { name: '审查与待办' }).click()
  await expect(
    page.getByRole('heading', { name: new RegExp(`第 1 件素材图 · ${ASSET_ONE}`) }),
  ).toBeVisible({ timeout: 30_000 })
  await submitAssetImage(page, 203, 'asset-1-try-3.png')
  await expect(
    page.getByRole('heading', { name: new RegExp(`第 2 件素材图 · ${ASSET_TWO}`) }),
  ).toBeVisible({ timeout: 30_000 })

  // -- 放弃第 2 件：确认里摊开代价，确认后卡片消失 --------------------------------
  await page.getByRole('button', { name: '放弃这一件' }).click()
  await expect(page.locator('[aria-label="放弃这一件"]')).toContainText(
    `放弃的是「${ASSET_TWO}」这一件图`,
  )
  await expect(page.locator('[aria-label="放弃这一件"]')).toContainText(
    '已经定稿的文本不会被自动改写',
  )
  await page.getByRole('button', { name: '确认放弃' }).click()

  // -- 补交口（循环后、阶段 2 前）：明确选「没有，就这样」 -------------------------
  await expect(page.getByRole('heading', { name: '素材视频 / 音频补交' })).toBeVisible({
    timeout: 30_000,
  })
  await page.getByRole('button', { name: '没有，就这样' }).click()
  await expect(page.getByRole('heading', { name: '素材视频 / 音频补交' })).toHaveCount(0, {
    timeout: 30_000,
  })

  // 补交口落定后容量按台账现算（#279 §8）：放弃件不计入，1 件定稿图、0 视频/音频。
  await page.getByRole('tab', { name: '素材' }).click()
  const secondRow = page.locator('.asset[data-number="2"]')
  await expect(secondRow).toHaveAttribute('data-status', 'abandoned')
  await expect(secondRow).toContainText('已放弃')
  await expect(secondRow.locator('img')).toHaveCount(0)
  await expect(page.getByTestId('assets-summary')).toContainText('已定稿 1 件')
  await expect(page.getByTestId('assets-summary')).toContainText('已放弃 1 件')
  const capacity = page.locator('[aria-label="参考素材容量"]')
  await expect(capacity.locator('li[data-kind="picture"]')).toContainText('图 1/9')
  await expect(capacity.locator('li[data-kind="video"]')).toContainText('视频 0/3')
  await expect(capacity.locator('li[data-kind="audio"]')).toContainText('音频 0/3')
  await expect(capacity.locator('li[data-kind="files"]')).toContainText('文件 1/12')

  // -- 整片注入卡进产物分区：只读、可复制，且只含已定稿的那一件 --------------------
  await page.getByRole('tab', { name: '产物' }).click()
  const deliverable = page.locator('section[aria-label="交付文本"]')
  await expect(deliverable).toContainText('injection-card.md', { timeout: 30_000 })
  await expect(deliverable).toContainText(ASSET_ONE)
  // 放弃件没有图，写进卡就是一个填不上的槽（#279 US16）。
  await expect(deliverable).not.toContainText(ASSET_TWO)
  await expect(deliverable.locator('textarea, input')).toHaveCount(0)
  await expect(
    deliverable.locator('article', { hasText: 'injection-card.md' }).getByRole('button'),
  ).toHaveText('复制')

  // -- 段分区：两段、段内参考素材只读且不含放弃件，第 1 段等交视频 ------------------
  await page.getByRole('tab', { name: '分段' }).click()
  await expect(page.getByText('已通过 0 / 总数 2')).toBeVisible({ timeout: 30_000 })
  await expect(page.locator('[data-segment-index]')).toHaveCount(2)
  await expect(page.locator('[data-segment-index="1"]')).toContainText('等待交视频')
  const segmentAssets = page.locator('[data-segment-index="1"] [data-segment-assets]')
  await expect(segmentAssets).toContainText('本段参考素材')
  await expect(segmentAssets).toContainText(ASSET_ONE)
  // 定义逐段重复：两段都带同一个 Subject 的接线，但放弃件不进任何一段的子集。
  await expect(page.locator('[data-segment-index="2"] [data-segment-assets]')).toContainText(
    ASSET_ONE,
  )
  await expect(page.getByRole('tabpanel')).not.toContainText(ASSET_TWO)

  // -- 逐段交视频（按快照里的段长现造）→ 拼接 ------------------------------------
  const detail = (await (await page.request.get(`/api/v1/sessions/${sessionId}`)).json()) as {
    segments: { index: number; start_s: number; end_s: number }[]
  }
  expect(detail.segments).toHaveLength(2)
  for (const segment of detail.segments) {
    const video = testInfo.outputPath(`ref-segment-${segment.index}.mp4`)
    writeStubRefSegmentVideo(video, segment.end_s - segment.start_s)
    await page.locator(`#segment-video-upload-${segment.index}`).setInputFiles(video)
    const picker = page.locator('select[name="segment-video"]')
    await expect(picker).not.toHaveValue('')
    await page.getByRole('button', { name: '提交视频' }).click()
    await expect(page.getByText(`已通过 ${segment.index} / 总数 2`)).toBeVisible({
      timeout: 30_000,
    })
  }

  // 成片可读：媒体产物里有 final.mp4，content 端点给出真 mp4（ftyp 容器）。
  await expect(page.getByText('已完成').first()).toBeVisible({ timeout: 30_000 })
  await page.getByRole('tab', { name: '产物' }).click()
  await expect(page.locator('section[aria-label="媒体产物"] video')).toHaveCount(1)
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
  await expect(page.locator('[data-segment-index="1"] [data-segment-assets]')).toContainText(
    ASSET_ONE,
  )
})
