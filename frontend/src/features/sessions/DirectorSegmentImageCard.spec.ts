import { flushPromises, mount } from '@vue/test-utils'
import { http, HttpResponse } from 'msw'
import { beforeEach, describe, expect, it } from 'vitest'

import { resetSessionToken, type ActionItemOut, type Asset } from '@/api/client'
import { server } from '@/test/server'

import DirectorSegmentImageCard from './DirectorSegmentImageCard.vue'

// 切镜段的段首图交接卡（D8 §5.6 / #264）：只收本会话图片素材，提交
// ``decision=submit_segment_image`` + ``asset_id``。
const item: ActionItemOut = {
  item_id: 'item-seg-image',
  kind: 'director_segment_image',
  status: 'pending',
  created_at: '2026-11-14T00:00:00Z',
  resolved_at: '',
  reason: '',
  segment_index: 2,
}

const image: Asset = {
  asset_id: 'sess-1/aaaaaaaaaaaaaaaa',
  name: 'cut.png',
  kind: 'image',
  bytes: 1024,
  created_at: '2026-11-14T00:00:00Z',
}
const video: Asset = {
  asset_id: 'sess-1/bbbbbbbbbbbbbbbb',
  name: 'clip.mp4',
  kind: 'video',
  bytes: 4096,
  created_at: '2026-11-14T00:00:00Z',
}

const resolveUrl = 'http://localhost/api/v1/sessions/sess-1/action-items/item-seg-image/resolve'

async function settle(): Promise<void> {
  await flushPromises()
  await new Promise((resolve) => setTimeout(resolve, 10))
  await flushPromises()
}

function mountCard() {
  return mount(DirectorSegmentImageCard, {
    props: { sessionId: 'sess-1', item, assets: [image, video] },
  })
}

function button(wrapper: ReturnType<typeof mountCard>, label: string) {
  return wrapper.findAll('button').find((entry) => entry.text() === label)!
}

beforeEach(() => resetSessionToken())

describe('DirectorSegmentImageCard', () => {
  it('只列图片素材，并点名这是第 2 段的段首帧', () => {
    const wrapper = mountCard()
    const options = wrapper.findAll('option').map((option) => option.text())
    expect(options).toContain('cut.png')
    expect(options).not.toContain('clip.mp4')
    expect(wrapper.text()).toContain('第 2 段')
  })

  it('提交 decision=submit_segment_image 与 asset_id', async () => {
    const bodies: unknown[] = []
    server.use(
      http.post(resolveUrl, async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json(null, { status: 202 })
      }),
    )
    const wrapper = mountCard()
    await wrapper.find('select').setValue(image.asset_id)
    await button(wrapper, '提交段首帧并继续').trigger('click')
    await settle()

    expect(bodies).toEqual([{ decision: 'submit_segment_image', asset_id: image.asset_id }])
  })
})
