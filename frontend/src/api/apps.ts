// 应用 API Key 域 API
import { http } from './http'
import type { AppInfo } from '@/types'

export const appsApi = {
  apps: () => http.get<unknown, { apps: AppInfo[] }>('/admin/apps'),
  createApp: (name: string, note = '') =>
    http.post<unknown, { ok: boolean; app_id: number; name: string; key: string }>('/admin/apps', { name, note }),
  appKey: (id: number) =>
    http.get<unknown, { ok: boolean; key: string | null; unavailable?: boolean }>(`/admin/apps/${id}/key`),
  toggleApp: (id: number) => http.post<unknown, { ok: boolean; enabled: boolean }>(`/admin/apps/${id}/toggle`),
  deleteApp: (id: number) => http.delete<unknown, { ok: boolean }>(`/admin/apps/${id}`),
}
