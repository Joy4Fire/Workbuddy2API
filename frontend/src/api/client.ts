import axios, { type AxiosInstance } from 'axios'
import { message } from 'ant-design-vue'
import type {
  AABenchmark,
  AccountInfo,
  AppInfo,
  CheckinResponse,
  ModelInfo,
  Overview,
  RecordsResponse,
  Settings,
  UsagePoint,
  UsageSummary,
} from '@/types'

const http: AxiosInstance = axios.create({
  baseURL: '/',
  timeout: 30000,
})

// 请求拦截：附加管理 Token（从 localStorage 读取，支持局域网访问时设置 ADMIN_TOKEN）
http.interceptors.request.use((config) => {
  const token = localStorage.getItem('workbuddy_admin_token') || ''
  if (token) {
    config.headers['X-Admin-Token'] = token
  }
  return config
})

// 响应拦截：统一错误处理
http.interceptors.response.use(
  (resp) => resp.data,
  (err) => {
    const detail = err?.response?.data?.detail
    const msg =
      typeof detail === 'string'
        ? detail
        : detail?.error?.message || detail?.message || err.message
    message.error(`请求失败：${msg}`)
    return Promise.reject(err)
  },
)

/** 设置/清除管理 Token（保存到 localStorage） */
export function setAdminToken(token: string) {
  if (token) localStorage.setItem('workbuddy_admin_token', token)
  else localStorage.removeItem('workbuddy_admin_token')
}

export const api = {
  overview: () => http.get<unknown, Overview>('/admin/overview'),
  accounts: () => http.get<unknown, { accounts: AccountInfo[] }>('/admin/accounts'),
  setEnabled: (uid: string, enabled: boolean) =>
    http.post<unknown, { ok: boolean }>(`/admin/accounts/${uid}/${enabled ? 'enable' : 'disable'}`),
  setPriority: (uid: string, priority: number) =>
    http.post<unknown, { ok: boolean; priority: number }>(`/admin/accounts/${uid}/priority`, { priority }),
  deleteAccount: (uid: string) => http.delete<unknown, { ok: boolean }>(`/admin/accounts/${uid}`),
  refreshCredits: () => http.post<unknown, { ok: boolean; accounts: AccountInfo[] }>('/admin/credits/refresh'),
  checkin: () => http.post<unknown, CheckinResponse>('/admin/checkin'),
  usageSummary: () => http.get<unknown, UsageSummary>('/admin/usage/summary'),
  usageTimeseries: (granularity = 'hour', points = 24, model?: string) =>
    http.get<unknown, { granularity: string; points: number; data: UsagePoint[] }>(
      `/admin/usage/timeseries?granularity=${granularity}&points=${points}${model ? `&model=${encodeURIComponent(model)}` : ''}`,
    ),
  usageRecent: (limit = 100, filters: { protocol?: string; model?: string; app_name?: string; status?: string } = {}) => {
    const p = new URLSearchParams({ limit: String(limit) })
    if (filters.protocol) p.set('protocol', filters.protocol)
    if (filters.model) p.set('model', filters.model)
    if (filters.app_name) p.set('app_name', filters.app_name)
    if (filters.status) p.set('status', filters.status)
    return http.get<unknown, RecordsResponse>(`/admin/usage/recent?${p.toString()}`)
  },
  usageFilters: () =>
    http.get<unknown, { protocols: string[]; models: string[]; apps: string[]; statuses: string[] }>(
      '/admin/usage/filters',
    ),
  models: () => http.get<unknown, { models: ModelInfo[]; source?: 'dynamic' | 'static' }>('/admin/models'),
  modelsRefresh: () =>
    http.post<unknown, { ok: boolean; models: ModelInfo[]; source?: 'dynamic' | 'static' }>('/admin/models/refresh'),
  benchmarks: () =>
    http.get<unknown, { configured: boolean; models: Record<string, AABenchmark> }>('/admin/models/benchmarks'),
  benchmarksRefresh: () =>
    http.post<unknown, { ok: boolean; configured: boolean }>('/admin/models/benchmarks/refresh'),

  // 自动签到 / 额度刷新 设置
  getSettings: () => http.get<unknown, Settings>('/admin/settings'),
  saveSettings: (data: Partial<Settings>) => http.post<unknown, Settings & { ok: boolean }>('/admin/settings', data),

  // 上传 auth 文件
  uploadAuth: (file: File) => {
    const form = new FormData()
    form.append('file', file)
    return http.post<unknown, { ok: boolean; account: AccountInfo }>('/admin/accounts/upload', form, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
  },

  // 扫码登录
  oauthStart: () => http.post<unknown, { ok: boolean; state: string; authUrl: string }>('/admin/oauth/start'),
  oauthStatus: (state: string) =>
    http.get<unknown, { status: 'pending' | 'ready'; account?: AccountInfo }>(
      `/admin/oauth/status?state=${encodeURIComponent(state)}`,
    ),

  // 应用 API Key
  apps: () => http.get<unknown, { apps: AppInfo[] }>('/admin/apps'),
  createApp: (name: string, note = '') =>
    http.post<unknown, { ok: boolean; app_id: number; name: string; key: string }>('/admin/apps', { name, note }),
  appKey: (id: number) =>
    http.get<unknown, { ok: boolean; key: string | null; unavailable?: boolean }>(`/admin/apps/${id}/key`),
  toggleApp: (id: number) => http.post<unknown, { ok: boolean; enabled: boolean }>(`/admin/apps/${id}/toggle`),
  deleteApp: (id: number) => http.delete<unknown, { ok: boolean }>(`/admin/apps/${id}`),
}

/** 二维码图片地址（后端生成 SVG） */
export function qrUrl(text: string): string {
  return `/admin/oauth/qr?text=${encodeURIComponent(text)}`
}
