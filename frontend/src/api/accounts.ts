// 账号域 API：列表/启停/优先级/删除/额度/签到/上传/扫码登录
import { http } from './http'
import type {
  AccountInfo, ActiveMapResponse, CheckinResponse, MakeupResponse, RedeemResponse, StreakRow,
  TravelResponse,
} from '@/types'

export const accountsApi = {
  accounts: () => http.get<unknown, { accounts: AccountInfo[] }>('/admin/accounts'),
  /**
   * 启停账号（人工位）。
   *
   * `force` 只对**启用**有意义：默认不清系统自动禁用位，否则登录态已废的账号
   * 会被放回池子，下一个请求立刻再吃一次同样的错。确认要强制放回时才传 true。
   * 返回值里的 `auto_disabled_reason` 非空 = 人工位已清但系统位还在，界面要提示。
   */
  setEnabled: (uid: string, enabled: boolean, force = false) =>
    http.post<unknown, { ok: boolean; auto_disabled_reason?: string }>(
      `/admin/accounts/${uid}/${enabled ? 'enable' : 'disable'}`,
      enabled && force ? { force: true } : {},
    ),
  /** 只清**系统**自动禁用位（不动人工停用位）——两个位独立清除，语义才不互相污染 */
  clearAutoDisable: (uid: string) =>
    http.post<unknown, { ok: boolean }>(`/admin/accounts/${uid}/clear-auto-disable`),
  setPriority: (uid: string, priority: number) =>
    http.post<unknown, { ok: boolean; priority: number }>(`/admin/accounts/${uid}/priority`, { priority }),
  deleteAccount: (uid: string) => http.delete<unknown, { ok: boolean }>(`/admin/accounts/${uid}`),
  refreshCredits: () => http.post<unknown, { ok: boolean; accounts: AccountInfo[] }>('/admin/credits/refresh'),
  checkin: () => http.post<unknown, CheckinResponse>('/admin/checkin'),

  /**
   * 连登天数与补签卡余额（只读）。补签判据的唯一界面观察入口。
   *
   * 显式放宽超时：这是少数**真的打上游**的管理端点（其余端点读缓存）。
   * 后端每个上游请求 `timeout=20`，两个查询并发 + 账号间并发 → 最坏约 20~25s，
   * 紧贴 axios 默认的 30s。不显式放宽的话，上游慢一点界面就只剩一个超时提示，
   * 连"哪个账号查询失败"都看不到。
   */
  streak: () => http.get<unknown, { ok: boolean; accounts: StreakRow[] }>(
    '/admin/streak', { timeout: 45000 },
  ),
  /**
   * 手动触发一次补签检查。`dryRun` **默认 true**——手动点一下也走演练，
   * 真要补必须显式传 false（补签花的是用户自己的卡，不能因为点错按钮就花掉）。
   *
   * 超时给到 120s：只读探测是并发的（约 20s），但**实际补签是串行**的
   * （花用户的卡，不能并发打上游），每个账号最坏再 +20s。
   */
  makeup: (dryRun = true) =>
    http.post<unknown, MakeupResponse>('/admin/makeup', { dry_run: dryRun }, { timeout: 120000 }),
  /**
   * 领取连登档位奖励（7d/14d/28d），可选顺带抽奖。
   *
   * `dryRun` **默认 true**，与 `makeup` 一致——先看清"会领哪几档、各发多少"再真领。
   * 但要注意两者的性质不同：**领奖不消耗任何东西**（补签要花卡），同月重复领同一档位
   * 上游返回 409 duplicate，是幂等无副作用的。所以这里默认演练纯粹是为了先确认判据。
   *
   * 超时同 makeup：只读探测并发（约 20s），实际领取串行，每账号最坏再 +20s。
   */
  redeem: (dryRun = true, draw = false) =>
    http.post<unknown, RedeemResponse>('/admin/redeem', { dry_run: dryRun, draw },
      { timeout: 120000 }),
  /**
   * 手动触发一次猫猫旅行巡检（无猫→领养 / idle→派出 / arrived→领取）。
   *
   * **只有国内版账号会被处理**：国际版没有这套体系（`travel/config` 返回空 data），
   * 调度层按区域过滤，结果里根本不会出现国际版账号——别把"没出现"当失败。
   *
   * `dryRun` **默认 true**，与 `makeup` / `redeem` 一致：先看清"会做什么"再显式传 false。
   * 与补签不同，旅行**不消耗任何东西**（纯收益，每天 5~10 积分），默认演练纯粹是
   * 为了先确认状态机判断正确。
   *
   * 超时同 makeup：只读探测并发（约 20s），写严格串行，每账号最坏再 +20s。
   */
  travel: (dryRun = true) =>
    http.post<unknown, TravelResponse>('/admin/travel', { dry_run: dryRun },
      { timeout: 120000 }),

  /**
   * 手动检查每个账号**今天**有没有点亮活跃地图（只读，不写上游、不花积分）。
   *
   * **没有 `dryRun`**：与补签/旅行不同，这个任务本身就没有"会改状态"的分支 ——
   * 它只查 heatmap 与 streak，把结果告诉你，由你**自己去官方客户端聊一句**点亮。
   *
   * （为什么不代发对话：2026-09-22 受控实验证明 chat API 根本点不亮活跃地图 ——
   * 给当天 score=0 的账号发两次真实请求后格子仍是 0。详见
   * `scheduler.do_active_map_check` 的说明。）
   *
   * `notify` **默认 false**：手动点一次就推一条 webhook 是骚扰，只有定时任务才该通知。
   * 需要连通知一起验证（比如刚配好 webhook）再显式传 true。
   *
   * 超时同 makeup：heatmap 与 streak 并发（各自 20s），所以 30s 默认超时不够用。
   */
  activeMapCheck: (notify = false) =>
    http.post<unknown, ActiveMapResponse>('/admin/active-map/check', { notify },
      { timeout: 120000 }),

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
