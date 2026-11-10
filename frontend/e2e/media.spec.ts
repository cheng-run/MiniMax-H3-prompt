import { expect, test } from '@playwright/test'
import type { Page } from '@playwright/test'
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

// D3-4（#226）：真实浏览器里走「页面内上传 → <video> 播放并 seek → 出现 206」。
//
// 夹具是入库的真实 WebM（tests/fixtures/tiny.webm，VP8，2 秒）：<video> 不接受
// 合成字节，CI 也不装 ffmpeg。页面内上传走 fetch（令牌在 X-Workbench-Token 头里）；
// <video> 由浏览器自己发请求、挂不上自定义头，只能靠 /auth/token 顺带下发的
// wb_session Cookie（ADR 0033）。凭据不进 URL，也不回传给测试。

const here = dirname(fileURLToPath(import.meta.url))
const fixture = resolve(here, '..', '..', 'tests', 'fixtures', 'tiny.webm')

// 手动验收用：本机真实照片与真实成片。CI 里没设，整条用例 skip。
const realPhoto = process.env.ACCEPT_D3_PHOTO
const realArtifact = process.env.ACCEPT_D3_ARTIFACT
const reportPath = process.env.ACCEPT_D3_REPORT

interface Uploaded {
  status: number
  sessionId: string
  assetId: string
}

interface Playback {
  ok: boolean
  duration: number
  currentTime: number
}

/** 页面内上传：建会话 → 用 fetch + FormData 提交文件（D3-AC15）。 */
async function uploadInPage(
  page: Page,
  bytes: Uint8Array,
  name: string,
  mime: string,
): Promise<Uploaded> {
  return page.evaluate(
    async ({ b64, name, mime }) => {
      const boot = await fetch('/api/v1/auth/token', { cache: 'no-store' })
      if (!boot.ok) {
        throw new Error(`引导令牌失败：${boot.status}`)
      }
      const { token } = (await boot.json()) as { token: string }
      const created = await fetch('/api/v1/sessions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Workbench-Token': token },
        body: JSON.stringify({
          mode: 'base',
          variant: 'T2VA',
          duration: 4.0,
          style: '写实',
          language: 'Chinese',
          plot: 'D3 浏览器验收',
        }),
      })
      if (!created.ok) {
        throw new Error(`建会话失败：${created.status}`)
      }
      const { session_id: sessionId } = (await created.json()) as { session_id: string }
      const binary = Uint8Array.from(atob(b64), (char) => char.charCodeAt(0))
      const form = new FormData()
      form.append('file', new File([binary], name, { type: mime }))
      // asset_id 自带会话标识里的斜杠：逐段编码，与测试里的 quote(safe="/") 同规矩。
      const encodePath = (value: string) => value.split('/').map(encodeURIComponent).join('/')
      const uploaded = await fetch(`/api/v1/sessions/${encodePath(sessionId)}/assets`, {
        method: 'POST',
        headers: { 'X-Workbench-Token': token },
        body: form,
      })
      const asset = (await uploaded.json()) as { asset_id: string }
      return { status: uploaded.status, sessionId, assetId: asset.asset_id }
    },
    { b64: Buffer.from(bytes).toString('base64'), name, mime },
  )
}

function encodePath(identifier: string): string {
  return identifier.split('/').map(encodeURIComponent).join('/')
}

function mediaUrl(assetId: string): string {
  return '/api/v1/media/' + encodePath(assetId)
}

/** 放进 <video> 等元数据、再 seek；返回播放器实际给出的时长与位置（D3-AC16）。 */
async function playAndSeek(page: Page, src: string): Promise<Playback> {
  return page.evaluate(async (src) => {
    const video = document.createElement('video')
    video.muted = true
    video.preload = 'auto'
    video.src = src
    document.body.append(video)
    const loaded = await new Promise<boolean>((resolve) => {
      video.addEventListener('loadedmetadata', () => resolve(true), { once: true })
      video.addEventListener('error', () => resolve(false), { once: true })
    })
    if (!loaded) {
      return { ok: false, duration: 0, currentTime: 0 }
    }
    // 拖到中段：浏览器通常因此发 Range，且能证明 seek 真的生效（不是只读了首帧）。
    const target = Math.min(video.duration / 2, video.duration - 0.1)
    const seeked = await new Promise<boolean>((resolve) => {
      video.addEventListener('seeked', () => resolve(true), { once: true })
      video.addEventListener('error', () => resolve(false), { once: true })
      video.currentTime = target
    })
    return { ok: seeked, duration: video.duration, currentTime: video.currentTime }
  }, src)
}

function watchStatuses(page: Page, marker: string): number[] {
  const statuses: number[] = []
  page.on('response', (response) => {
    if (response.url().includes(marker)) {
      statuses.push(response.status())
    }
  })
  return statuses
}

test('页面内上传 WebM，<video> 播放并 seek 出现 206', async ({ page }) => {
  await page.goto('/')
  const mediaStatuses = watchStatuses(page, '/api/v1/media/')

  const uploaded = await uploadInPage(page, readFileSync(fixture), 'tiny.webm', 'video/webm')
  expect(uploaded.status).toBe(201)
  // 标识只回一句：全长 16 位十六进制哈希，不在这里重算（由 pytest 核对磁盘）。
  expect(uploaded.assetId).toMatch(/\/[0-9a-f]{16}$/)

  const playback = await playAndSeek(page, mediaUrl(uploaded.assetId))
  expect(playback.ok).toBe(true)
  expect(playback.duration).toBeGreaterThan(1.5)
  expect(playback.currentTime).toBeGreaterThan(0.2)

  // 播放器确实走了 Range：整份 200 也算能播，但 D3-AC16 要的是 206。
  await expect.poll(() => mediaStatuses.includes(206), { timeout: 10_000 }).toBe(true)
})

test('真实照片上传与真实成片播放（手动验收，不进 CI）', async ({ page }) => {
  test.skip(
    !realPhoto || !realArtifact,
    '需要 ACCEPT_D3_PHOTO（照片路径）与 ACCEPT_D3_ARTIFACT（产物标识）',
  )
  const started = Date.now()
  await page.goto('/')
  const mediaStatuses = watchStatuses(page, '/api/v1/media/')
  const artifactStatuses = watchStatuses(page, '/api/v1/artifacts/')

  const photoName = realPhoto!.split(/[\\/]/).pop() ?? 'photo.png'
  const photoMime = photoName.toLowerCase().endsWith('.png') ? 'image/png' : 'image/jpeg'
  const uploaded = await uploadInPage(page, readFileSync(realPhoto!), photoName, photoMime)
  expect(uploaded.status).toBe(201)

  // 照片走 <img src>（Cookie 认证），自然尺寸非零才算真的解码出来了。
  const imgSrc = mediaUrl(uploaded.assetId)
  const image = await page.evaluate(async (src) => {
    const img = document.createElement('img')
    img.src = src
    document.body.append(img)
    const ok = await new Promise<boolean>((resolve) => {
      img.addEventListener('load', () => resolve(true), { once: true })
      img.addEventListener('error', () => resolve(false), { once: true })
    })
    return { ok, width: img.naturalWidth, height: img.naturalHeight }
  }, imgSrc)
  expect(image.ok).toBe(true)
  expect(image.width).toBeGreaterThan(0)
  expect(image.height).toBeGreaterThan(0)

  // 成片走产物 content 端点（#225），与素材共用同一套 Range。
  const artifactSrc = '/api/v1/artifacts/' + encodePath(realArtifact!) + '/content'
  const playback = await playAndSeek(page, artifactSrc)
  expect(playback.ok).toBe(true)
  expect(playback.duration).toBeGreaterThan(0)
  expect(playback.currentTime).toBeGreaterThan(0)

  // 照片是整份读取（<img> 不发 Range）：200 就对了。206 只对 <video> 有意义——
  // 成片那条走用户机器上的真实 final.mp4，seek 后必须看到 206（D3-AC16）。
  await expect.poll(() => mediaStatuses.includes(200), { timeout: 10_000 }).toBe(true)
  await expect.poll(() => artifactStatuses.includes(206), { timeout: 10_000 }).toBe(true)

  const facts = {
    photo: {
      name: photoName,
      assetId: uploaded.assetId,
      naturalSize: [image.width, image.height],
      mediaStatuses,
    },
    artifact: { id: realArtifact, duration: playback.duration, statuses: artifactStatuses },
    elapsedMs: Date.now() - started,
  }
  test.info().annotations.push({ type: 'accept-d3', description: JSON.stringify(facts) })
  if (reportPath) {
    const { writeFileSync } = await import('node:fs')
    writeFileSync(reportPath, `${JSON.stringify(facts, null, 2)}\n`, 'utf-8')
  }
})
