import js from '@eslint/js'
import pluginVue from 'eslint-plugin-vue'
import globals from 'globals'
import tseslint from 'typescript-eslint'

// Flat config。生成文件只由生成脚本负责（格式化与规范），因此从 lint 排除——
// 否则重生成后 lint 会对机器产物报错，drift check 也会被二次格式化的差异带偏。
export default tseslint.config(
  {
    ignores: [
      'dist/**',
      'coverage/**',
      'playwright-report/**',
      'test-results/**',
      'node_modules/**',
      'src/api/generated.ts',
    ],
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  // 只取 vue 的 essential 规则：排版交给 Prettier，避免 ESLint 的样式规则与
  // Prettier 互相改写（两者都管换行/属性换行时，会来回改）。
  ...pluginVue.configs['flat/essential'],
  {
    files: ['**/*.vue'],
    languageOptions: {
      parserOptions: {
        parser: tseslint.parser,
      },
    },
  },
  {
    languageOptions: {
      globals: {
        ...globals.browser,
        ...globals.node,
      },
    },
  },
  {
    // D1 spec §9 明确基础组件就叫 Button（单文件、无业务语义），与「组件名多词」的
    // 通用建议冲突，这里按 spec 豁免；其余组件（AppShell/StatusBadge/…）已是多词。
    rules: {
      'vue/multi-word-component-names': ['error', { ignores: ['Button'] }],
    },
  },
)
