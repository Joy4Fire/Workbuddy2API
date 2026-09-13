// API 统一出口：各业务域模块在此组装。视图层保持 `import { api } from '@/api/client'` 不变。
// axios 实例与拦截器在 http.ts；各域实现见同目录 accounts/usage/models/apps/settings.ts。
import { accountsApi } from './accounts'
import { appsApi } from './apps'
import { modelsApi } from './models'
import { settingsApi } from './settings'
import { usageApi } from './usage'

export const api = {
  ...accountsApi,
  ...usageApi,
  ...modelsApi,
  ...appsApi,
  ...settingsApi,
}

export { setAdminToken } from './http'

/** 二维码图片地址（后端生成 SVG） */
export function qrUrl(text: string): string {
  return `/admin/oauth/qr?text=${encodeURIComponent(text)}`
}
