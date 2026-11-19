import type { Page } from '@playwright/test'

// 起步澄清（#339 / spec #332 E1）在 E2E 里的统一处理。
//
// E2E 全套跑在桩管线上，桩**也**回答澄清问题（`fake_pipeline.clarification_questions`），
// 所以点「创建并开始」后表单会先展开问答面板、不立刻建会话。既有用例只关心「创建后
// 进入工作台」，不关心澄清内容——这里替它们点「跳过」（＝一条问答对都不交），
// 把创建流程恢复成加澄清之前的一步动作。
//
// 为什么要等面板出现再点：请求是异步的，直接 `getByRole` 会撞「元素还不存在」。
// 若这个主题确实没问到问题（端点返回空表），面板不展开、创建会自己走下去，
// 此时 `跳过` 不可见——用 `isVisible()` 判一下，两种形态都能过。
export async function skipClarifications(page: Page): Promise<void> {
  const skip = page.getByRole('button', { name: '跳过', exact: true })
  if (await skip.isVisible({ timeout: 5_000 }).catch(() => false)) {
    await skip.click()
  }
}
