import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vitest/config'

// 覆盖率只统计手写源码：排除生成文件、测试自身与构建配置（见 D1 spec §10）。
// 覆盖率门禁（行/函数 ≥ 80%、分支 ≥ 70%）在 D1-6（#211）加上。
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.spec.ts'],
    coverage: {
      provider: 'v8',
      include: [
        'src/main.ts',
        'src/app/**',
        'src/api/client.ts',
        'src/router/**',
        'src/stores/**',
        'src/features/**',
        'src/components/**',
      ],
      exclude: ['src/api/generated.ts', 'src/test/**', 'src/**/*.d.ts'],
    },
  },
})
