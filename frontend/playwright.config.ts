import { defineConfig, devices } from '@playwright/test'

// E2E 跑真实构建产物 + 真实 FastAPI 同源托管（D1 spec §10）：
// - API 错误态由 Vitest + MSW 组件测试覆盖，E2E 不伪造错误；
// - Vite proxy 只在开发期用本地手工证据，不进 E2E。
//
// 默认用 Playwright 自带 chromium（CI 走 `playwright install chromium`）。本地若想
// 复用系统已装的 Chrome、免下载，设 PW_CHANNEL=chrome 即可（同为 Chromium 内核）。
const channel = process.env.PW_CHANNEL

export default defineConfig({
  testDir: './e2e',
  timeout: 60_000,
  expect: { timeout: 10_000 },
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  reporter: process.env.CI ? [['list'], ['html', { open: 'never' }]] : 'list',
  use: {
    baseURL: 'http://127.0.0.1:8000',
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'], ...(channel ? { channel } : {}) } },
  ],
  webServer: {
    command: 'node scripts/serve-e2e.mjs',
    url: 'http://127.0.0.1:8000/api/v1/health',
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
})
