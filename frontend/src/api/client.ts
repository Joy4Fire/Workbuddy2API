import axios, { type AxiosInstance, type InternalAxiosRequestConfig } from 'axios'
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
  UsageRecord,
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

// 响应拦截：统一错误处理 + GET 幂等请求的指数退避重试
// 说明：仅对 GET（读数据、幂等）自动重试，POST/DELETE 等写操作不重试，避免重复副作用。
const MAX_RETRIES = 2

// 错误提示去重：概览页每 20s 自动轮询，后端短暂不可用时会连续触发多个请求失败，
// 若每次都弹 toast 会造成刷屏。同一错误文案在 DEDUPE_WINDOW 内只提示一次。
const DEDUPE_WINDOW = 5000
let _lastToastKey = ''
let _lastToastAt = 0

function toastOnce(msg: string) {
  const now = Date.now()
  if (msg === _lastToastKey && now - _lastToastAt < DEDUPE_WINDOW) return
  _lastToastKey = msg
  _lastToastAt = now
  message.error(`请求失败：${msg}`)
}

http.interceptors.response.use(
  (resp) => resp.data,
  async (err) => {
    const config = err?.config as (InternalAxiosRequestConfig & { _retry?: number }) | undefined
    const method = typeof config?.method === 'string' ? config.method.toLowerCase() : ''
    const status = err?.response?.status as number | undefined
    // 可重试条件：GET 请求 + （网络错误 / 5xx 服务端错误）+ 未超过重试次数
    const retriable =
      method === 'get' &&
      config !== undefined &&
      (status === undefined || status >= 500) &&
      (config._retry ?? 0) < MAX_RETRIES
    if (retriable) {
      config!._retry = (config!._retry ?? 0) + 1
      const delay = 500 * 2 ** ((config!._retry ?? 1) - 1) // 500ms, 1000ms
      await new Promise((r) => setTimeout(r, delay))
      return http(config!)
    }
    const detail = err?.response?.data?.detail
    const msg =
      typeof detail === 'string'
        ? detail
        : detail?.error?.message || detail?.message || err.message
    toastOnce(msg)
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
  // 服务端分页：page/page_size + 筛选条件，返回 records + total（总数驱动分页器）
  usageRecent: (page = 1, pageSize = 20, filters: { protocol?: string; model?: string; app_name?: string; status?: string } = {}) => {
    const p = new URLSearchParams({ page: String(page), page_size: String(pageSize), light: '1' })
    if (filters.protocol) p.set('protocol', filters.protocol)
    if (filters.model) p.set('model', filters.model)
    // app_name 允许空字符串（筛"未记录应用"的旧记录），用 != null 判断而非真值
    if (filters.app_name != null) p.set('app_name', filters.app_name)
    if (filters.status) p.set('status', filters.status)
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
