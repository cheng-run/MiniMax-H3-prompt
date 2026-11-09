import { afterAll, afterEach, beforeAll } from 'vitest'

import { server } from './server'

// 未匹配的请求直接报错：避免用例默默打到真实网络而假装通过。
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())
