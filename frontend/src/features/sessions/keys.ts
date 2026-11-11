// 幂等键生成：防双击建会话 / 重复起跑。只需唯一，不需要密码学强度。
// jsdom 与老浏览器可能没有 crypto.randomUUID，退回时间戳 + 随机串。
export function newIdempotencyKey(): string {
  const cryptoApi = globalThis.crypto
  if (cryptoApi && typeof cryptoApi.randomUUID === 'function') {
    return cryptoApi.randomUUID()
  }
  return `k-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`
}
