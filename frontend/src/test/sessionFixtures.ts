import type { JobSummary, SessionDetail, SessionSummary } from '@/api/client'

// 会话资源的测试夹具：形状照生成契约补齐必填字段，用例只覆盖关心的那几个键。
// 放在 src/test 下（不进覆盖率分母），避免每个 spec 各抄一份长对象。

const createdAt = '2026-11-11T00:00:00Z'
const updatedAt = '2026-11-11T00:01:00Z'

export function makeSummary(overrides: Partial<SessionSummary> = {}): SessionSummary {
  return {
    session_id: 'sess-1',
    status: 'created',
    mode: 'base',
    variant: 'FL2VA',
    plot: '雨夜旧信',
    revision: 1,
    legacy: false,
    created_at: createdAt,
    updated_at: updatedAt,
    ...overrides,
  }
}

export function makeDetail(overrides: Partial<SessionDetail> = {}): SessionDetail {
  return {
    session_id: 'sess-1',
    status: 'created',
    mode: 'base',
    variant: 'FL2VA',
    plot: '雨夜旧信',
    revision: 1,
    legacy: false,
    created_at: createdAt,
    updated_at: updatedAt,
    last_event_seq: 1,
    brief: {
      mode: 'base',
      variant: 'FL2VA',
      duration: 5,
      style: 'Cinematic',
      language: 'Chinese',
      plot: '雨夜旧信',
      clarifications: '',
      draft: '',
      director_mode: 'off',
      director_node_id: '',
      director_cache_name: '',
      director_continuity_mode: '',
    },
    ...overrides,
  }
}

export function makeJob(overrides: Partial<JobSummary> = {}): JobSummary {
  return {
    job_id: 'job-1',
    status: 'running',
    kind: 'advance',
    started_at: createdAt,
    finished_at: '',
    cancel_requested: false,
    ...overrides,
  }
}
