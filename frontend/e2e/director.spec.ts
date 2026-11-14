import { execFileSync } from 'node:child_process'
import { mkdirSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { expect, test } from '@playwright/test'

// D8-7（#266）：导演台整条路线的浏览器证据。
//
// 真实浏览器 + 真实 FastAPI + 确定性桩管线（serve-e2e.mjs 注入 MINIMAX_FAKE_PIPELINE=1）。
// 走完整闭环：建 director 会话 → 阶段 1 → 开场首帧 + 画幅 → 规划/写段 → 打包下载 →
// 段回交（一条合并导出、一条单段导出）→ 改一段级联 → 交付归档。
//
// 造件一律调仓库 `.venv` 里的桩函数（与 longfilm.spec.ts 同形）：帧数从桩计划现算，
// 不在 TS 里重写一套 ffmpeg 逻辑，也不手抄「第几段多少帧」。
const here = dirname(fileURLToPath(import.meta.url))
const repoRoot = resolve(here, '..', '..')
const python = resolve(
  repoRoot,
  '.venv',
  process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python',
)

const PLOT = 'E2E 导演台三段闭环与交付'

interface Assets {
  exports: number[]
  merged: number[]
}

/** 用桩函数的真实计划造件：三张图 + 三段段视频 + 一条成片。 */
function buildAssets(outDir: string): Assets {
  mkdirSync(outDir, { recursive: true })
  const code = [
    'import json, sys',
    'from pathlib import Path',
    'from PIL import Image',
    'from minimax_h3_prompt.api.fake_pipeline import director_plan, write_director_segment_video',
    'out = Path(sys.argv[1])',
    'plan = director_plan("fl2v", 14.0)',
    'segs = plan.segments',
    'Image.new("RGB", (736, 1280), (30, 40, 60)).save(out / "start.png")',
    'Image.new("RGB", (736, 1280), (60, 30, 40)).save(out / "cut.png")',
    'write_director_segment_video(out / "seg1.mp4", segs[0].export_frames)',
    'write_director_segment_video(out / "seg2-merged.mp4", segs[1].end_frame)',
    'write_director_segment_video(out / "seg3.mp4", segs[2].export_frames)',
    'write_director_segment_video(out / "film.mp4", plan.total_frames)',
    'print(json.dumps({',
    '    "exports": [s.export_frames for s in segs],',
    '    "merged": [s.end_frame for s in segs],',
    '}))',
  ].join('\n')
  const stdout = execFileSync(python, ['-c', code, outDir], {
    cwd: repoRoot,
    env: { ...process.env, PYTHONPATH: resolve(repoRoot, 'src') },
    encoding: 'utf-8',
  })
  return JSON.parse(stdout.trim()) as Assets
}

/** 从下载到的初始包复制一份最终包：改第 index 段的 prompt（交付时应补一条 rewrite）。 */
function makeFinalPack(source: string, target: string, index = 1): void {
  const code = [
    'import json, sys, zipfile',
    'src, dst, index = sys.argv[1], sys.argv[2], int(sys.argv[3])',
    'with zipfile.ZipFile(src) as archive:',
    '    members = {name: archive.read(name) for name in archive.namelist()}',
    'data = json.loads(members["timeline.json"].decode("utf-8"))',
    'rows = data["segments"]',
    'rows[index]["prompt"] = str(rows[index].get("prompt") or "") + " 用户在导演台补了一句：雨更大了。"',
    'members["timeline.json"] = json.dumps(data, ensure_ascii=False).encode("utf-8")',
    'with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as archive:',
    '    for name, blob in members.items():',
    '        archive.writestr(name, blob)',
  ].join('\n')
  execFileSync(python, ['-c', code, source, target, String(index)], {
    cwd: repoRoot,
    env: { ...process.env, PYTHONPATH: resolve(repoRoot, 'src') },
  })
}

async function submitSegmentVideo(
  page: import('@playwright/test').Page,
  index: number,
  file: string,
): Promise<void> {
  await page.getByRole('tab', { name: '分段' }).click()
  // 级联换段后按需词要重写、再挂待办：等本段的上传槽出现（不是凭固定睡眠）。
  await expect(page.locator(`#segment-video-upload-${index}`)).toBeVisible({
    timeout: 30_000,
  })
  await page.locator(`#segment-video-upload-${index}`).setInputFiles(file)
  const picker = page.locator('select[name="segment-video"]')
  await expect(picker).not.toHaveValue('')
  await page.getByRole('button', { name: '提交视频' }).click()
}

test('导演台三段：阶段 1 → 首帧 → 打包下载 → 两种段回交 → 级联 → 交付归档', async ({
  page,
}, testInfo) => {
  // 这条闭环比别的 spec 长：四个桩视频 + 三张图 + 五段回交 + 打包 + 交付。60 秒是
  // 为单步交互写的默认值，覆盖不了一次全片。
  test.setTimeout(300_000)
  const outDir = testInfo.outputPath('assets')
  const assets = buildAssets(outDir)
  // 桩计划固定是 107/107/119：前两段无引导、收尾有引导（导出 119）。
  expect(assets.exports).toEqual([107, 107, 119])
  expect(assets.merged).toEqual([107, 214, 333])

  // --- 建会话 ---
  await page.goto('/')
  await page.getByRole('button', { name: '新建会话' }).click()
  await page.locator('textarea[name="plot"]').fill(PLOT)
  await page.getByRole('radio', { name: '导演台（director）' }).check()
  await page.locator('select[name="director_mode"]').selectOption('fl2v')
  await page.locator('input[name="director_node_id"]').fill('e2e-node-266')
  await page.locator('input[name="duration"]').fill('14')
  await page.getByRole('button', { name: '创建并开始' }).click()
  await expect(page).toHaveURL(/\/sessions\/.+/)

  // --- 阶段 1 审查通过 ---
  await expect(page.getByText('等待人工').first()).toBeVisible({ timeout: 20_000 })
  await page.getByRole('tab', { name: '审查与待办' }).click()
  await expect(page.getByText('分场剧本（桩管线第 1 稿）')).toBeVisible()
  await page.getByRole('button', { name: '通过' }).click()

  // --- 开场首帧（画幅与配置的 736×1280 一致，不需要确认勾选）---
  await expect(page.getByRole('heading', { name: '提交开场首帧' })).toBeVisible({
    timeout: 20_000,
  })
  await page.locator('#director-start-upload').setInputFiles(resolve(outDir, 'start.png'))
  // 上传是异步的：选中值落进下拉之后才算交得上（不然点提交只会被客户端拦成「请先选」）。
  await expect(page.locator('#director-start-asset')).not.toHaveValue('')
  await page.getByRole('button', { name: '提交开场首帧并继续' }).click()

  // --- 规划 / 写段 / 打包：三段表落盘，第 1 段等视频 ---
  await page.getByRole('tab', { name: '分段' }).click()
  await expect(page.getByText('已通过 0 / 总数 3')).toBeVisible({ timeout: 30_000 })
  await expect(page.locator('[data-segment-index="1"]')).toContainText('等待交视频')

  // --- 第 1 段（开场）：单段导出 107 ---
  await submitSegmentVideo(page, 1, resolve(outDir, 'seg1.mp4'))
  await expect(page.getByText('已通过 1 / 总数 3')).toBeVisible({ timeout: 30_000 })

  // --- 第 2 段（切镜）：先交本段首帧图，再交**合并导出**（前 2 段累计 214）---
  await page.getByRole('tab', { name: '审查与待办' }).click()
  await expect(page.getByRole('heading', { name: '第 2 段的段首帧' })).toBeVisible({
    timeout: 30_000,
  })
  await page.locator('#director-segment-image-upload').setInputFiles(resolve(outDir, 'cut.png'))
  await expect(page.locator('#director-segment-image')).not.toHaveValue('')
  await page.getByRole('button', { name: '提交段首帧并继续' }).click()

  await submitSegmentVideo(page, 2, resolve(outDir, 'seg2-merged.mp4'))
  await expect(page.getByText('已通过 2 / 总数 3')).toBeVisible({ timeout: 30_000 })

  // --- 第 3 段（收尾）：单段导出 119 ---
  await submitSegmentVideo(page, 3, resolve(outDir, 'seg3.mp4'))
  await expect(page.getByText('已通过 3 / 总数 3')).toBeVisible({ timeout: 30_000 })

  // --- 改一段级联：换掉第 3 段，下游只有它自己；重出按需词后再交一次 ---
  const thirdSegment = page.locator('[data-segment-index="3"]')
  await expect(thirdSegment).toContainText('已通过')
  await thirdSegment.getByRole('button', { name: '换一段' }).click()
  await expect(page.getByText('已通过 2 / 总数 3')).toBeVisible({ timeout: 30_000 })
  await submitSegmentVideo(page, 3, resolve(outDir, 'seg3.mp4'))
  await expect(page.getByText('已通过 3 / 总数 3')).toBeVisible({ timeout: 30_000 })

  // --- 打包下载：把初始包下载下来，改一段 prompt 造成最终包 ---
  await page.getByRole('tab', { name: '产物' }).click()
  const [download] = await Promise.all([
    page.waitForEvent('download'),
    page.locator('[data-download="director/initial.mmxpack.zip"]').click(),
  ])
  const initialPack = testInfo.outputPath('initial.mmxpack.zip')
  await download.saveAs(initialPack)
  const finalPack = testInfo.outputPath('final.mmxpack.zip')
  makeFinalPack(initialPack, finalPack)

  // --- 交付归档：成片 + 最终包 ---
  await page.getByRole('tab', { name: '交付' }).click()
  await expect(page.getByText('初始包＝计划，最终包＝结论', { exact: false })).toBeVisible()
  await page.locator('#delivery-film-upload').setInputFiles(resolve(outDir, 'film.mp4'))
  // 卡内一次只允许一个上传在进行：等成片上完再交包，否则第二个 change 会被丢掉。
  await expect(page.getByText('已选：').first()).toBeVisible({ timeout: 30_000 })
  await expect(page.locator('#delivery-film-upload')).toBeEnabled()
  await page.locator('#delivery-pack-upload').setInputFiles(finalPack)
  await expect(page.getByText('已选：')).toHaveCount(2, { timeout: 30_000 })
  const submitDelivery = page.getByRole('button', {
    name: '提交成片与最终包并归档',
  })
  await expect(submitDelivery).toBeEnabled({ timeout: 30_000 })
  await submitDelivery.click()

  // 归档完成：三件齐备 + 改写条数可见。
  await expect(page.getByText('已交付').first()).toBeVisible({ timeout: 30_000 })
  const result = page.locator('[data-delivery-result]')
  await expect(result).toContainText('final.mmxpack.zip')
  await expect(result).toContainText('final-film.json')
  await expect(result.locator('[data-delivery-detail]')).toContainText('改写账')
  await expect(result.locator('[data-delivery-detail]')).toContainText('1 行')

  // 产物分区：最终包可下载、成片记录是文本。
  await page.getByRole('tab', { name: '产物' }).click()
  await expect(page.locator('[data-download="director/final.mmxpack.zip"]')).toBeVisible()
  await expect(page.locator('[data-download="director/final-film.json"]')).toBeVisible()
})

// ---------------------------------------------------------------------------
// D8-8（#268）：r2v 复用 D7 的素材台账与素材循环
// ---------------------------------------------------------------------------

// 1×1 透明 PNG：服务端按魔数嗅探成 Picture（桩不读图）；每次追加一个唯一尾字节，
// 否则台账按内容判重、第 2 件会以 asset_duplicate 被拒——那不是被测的东西。
const R2V_PNG = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==',
  'base64',
)

function r2vPng(seed: number, filename: string) {
  return {
    name: filename,
    mimeType: 'image/png',
    buffer: Buffer.concat([R2V_PNG, Buffer.from([seed % 256])]),
  }
}

/** r2v 桩件：本段导出视频 + 整片视频，帧数从真实段表现算（单段 r2v 计划）。 */
function buildR2vVideos(outDir: string): { export: number; total: number } {
  mkdirSync(outDir, { recursive: true })
  const code = [
    'import json, sys',
    'from pathlib import Path',
    'from minimax_h3_prompt.api.fake_pipeline import director_plan, write_director_segment_video',
    'out = Path(sys.argv[1])',
    'plan = director_plan("r2v", 4.0)',
    'write_director_segment_video(out / "seg1.mp4", plan.segments[0].export_frames)',
    'write_director_segment_video(out / "film.mp4", plan.total_frames)',
    'print(json.dumps({"export": plan.segments[0].export_frames, "total": plan.total_frames}))',
  ].join('\n')
  const stdout = execFileSync(python, ['-c', code, outDir], {
    cwd: repoRoot,
    env: { ...process.env, PYTHONPATH: resolve(repoRoot, 'src') },
    encoding: 'utf-8',
  })
  return JSON.parse(stdout.trim()) as { export: number; total: number }
}

async function uploadR2vReference(
  page: import('@playwright/test').Page,
  name: string,
  filename: string,
  seed: number,
): Promise<void> {
  await page.locator('#asset-semantic-name').fill(name)
  await page.locator('#asset-upload').setInputFiles(r2vPng(seed, filename))
}

test('导演台 r2v：台账素材 → 素材循环（被拒重开）→ 规划/打包 → 段视频 → 交付', async ({
  page,
}, testInfo) => {
  // 素材循环 + 规划 + 打包 + 交付，比单页交互长得多。
  test.setTimeout(300_000)
  const outDir = testInfo.outputPath('r2v-assets')
  const frames = buildR2vVideos(outDir)
  expect(frames.total).toBe(frames.export)

  // --- 建会话：零素材可以建，起不来（台账起跑闸门）---
  await page.goto('/')
  await page.getByRole('button', { name: '新建会话' }).click()
  await page.locator('textarea[name="plot"]').fill('E2E 导演台 r2v 参考素材闭环')
  await page.getByRole('radio', { name: '导演台（director）' }).check()
  await page.locator('select[name="director_mode"]').selectOption('r2v')
  await page.locator('input[name="director_node_id"]').fill('e2e-node-268')
  await page.locator('input[name="duration"]').fill('4')
  await page.getByRole('button', { name: '创建并开始' }).click()
  await expect(page).toHaveURL(/\/sessions\/.+/)
  await expect(page.getByRole('button', { name: '开始' })).toBeVisible({ timeout: 20_000 })

  // --- 素材：语义名必填、编号按上传序（r2v 与 ref 同一份界面）---
  await page.getByRole('tab', { name: '素材' }).click()
  await uploadR2vReference(page, '橘猫', 'cat.png', 11)
  await expect(page.locator('.asset-list .asset')).toHaveCount(1)
  await uploadR2vReference(page, '台灯', 'lamp.png', 12)
  await expect(page.locator('.asset-list')).toContainText('picture 2')

  // --- 起跑 → 阶段 1 通过 → **直接进素材循环**（不挂开场首帧待办）---
  await page.getByRole('tab', { name: '概览与进度' }).click()
  await page.getByRole('button', { name: '开始' }).click()
  await expect(page.getByText('等待人工').first()).toBeVisible({ timeout: 30_000 })
  await page.getByRole('tab', { name: '审查与待办' }).click()
  await expect(page.getByText('分场剧本（桩管线第 1 稿）')).toBeVisible()
  await page.getByRole('button', { name: '通过' }).click()

  await expect(page.getByRole('heading', { name: /第 1 件素材图 · 橘猫/ })).toBeVisible({
    timeout: 30_000,
  })
  await expect(page.getByRole('heading', { name: '提交开场首帧' })).toHaveCount(0)

  // 第 1 件被拒 → 同一位置重开 → 重交通过 → 第 2 件自动出现。
  await page.locator('#asset-image-file').setInputFiles(r2vPng(21, 'cat-try-1.png'))
  await page.getByRole('button', { name: '提交并校对' }).click()
  await expect(page.getByRole('heading', { name: '校对结论：不通过' })).toBeVisible({
    timeout: 30_000,
  })
  await page.locator('#asset-image-file').setInputFiles(r2vPng(22, 'cat-try-2.png'))
  await page.getByRole('button', { name: '提交并校对' }).click()
  await expect(page.getByRole('heading', { name: /第 2 件素材图 · 台灯/ })).toBeVisible({
    timeout: 30_000,
  })
  await page.locator('#asset-image-file').setInputFiles(r2vPng(23, 'lamp-try-1.png'))
  await page.getByRole('button', { name: '提交并校对' }).click()

  // --- 规划 / 打包：单段，段内素材子集只读可见（重编号后的 Picture 号 + 语义名）---
  await page.getByRole('tab', { name: '分段' }).click()
  await expect(page.getByText('已通过 0 / 总数 1')).toBeVisible({ timeout: 60_000 })
  await expect(page.locator('[data-segment-index="1"]')).toContainText('等待交视频')
  await expect(page.locator('[data-segment-index="1"] [data-segment-assets]')).toContainText('橘猫')
  // 只读：段表里没有任何手填素材的入口。
  await expect(
    page
      .locator('[data-segment-index="1"] [data-segment-assets]')
      .locator('input, textarea, select'),
  ).toHaveCount(0)

  // --- 交本段视频 → 只剩交付 ---
  await submitSegmentVideo(page, 1, resolve(outDir, 'seg1.mp4'))
  await expect(page.getByText('已通过 1 / 总数 1')).toBeVisible({ timeout: 30_000 })

  // --- 打包下载 → 改一段 prompt 造成最终包 → 交付归档 ---
  await page.getByRole('tab', { name: '产物' }).click()
  const [download] = await Promise.all([
    page.waitForEvent('download'),
    page.locator('[data-download="director/initial.mmxpack.zip"]').click(),
  ])
  const initialPack = testInfo.outputPath('r2v-initial.mmxpack.zip')
  await download.saveAs(initialPack)
  const finalPack = testInfo.outputPath('r2v-final.mmxpack.zip')
  makeFinalPack(initialPack, finalPack, 0)

  await page.getByRole('tab', { name: '交付' }).click()
  await page.locator('#delivery-film-upload').setInputFiles(resolve(outDir, 'film.mp4'))
  await expect(page.getByText('已选：').first()).toBeVisible({ timeout: 30_000 })
  await expect(page.locator('#delivery-film-upload')).toBeEnabled()
  await page.locator('#delivery-pack-upload').setInputFiles(finalPack)
  await expect(page.getByText('已选：')).toHaveCount(2, { timeout: 30_000 })
  const submitDelivery = page.getByRole('button', {
    name: '提交成片与最终包并归档',
  })
  await expect(submitDelivery).toBeEnabled({ timeout: 30_000 })
  await submitDelivery.click()
  await expect(page.getByText('已交付').first()).toBeVisible({ timeout: 30_000 })
  await expect(page.locator('[data-delivery-result]')).toContainText('final.mmxpack.zip')
})
