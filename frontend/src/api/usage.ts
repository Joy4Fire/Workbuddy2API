// 使用记录域 API：聚合统计、趋势、分页列表、详情、筛选
import { http } from './http'
import type { RecordsResponse, UsagePoint, UsageRecord, UsageSummary } from '@/types'

export const usageApi = {
  usageSummary: () => http.get<unknown, UsageSummary>('/admin/usage/summary'),
  usageTimeseries: (granularity = 'hour', points = 24, model?: string) =>
    http.get<unknown, { granularity: string; points: number; data: UsagePoint[] }>(
      `/admin/usage/timeseries?granularity=${granularity}&points=${points}${model ? `&model=${encodeURIComponent(model)}` : ''}`,
    ),
  // 服务端分页：page/page_size + 筛选条件，返回 records + total（总数驱动分页器）
  usageRecent: (
    page = 1,
    pageSize = 20,
    filters: { protocol?: string; model?: string; app_name?: string; status?: string; search?: string } = {},
  ) => {
    const p = new URLSearchParams({ page: String(page), page_size: String(pageSize), light: '1' })
    if (filters.protocol) p.set('protocol', filters.protocol)
    if (filters.model) p.set('model', filters.model)
    // app_name 允许空字符串（筛"未记录应用"的旧记录），用 != null 判断而非真值
    if (filters.app_name != null) p.set('app_name', filters.app_name)
    if (filters.status) p.set('status', filters.status)
    if (filters.search) p.set('search', filters.search)
    return http.get<unknown, RecordsResponse>(`/admin/usage/recent?${p.toString()}`)
  },
  // 单条记录详情（含完整输入/输出/COT）：列表走 light 投影，点开详情才按需取大文本
  usageDetail: (id: number) => http.get<unknown, { record: UsageRecord }>(`/admin/usage/${id}`),
  usageFilters: () =>
    http.get<unknown, {
      protocols: string[]
      models: string[]
      apps: string[]          // 现存应用（与应用页一致）
      apps_history: string[]  // 已删除应用的历史记录名
      has_unnamed?: boolean   // 是否存在未记录应用的旧记录
      statuses: string[]
    }>('/admin/usage/filters'),
}
