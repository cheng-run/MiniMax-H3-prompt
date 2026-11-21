import type { Page } from '@playwright/test'

// 起步澄清（#339 / spec #332 E1）在 E2E 里的统一处理。
//
// E2E 全套跑在桩管线上，桩**也**回答澄清问题（`fake_pipeline.clarification_questions`），
// 所以点「创建并开始」后表单会先展开问答面板、不立刻建会话。既有用例只关心「创建后
// 进入工作台」，不关心澄清内容——这里替它们点「跳过」（＝一条问答对都不交），
// 把创建流程恢复成加澄清之前的一步动作。
//
// 为什么要等面板出现再点：请求是异步的。早先这里用 `isVisible({ timeout })`——
// 那个 timeout 是被 Playwright **忽略**的（该方法立刻返回，不等待），所以面板只要
// 比点击晚一点点渲染，就会判成「没问到问题」而不点，创建于是停在原地
// （2026-11-21 重做实测：assets-loop / review / director 多条例用稳定挂在这里）。
// 改用 `waitFor`，再 catch 掉超时——端点返回空表时面板不展开，这条路同样要能过。
export async function skipClarifications(page: Page): Promise<void> {
  const skip = page.getByRole('button', { name: '跳过', exact: true })
  try {
    await skip.waitFor({ state: 'visible', timeout: 10_000 })
  } catch {
    // 这个主题确实没问到问题：面板不展开，创建会自己走下去。
    return
  }
  await skip.click()
}
