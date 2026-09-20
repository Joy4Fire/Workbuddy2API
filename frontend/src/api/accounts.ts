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

  // 扫码登录。region 必须显式传："cn"（国内版，默认）/"global"（国际版）——
  // 两个区域的控制面 host 不同，国际版账号打到国内站会拿不到有效二维码。
  // status 轮询必须回传 start 返回的 region，否则两次请求会打到不同控制面。
  oauthStart: (region: 'cn' | 'global' = 'cn') =>
    http.post<unknown, { ok: boolean; state: string; authUrl: string; region: string }>(
      `/admin/oauth/start?region=${encodeURIComponent(region)}`,
    ),
  oauthStatus: (state: string, region: 'cn' | 'global' = 'cn') =>
    http.get<unknown, { status: 'pending' | 'ready' | 'expired'; account?: AccountInfo }>(
      `/admin/oauth/status?state=${encodeURIComponent(state)}&region=${encodeURIComponent(region)}`,
    ),
}
