// Playwright E2E 的启动脚本：用仓库 .venv 的 python 起真实 FastAPI（同源托管 dist）。
// 不用 uv/npx 包一层，避免 Windows 下 node spawn 找不到可执行文件的坑；CI 与本地
// 都先 `uv sync` 生成了 .venv，路径稳定。
import { spawn } from 'node:child_process'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const frontendRoot = resolve(here, '..')
const repoRoot = resolve(frontendRoot, '..')
const isWin = process.platform === 'win32'
const python = resolve(repoRoot, '.venv', isWin ? 'Scripts/python.exe' : 'bin/python')

const child = spawn(python, ['-m', 'minimax_h3_prompt.api'], {
  cwd: repoRoot,
  stdio: 'inherit',
  env: {
    ...process.env,
    PYTHONPATH: resolve(repoRoot, 'src'),
    // 所有 E2E 共用这个 webServer：注入确定性桩管线，让 D4-3 的「创建即起跑」不用真实
    // 模型即可跑完整闭环。既有 spec 不起 job，因此这个开关对它们无副作用。
    MINIMAX_FAKE_PIPELINE: '1',
    // 端口要与 playwright.config.ts 的 baseURL 同源；并行会话用 E2E_PORT 各用各的。
    ...(process.env.E2E_PORT ? { H3_PORT: process.env.E2E_PORT } : {}),
  },
})

for (const sig of ['SIGINT', 'SIGTERM']) {
  process.on(sig, () => child.kill(sig))
}
child.on('exit', (code) => process.exit(code ?? 0))
