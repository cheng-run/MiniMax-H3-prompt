import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vitest/config'

// 覆盖率只统计手写源码：排除生成文件、测试自身与构建配置（见 D1 spec §10）。
// 门禁：行/函数 ≥ 80%、分支 ≥ 70%（D1-6，#211）。阀值直接卡在这里，CI 跑 --coverage
// 时低于标准即非零退出——「门禁」不是一份建议，是能红的判据。
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
      // 关键：排除测试文件自身，否则它们是「跑了就算覆盖」的 100% 行，会把数字撑高
      // （等于靠排除手写源码凑数）。也排除生成文件与类型声明。
      exclude: ['src/api/generated.ts', 'src/test/**', 'src/**/*.spec.ts', 'src/**/*.d.ts'],
      thresholds: { lines: 80, functions: 80, branches: 70 },
    },
  },
})
