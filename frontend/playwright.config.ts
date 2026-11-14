import { defineConfig, devices } from '@playwright/test'

// E2E 跑真实构建产物 + 真实 FastAPI 同源托管（D1 spec §10）：
// - API 错误态由 Vitest + MSW 组件测试覆盖，E2E 不伪造错误；
// - Vite proxy 只在开发期用本地手工证据，不进 E2E。
//
// 默认用 Playwright 自带 chromium（CI 走 `playwright install chromium`）。本地若想
// 复用系统已装的 Chrome、免下载，设 PW_CHANNEL=chrome 即可（同为 Chromium 内核）。
const channel = process.env.PW_CHANNEL

// E2E 的端口默认 8000，可由 E2E_PORT 覆盖（retro 2026-11-14）：本仓常并行开多个会话，
// 两个 E2E 同时默认 8000 时，后跑的那个会**复用别人的服务**（错的数据根、错的桩开关），
// 测试挂住且零输出。`reuseExistingServer` 因此恒为 false：端口被占用时当场点名报错，
// 而不是静默连上一个不属于本次运行的服务。
const port = process.env.E2E_PORT ?? '8000'
const baseURL = `http://127.0.0.1:${port}`

export default defineConfig({
  testDir: './e2e',
  timeout: 60_000,
  expect: { timeout: 10_000 },
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  reporter: process.env.CI ? [['list'], ['html', { open: 'never' }]] : 'list',
  use: {
    baseURL,
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'], ...(channel ? { channel } : {}) } },
  ],
  webServer: {
    // 先 `vite build` 再起 API（retro 2026-11-14）：`serve-e2e.mjs` 托管的是
    // `frontend/dist`，本地残留一份旧产物时，新加的 UI 元素在浏览器里根本不存在——
    // 表现为 locator 等 5 分钟超时，而不是「构建过期」。CI 本来就先 build，
    // 这里让本地与 CI 同一条命令。
    command: 'npm run build && node scripts/serve-e2e.mjs',
    url: `${baseURL}/api/v1/health`,
    reuseExistingServer: false,
    timeout: 120_000,
  },
})
