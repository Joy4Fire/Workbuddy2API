// 设置与概览域 API
import { http } from './http'
import type { Overview, Settings } from '@/types'

export const settingsApi = {
  overview: () => http.get<unknown, Overview>('/admin/overview'),
  getSettings: () => http.get<unknown, Settings>('/admin/settings'),
  saveSettings: (data: Partial<Settings>) => http.post<unknown, Settings & { ok: boolean }>('/admin/settings', data),
}
