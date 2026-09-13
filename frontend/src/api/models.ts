// 模型目录与 AA 评测域 API
import { http } from './http'
import type { AABenchmark, ModelInfo } from '@/types'

export const modelsApi = {
  models: () => http.get<unknown, { models: ModelInfo[]; source?: 'dynamic' | 'static' }>('/admin/models'),
  modelsRefresh: () =>
    http.post<unknown, { ok: boolean; models: ModelInfo[]; source?: 'dynamic' | 'static' }>('/admin/models/refresh'),
  benchmarks: () =>
    http.get<unknown, { configured: boolean; models: Record<string, AABenchmark> }>('/admin/models/benchmarks'),
  benchmarksRefresh: () =>
    http.post<unknown, { ok: boolean; configured: boolean }>('/admin/models/benchmarks/refresh'),
}
