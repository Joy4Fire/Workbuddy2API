// axios 实例与全局拦截器：管理 Token 注入、GET 幂等重试、错误提示去重。
// 各业务域的 API 函数在 accounts.ts / usage.ts 等文件中，client.ts 统一组装导出。
import axios, { type AxiosInstance, type InternalAxiosRequestConfig } from 'axios'
import { message } from 'ant-design-vue'

export const http: AxiosInstance = axios.create({
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
