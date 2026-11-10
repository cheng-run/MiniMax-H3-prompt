# 前端工程（`frontend/`）

Vue 3 + TypeScript + Vite 单包，与 Python 后端同仓。这里记**环境里查不到、只能踩出来**的东西；
能靠 `frontend/package.json` 的 scripts / 配置文件读到的，不在这里重复。

## 版本矩阵（2026-11-09 实测可用）

| 项 | 值 | 约束 |
|---|---|---|
| Node | 24（`frontend/.node-version`） | CI 用 24；本机 25.x 也能跑 |
| pnpm | 11.7.0（`package.json` 的 `packageManager`） | **一律 `npx pnpm@11.7.0`**，别裸 `npx pnpm`（会去解析不存在的 12.x） |
| Vite / Vitest | Vite **5.4** + Vitest **2.1** | **必须同一 vite 主版本**：vitest@2.1 依赖 `vite ^5`，若应用侧再声明 `vite ^6`，树里会并存 vite5+vite6，`vitest/config` 的 `defineConfig` 与插件类型互斥，`vue-tsc` 报一屏 plugin 类型错 |
| TypeScript / vue-tsc | TS 5.7 + vue-tsc 2.1.10 | 兼容 |
| ESLint | flat config，只取 `pluginVue.configs['flat/essential']` | 排版交给 Prettier；用 `flat/recommended` 会与 Prettier 互相改写（样式类规则） |
| Playwright | 自带 chromium（CI）；本机可 `PW_CHANNEL=chrome` 复用系统 Chrome 免下载 | `playwright.config.ts` 读 `PW_CHANNEL` |

## 本地命令

```bash
cd frontend
npx pnpm@11.7.0 install        # 装依赖（下载由用户执行）
npx --no-install vite build    # 或 npm script: build
npx --no-install vitest run --coverage
PW_CHANNEL=chrome npx --no-install playwright test   # 本机免下载 chromium 跑 E2E
```

纯 Python 提交不跑前端：`scripts/check.sh frontend` 单独跑
`eslint + prettier --check + vue-tsc`，`scripts/check.sh fast` 只在「前端有改动」时
自动带上它（见 AGENTS.md）。Prettier 那一步与 CI 的 `format:check` 同一条命令
（2026-11-10 #225 retro 补：CI 有、本地没有，main 因此红过好几次）。

## 坑（都实测踩过）

- **双 vite 主版本 = 类型灾难**：见上表。修法是把应用侧 vite 降到 `^5.4.0`，**不要**靠 upgrade vitest 硬凑；且 `tsconfig.json` 只 `include` `src/`，让 `vite.config.ts` / `vitest.config.ts` 不参与 `vue-tsc`（跨 vite 副本的类型冲突是假阳性，运行期没问题）。
- **`frontend/pnpm-workspace.yaml` 是 pnpm 11 的构建批准表，不是二级 workspace**：`allowBuilds` 下 `esbuild` / `msw` / `vue-demi` 必须显式为 `true`，否则干净安装不跑它们的 postinstall（esbuild 缺二进制、vue-demi 不切 Vue 版本）。它是单包，不是 `apps/`/`packages/` workspace。
- **jsdom 两处不模拟真实浏览器**：① 不解析相对 URL（`fetch('/api/...')` 在 Node 下报 URL 解析错）——所以 `client.ts` 仅在 `import.meta.env.MODE === 'test'` 给 `baseUrl='http://localhost'`，生产恒为空（同源）；② 不实现 button 的「Enter→click」默认行为——键盘切换用 Playwright E2E 的真实按键验证，组件测试只断言原生 `button` 且 `tabIndex===0`。
- **覆盖率 `include` 必须排除 `*.spec.ts`**：测试文件「跑了就 100% 行覆盖」，混进去等于拿测试自身凑数（`vitest.config.ts` 的 `exclude` 已含 `src/**/*.spec.ts`）。
- **契约媒体类型**：错误响应运行时是 `application/problem+json`，FastAPI 的 `responses={model:...}` 默认却生成 `application/json`——`api/problems.py` 的 `apply_problem_media_type_openapi` 在 app 层改写成 problem+json，使 `/openapi.json` 与入库 `contracts/openapi.json` 同口径。
- **Windows 检出的 `.vue`/`.html` 曾是 CRLF，Prettier 判 LF 必红**：`.gitattributes`
  原先只声明了 `*.ts`/`*.css`/`*.mjs`，`core.autocrlf=true` 下 `.vue`/`.html` 检出成
  CRLF——`prettier --check` 在这 10 个文件上必报，`git status` 还会把它们报成改动而
  `git diff` 为空（2026-11-10 #225 retro 实测）。现已补 `*.vue`/`*.html` 的 `eol=lf`；
  老工作区要 `git add --renormalize .` 或重新检出才会换成 LF。
- **`openapi-fetch` 0.13 的中间件只能事后注册**：`createClient({ middleware: [...] })` 不报错也不生效（被当作 fetch 选项透传），必须 `client.use(middleware)`。2026-11-10 #223 实测：中间件写好、用例照红，翻 `node_modules/openapi-fetch/dist/index.js` 才定位。

## 本地跑前后端 + 关服务（Windows）

- 后端：`python -m minimax_h3_prompt.api` 绑 `127.0.0.1:8000`；前端 dev：`npx vite` 监听 `localhost:5173`（注意是 `::1`，直连 `127.0.0.1:5173` 会拒）。
- **`pkill -f` 杀不掉 uvicorn/node 子进程**（Git Bash 下只杀外壳）：用 `netstat -ano | grep ':8000 .*LISTENING'` 拿 PID，再 `powershell -Command "Stop-Process -Id <PID> -Force"`。杀不掉时对着**旧进程**的响应排错会得出假结论（实测被旧服务误导过一次）。
