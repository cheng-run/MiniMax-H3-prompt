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
  env: { ...process.env, PYTHONPATH: resolve(repoRoot, 'src') },
})

for (const sig of ['SIGINT', 'SIGTERM']) {
  process.on(sig, () => child.kill(sig))
}
child.on('exit', (code) => process.exit(code ?? 0))
