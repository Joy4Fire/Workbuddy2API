import { http } from './http'
import type { UpdateInfo } from '@/types'

export const updatesApi = {
  getUpdates: () => http.get<unknown, UpdateInfo>('/admin/updates'),
  checkUpdates: () => http.post<unknown, UpdateInfo>('/admin/updates/check'),
}
