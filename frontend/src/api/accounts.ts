// 账号域 API：列表/启停/优先级/删除/额度/签到/上传/扫码登录
import { http } from './http'
import type { AccountInfo, CheckinResponse } from '@/types'

export const accountsApi = {
  accounts: () => http.get<unknown, { accounts: AccountInfo[] }>('/admin/accounts'),
  setEnabled: (uid: string, enabled: boolean) =>
    http.post<unknown, { ok: boolean }>(`/admin/accounts/${uid}/${enabled ? 'enable' : 'disable'}`),
  setPriority: (uid: string, priority: number) =>
    http.post<unknown, { ok: boolean; priority: number }>(`/admin/accounts/${uid}/priority`, { priority }),
  deleteAccount: (uid: string) => http.delete<unknown, { ok: boolean }>(`/admin/accounts/${uid}`),
  refreshCredits: () => http.post<unknown, { ok: boolean; accounts: AccountInfo[] }>('/admin/credits/refresh'),
  checkin: () => http.post<unknown, CheckinResponse>('/admin/checkin'),

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
    http.get<unknown, { status: 'pending' | 'ready' | 'expired'; account?: AccountInfo }>(
      `/admin/oauth/status?state=${encodeURIComponent(state)}`,
    ),
}
