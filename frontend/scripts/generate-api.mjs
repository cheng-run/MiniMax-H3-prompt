// 从仓库根的 contracts/openapi.json 生成 src/api/generated.ts。
//
// - 契约唯一真源是 Python FastAPI（导出脚本 scripts/export_openapi.py）。
// - 本脚本只负责把契约转成 TypeScript 类型；产物统一 LF，且入库。
// - generated.ts 禁止手改，也从 ESLint/Prettier 排除（见配置），CI 会重新生成并做 diff。
import { writeFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

import openapiTS, { astToString } from 'openapi-typescript'

const here = dirname(fileURLToPath(import.meta.url))
const frontendRoot = resolve(here, '..')
const repoRoot = resolve(frontendRoot, '..')
const inputPath = resolve(repoRoot, 'contracts', 'openapi.json')
const outputPath = resolve(frontendRoot, 'src', 'api', 'generated.ts')

const ast = await openapiTS(pathToFileURL(inputPath))
// Windows 上 astToString 可能带 CRLF；统一成 LF，避免与 CI（Linux）漂移。
const text = astToString(ast).replace(/\r\n/g, '\n')
writeFileSync(outputPath, text, 'utf8')

console.log(`generated ${outputPath} from ${inputPath}`)
