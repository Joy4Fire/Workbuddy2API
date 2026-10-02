<script setup lang="ts">
// 设置页：四个分组各自独立保存。
//
// 为什么做成独立页面 + 分组保存，而不是原来的"单弹窗 + 底部统一保存/取消"：
// 1) 后端 /admin/settings 本就是逐字段判断（`if "x" in body`），分组提交天然只影响本组；
//    而统一保存意味着改了一组再点"取消"，另一组已改好的值也跟着丢。
// 2) 内容已超过一屏且会继续增长（每加一个功能就多一个分组），弹窗只会越来越长。
// 3) 设置本来就与"账号"无关（模型别名、AA Key），挂在账号页工具栏里是错位的。
import { ref, onMounted } from 'vue'
import SystemUpdates from '@/components/SystemUpdates.vue'
import { message } from 'ant-design-vue'
import {
  GlobalOutlined, ClockCircleOutlined, DatabaseOutlined, BellOutlined,
} from '@ant-design/icons-vue'
import { api } from '@/api/client'
import type {
  ActiveMapResult, MakeupResult, RedeemResult, RegionCount, Settings, StreakRow, TravelResult,
} from '@/types'

const activeTab = ref('region')
const loading = ref(false)
// 正在保存的分组 key（null = 空闲）。按组记录，避免一个组的转圈影响其他组的按钮。
const savingGroup = ref<string | null>(null)

const settings = ref<Settings>({
  checkin_hours: '9,21', credit_refresh_min: '30', model_refresh_hour: '6',
  model_ttl_min: '60', aa_refresh_hour: '7', keepalive_hour: '22',
})
const creditMinutes = ref(30)
const modelRefreshHour = ref(6)
const modelTtlMin = ref(60)
const aaRefreshHour = ref(7)
const keepaliveHour = ref(22)
const aaKey = ref('')
const aaKeyMasked = ref('')
const aaEnabled = ref(false)
const aaClear = ref(false)
const keepaliveEnabled = ref(true)
// 积分预警
const alertEnabled = ref(false)
const alertWebhookUrl = ref('')
const alertThreshold = ref(10)
const alertExpiryDays = ref(3)
// 模型别名映射（textarea 原文）
const modelAliases = ref('')
const checkinErr = ref('')
// ---- 区域与网络 ----
// DB 值（输入框）与环境变量值分开保存：输入框留空时，下方提示"当前用的是环境变量 XXX"，
// 避免用户误以为"没配置"而重复填写。
const poolRegions = ref<RegionCount[]>([])
const backend = ref('')
const proxy = ref('')
const workbuddyExe = ref('')
const envBackend = ref('')
const envProxy = ref('')
const envWorkbuddyExe = ref('')
// 在途并发上限（P1-1）。刻意用字符串 + 文本框而不是 a-input-number：
// "留空"（回落环境变量）与 "0"（不限制）是两个**不同**的语义，
// a-input-number 清空后给 null，序列化时容易变成 "null"/0，把两种语义搅在一起。
const maxInFlight = ref('')
const maxInFlightGlobal = ref('')
const envMaxInFlight = ref('')
const envMaxInFlightGlobal = ref('')
const inFlightErr = ref('')

// ---- 系统提示词三模式（P2-1）----
const promptMode = ref<'passthrough' | 'custom' | 'append'>('passthrough')
const promptText = ref('')

// ---- 补签保连登（P3 试点）----
const makeupEnabled = ref(false)
const makeupDryRun = ref(true)
const streakOpen = ref(false)
const streakLoading = ref(false)
const streakRows = ref<StreakRow[]>([])
const makeupRunning = ref(false)
const makeupResults = ref<MakeupResult[]>([])
const makeupDryRunUsed = ref(true)
// 连登档位奖励（补签卡的唯一常规来源）。默认演练，与补签一致。
const redeemRunning = ref(false)
const redeemResults = ref<RedeemResult[]>([])
const redeemDryRunUsed = ref(true)
const redeemDraw = ref(false)

// ---- 猫猫旅行（国内版专属）----
const travelEnabled = ref(false)
const travelDryRun = ref(true)
const travelRunning = ref(false)
const travelResults = ref<TravelResult[]>([])
const travelDryRunUsed = ref(true)

// ---- 活跃地图每日提醒 ----
// **只读任务**：只查上游、只提醒，不写任何东西、不花积分 → 默认开，且没有演练档。
const activeMapEnabled = ref(true)
const activeMapHour = ref(23)
const activeMapRunning = ref(false)
const activeMapResults = ref<ActiveMapResult[]>([])

async function load() {
  loading.value = true
  checkinErr.value = ''
  try {
    const res = await api.getSettings()
    settings.value = res
    creditMinutes.value = parseInt(res.credit_refresh_min, 10) || 30
    modelRefreshHour.value = parseInt(res.model_refresh_hour, 10) || 6
    modelTtlMin.value = parseInt(res.model_ttl_min as any, 10) || 60
    aaRefreshHour.value = parseInt(res.aa_refresh_hour, 10) || 7
    keepaliveHour.value = parseInt(res.keepalive_hour, 10) || 22
    aaKeyMasked.value = res.aa_api_key_masked || ''
    aaEnabled.value = !!res.aa_enabled
    aaKey.value = ''
    aaClear.value = false
    keepaliveEnabled.value = res.keepalive_enabled !== '0'
    alertEnabled.value = res.alert_enabled === '1'
    alertWebhookUrl.value = res.alert_webhook_url || ''
    alertThreshold.value = parseInt(res.alert_threshold_percent as any, 10) || 10
    alertExpiryDays.value = parseInt(res.alert_expiry_days as any, 10) || 3
    modelAliases.value = res.model_aliases || ''
    promptMode.value = (res.prompt_mode as typeof promptMode.value) || 'passthrough'
    promptText.value = res.prompt_text || ''
    makeupEnabled.value = res.makeup_enabled === '1'
    makeupDryRun.value = res.makeup_dry_run !== '0'
    travelEnabled.value = res.travel_enabled === '1'
    travelDryRun.value = res.travel_dry_run !== '0'
    activeMapEnabled.value = res.active_map_enabled !== '0'
    activeMapHour.value = parseInt(res.active_map_hour as any, 10)
    if (Number.isNaN(activeMapHour.value)) activeMapHour.value = 23
    poolRegions.value = res.regions || []
    backend.value = res.backend || ''
    proxy.value = res.proxy || ''
    workbuddyExe.value = res.workbuddy_exe || ''
    envBackend.value = res.env_backend || ''
    envProxy.value = res.env_proxy || ''
    envWorkbuddyExe.value = res.env_workbuddy_exe || ''
    maxInFlight.value = res.max_in_flight || ''
    maxInFlightGlobal.value = res.max_in_flight_global || ''
    envMaxInFlight.value = res.env_max_in_flight || ''
    envMaxInFlightGlobal.value = res.env_max_in_flight_global || ''
  } catch { /* 拦截器已提示 */ } finally {
    loading.value = false
  }
}
onMounted(load)

function validateCheckinHours(s: string): boolean {
  const parts = s.split(',').map((p) => p.trim()).filter((p) => p !== '')
  if (parts.length === 0) return false
  for (const p of parts) {
    const n = Number(p)
    if (!/^\d{1,2}$/.test(p) || n < 0 || n > 23) return false
  }
  return true
}

// 在途并发上限：留空 = 回落环境变量；0 = 不限制；其余为 1-64 的整数。
// 空串与 0 语义不同，所以这里必须显式区分，不能用 falsy 判断。
function validateInFlight(v: string): boolean {
  const s = v.trim()
  if (s === '') return true
  if (!/^\d{1,2}$/.test(s)) return false
  const n = Number(s)
  return n >= 0 && n <= 64
}

function onAAClearChange() {
  if (aaClear.value) aaKey.value = ''
}

// 统一提交入口：按分组只发送本组字段。
// 后端对每个字段独立判断"是否出现"，所以未包含的字段不会被清空——
// 这是"按组保存"能成立的前提，不要改成发送完整 payload。
async function saveGroup(group: string) {
  const payload: Partial<Settings> = {}
  if (group === 'region') {
    if (!validateInFlight(maxInFlight.value) || !validateInFlight(maxInFlightGlobal.value)) {
      inFlightErr.value = '格式无效：留空表示用环境变量，或填 0-64 的整数（0 = 不限制）'
      return
    }
    inFlightErr.value = ''
    payload.backend = backend.value.trim()
    payload.proxy = proxy.value.trim()
    payload.workbuddy_exe = workbuddyExe.value.trim()
    // 空串照发：后端把空串严格解释为"未设置"→ 回落环境变量，这是有意设计的语义
    payload.max_in_flight = maxInFlight.value.trim()
    payload.max_in_flight_global = maxInFlightGlobal.value.trim()
  } else if (group === 'schedule') {
    const hrs = settings.value.checkin_hours
    if (!validateCheckinHours(hrs)) {
      checkinErr.value = '格式无效：请输入 0-23 之间的整数，逗号分隔（如 9,21）'
      return
    }
    checkinErr.value = ''
    payload.checkin_hours = hrs
    payload.credit_refresh_min = String(creditMinutes.value)
    payload.aa_refresh_hour = String(aaRefreshHour.value)
    payload.keepalive_enabled = keepaliveEnabled.value ? '1' : '0'
    payload.keepalive_hour = String(keepaliveHour.value)
    payload.makeup_enabled = makeupEnabled.value ? '1' : '0'
    payload.makeup_dry_run = makeupDryRun.value ? '1' : '0'
    payload.travel_enabled = travelEnabled.value ? '1' : '0'
    payload.travel_dry_run = travelDryRun.value ? '1' : '0'
    payload.active_map_enabled = activeMapEnabled.value ? '1' : '0'
    payload.active_map_hour = String(activeMapHour.value)
  } else if (group === 'model') {
    payload.model_refresh_hour = String(modelRefreshHour.value)
    payload.model_ttl_min = String(modelTtlMin.value)
    payload.model_aliases = modelAliases.value
    payload.prompt_mode = promptMode.value
    payload.prompt_text = promptText.value
  } else if (group === 'notify') {
    payload.alert_enabled = alertEnabled.value ? '1' : '0'
    payload.alert_webhook_url = alertWebhookUrl.value.trim()
    payload.alert_threshold_percent = String(alertThreshold.value)
    payload.alert_expiry_days = String(alertExpiryDays.value)
    if (aaKey.value) {
      payload.aa_api_key = aaKey.value.trim()
    } else if (aaClear.value) {
      payload.aa_api_key = ''
      payload.clear_aa_api_key = true
    }
  } else {
    return
  }
  savingGroup.value = group
  try {
    await api.saveSettings(payload)
    message.success('已保存，将按新配置生效')
    // 重新拉一次，把后端规范化后的值同步回界面
    // （签到时间会被排序去重、AA key 会变成掩码、BACKEND 会去掉尾部斜杠）
    await load()
  } catch { /* 拦截器已提示 */ } finally {
    savingGroup.value = null
  }
}

// ---------- 连登 / 补签（P3 试点）----------
// 补签花的是**用户自己的卡**，所以这里所有入口都默认走演练：
// 「查看连登状态」纯只读；「立即检查」默认 dry_run=true，只有点"确认实际补签"才发真请求。
async function openStreak() {
  streakOpen.value = true
  streakLoading.value = true
  makeupResults.value = []
  redeemResults.value = []
  travelResults.value = []
  activeMapResults.value = []
  try {
    const res = await api.streak()
    streakRows.value = res.accounts || []
  } catch {
    streakRows.value = []
  } finally {
    streakLoading.value = false
  }
}

// ⚠️ 顺序陷阱：`openStreak()` 会**清空结果表**（它是"重新打开弹窗"的语义）。
// 所以三个 runXxx 里都必须**先 `await openStreak()` 再回填 results**，反了的话
// 结果表永远是空的——演练的全部意义就是"先看清会做什么"，表空了功能就白做。

async function runMakeup(dryRun: boolean) {
  makeupRunning.value = true
  try {
    const res = await api.makeup(dryRun)
    if (!res.results?.length) {
      message.info('没有需要处理的账号（可能昨日在当月之外，跨月补不了）')
    } else if (res.dry_run) {
      message.success(`演练完成：${res.results.length} 个账号已判定，未实际补签`)
    } else {
      const done = res.results.filter((r) => r.action === 'makeup').length
      message.success(`补签完成：成功 ${done} 个`)
    }
    // 补完刷新一下，界面上的连登天数才是新的；再回填本次结论（顺序见上方说明）
    await openStreak()
    makeupDryRunUsed.value = res.dry_run
    makeupResults.value = res.results || []
  } catch { /* 拦截器已提示 */ } finally {
    makeupRunning.value = false
  }
}

// 判据链结论 → 标签配色。用"是否真的动了用户的卡"来分色，而不是按成败：
// skip（没条件补）和 would-makeup（演练判定可补）都是**没花钱**的，不该显示成红色。
function makeupTag(r: MakeupResult): { color: string; text: string } {
  if (r.action === 'makeup') return { color: 'green', text: '已补签' }
  if (r.action === 'makeup-failed') return { color: 'red', text: '补签失败' }
  if (r.action === 'would-makeup') return { color: 'blue', text: '演练：可补' }
  return { color: 'default', text: '跳过' }
}

/**
 * 领取连登档位奖励（7d/14d/28d）。**补签卡唯一的常规来源就是这里**
 * （每档每月各送 1 张），所以补签卡余额长期为 0 通常不是功能坏了，
 * 而是活跃连登还没到 7 天、三个档位都还 locked。
 *
 * 与补签的性质差异要记住：**领奖不消耗任何东西**（补签要花卡），
 * 同月重复领同一档位上游返回 409，是幂等无副作用的。
 */
async function runRedeem(dryRun: boolean, draw = false) {
  redeemRunning.value = true
  try {
    const res = await api.redeem(dryRun, draw)
    if (!res.results?.length) {
      message.info('没有可处理的账号')
    } else if (res.dry_run) {
      const can = res.results.reduce((n, r) => n + (r.claimable?.length || 0), 0)
      message.success(can ? `演练完成：有 ${can} 个档位可领，未实际领取` : '演练完成：当前没有可领的档位')
    } else {
      const ok = res.results.reduce(
        (n, r) => n + (r.claimed?.filter((c) => c.ok).length || 0), 0)
      message.success(ok ? `领取完成：成功 ${ok} 个档位` : '领取完成：没有档位被领取（可能都还没到条件）')
    }
    // 领完刷新：补签卡余额、档位状态都会变；再回填本次结论（顺序见 openStreak 上方说明）
    await openStreak()
    redeemDryRunUsed.value = res.dry_run
    redeemResults.value = res.results || []
  } catch { /* 拦截器已提示 */ } finally {
    redeemRunning.value = false
  }
}

/** 档位状态 → 标签。locked 用灰、available 用蓝（可领）、claimed 用绿。 */
function tierTag(status?: string): { color: string; text: string } {
  if (status === 'available') return { color: 'blue', text: '可领' }
  if (status === 'claimed') return { color: 'green', text: '本月已领' }
  return { color: 'default', text: '未达标' }
}

/** 档位回执拼成一行说明。放在 script 里而不是模板里，模板里 `record` 是 any，类型检查覆盖不到。 */
function claimedSummary(r: RedeemResult): string {
  return (r.claimed || []).map((c) => `${c.tier}: ${c.message}`).join('；')
}

/** 抽奖结果 → 一行说明。 */
function drawSummary(r: RedeemResult): string {
  const d = r.draw
  if (!d) return ''
  if (d.ok) return `抽奖：中得 ${d.prize?.name || d.prize?.credit || '奖品'}`
  return `抽奖：${d.message}`
}

/** 把一行的档位状态拼成可读文本，如「7d 未达标 / 14d 未达标」。 */
function tierSummary(row: StreakRow): string {
  const st = row.redemption?.status
  if (!st) return '-'
  const parts = ['7d', '14d', '28d'].map((t) => `${t} ${tierTag(st[t]).text}`)
  if (row.lottery_chances !== null && row.lottery_chances !== undefined) {
    parts.push(`抽奖次数 ${row.lottery_chances}`)
  }
  return parts.join(' / ')
}

// ---------- 猫猫旅行（国内版专属）----------
/**
 * 手动触发一次旅行巡检。`dryRun` 默认 true，与补签/领奖一致。
 *
 * 注意旅行**只有国内版账号**会被处理（国际版没有这套体系），所以结果表里看不到
 * 国际版账号是正常的，不是失败。
 */
async function runTravel(dryRun: boolean) {
  travelRunning.value = true
  try {
    const res = await api.travel(dryRun)
    if (!res.results?.length) {
      message.info('没有可处理的账号（猫猫旅行只有国内版有）')
    } else if (res.dry_run) {
      message.success(`演练完成：${res.results.length} 个账号已判定，未发任何写请求`)
    } else {
      const got = res.results.reduce((n, r) => n + (r.reward || 0), 0)
      message.success(got ? `旅行完成：共领取 ${got} 积分` : '旅行完成：本轮没有可领的积分')
    }
    // 巡检完刷新：状态与可领积分都变了；再回填本次结论（顺序见 openStreak 上方说明）
    await openStreak()
    travelDryRunUsed.value = res.dry_run
    travelResults.value = res.results || []
  } catch { /* 拦截器已提示 */ } finally {
    travelRunning.value = false
  }
}

/**
 * 旅行状态 → 标签。
 *
 * `travel` 为 `null`/`undefined` = **该账号区域没有这套活动**（国际版），
 * 要显示成"无此活动"而不是"查询失败"——后端是按区域主动跳过的。
 */
function travelTag(row: StreakRow): { color: string; text: string } {
  const t = row.travel
  if (!t) return { color: 'default', text: '无此活动' }
  if (t.state === 'arrived') {
    return { color: 'green', text: t.reward_credit ? `已到站 +${t.reward_credit}` : '已到站' }
  }
  if (t.state === 'traveling') return { color: 'blue', text: '旅行中' }
  if (t.state === 'idle') {
    return t.daily_limit_reached
      ? { color: 'default', text: '今日已派出' }
      : { color: 'orange', text: '待派出' }
  }
  if (t.state === 'none') return { color: 'default', text: '未领养' }
  // 未知状态原样显示：上游加了新状态时要能立刻看见，别悄悄归到某一类里
  return { color: 'default', text: t.state }
}

/** 旅行状态的一行说明（地点 / 幂等键等），供 tooltip 用。 */
function travelSummary(row: StreakRow): string {
  const t = row.travel
  if (!t) return '该账号所属区域没有猫猫旅行活动（国际版无此体系）'
  const parts = [`状态 ${t.state}`]
  if (t.location) parts.push(`地点 ${t.location}`)
  if (t.reward_credit) parts.push(`可领 ${t.reward_credit} 积分`)
  if (t.record_id) parts.push(`record ${t.record_id}`)
  if (t.daily_limit_reached) parts.push('今日已派出过（每天 1 趟）')
  return parts.join(' / ')
}

/** 巡检结论 → 标签。按"是否真的动了账号"分色，正常拒绝（如门槛未达）不该显示成红色。 */
function travelActionTag(r: TravelResult): { color: string; text: string } {
  if (r.action === 'depart' || r.action === 'claim' || r.action === 'claim+depart') {
    return { color: 'green', text: r.action === 'claim+depart' ? '已领并派出' : (r.action === 'claim' ? '已领取' : '已派出') }
  }
  if (r.action === 'adopt') return { color: 'green', text: '已领养' }
  if (r.action === 'depart-failed') return { color: 'red', text: '派出失败' }
  if (r.action === 'would-depart') return { color: 'blue', text: '演练：将派出' }
  if (r.action === 'would-claim') return { color: 'blue', text: '演练：将领取' }
  if (r.action === 'would-adopt') return { color: 'blue', text: '演练：将领养' }
  return { color: 'default', text: '跳过' }
}

// ---------- 活跃地图每日提醒 ----------
/**
 * 手动检查一次"今天点亮了没有"。
 *
 * **`notify` 固定传 false**：手动点一次就推一条 webhook 是骚扰，只有定时任务才该通知。
 * 后端默认也是 false，这里显式传是为了让意图在代码里可见。
 *
 * 这个任务只读：不写上游、不花积分、不代发对话。所以**没有演练档**——
 * 结果表里的 unlit 就是"需要你自己去官方客户端聊一句"的账号。
 */
async function runActiveMapCheck() {
  activeMapRunning.value = true
  try {
    const res = await api.activeMapCheck(false)
    const unlit = (res.results || []).filter((r) => r.action === 'unlit')
    if (!res.results?.length) {
      message.info('没有可检查的账号')
    } else if (unlit.length) {
      // 用 warning 而不是 success：这是需要用户动手的信号，别让绿色对勾把注意力带走
      message.warning(`${unlit.length} 个账号今天还没点亮活跃地图，需要到官方客户端聊一句`)
    } else {
      message.success('今天所有账号都已点亮（或地图暂未覆盖今天）')
    }
    // 先 await 刷新、再回填：反了会被 openStreak 里的 results.value = [] 清空
    await openStreak()
    activeMapResults.value = res.results || []
  } catch { /* 拦截器已提示 */ } finally {
    activeMapRunning.value = false
  }
}

/**
 * 今天的活跃地图分数 → 标签。
 *
 * `null`/`undefined` = **地图里没有今天这一格**（无判据），必须与 `0`
 * （有格子但今天还没活跃）分开显示：前者是"上游换了窗口口径"，后者才是
 * "今天真没活跃"。混为一谈会让用户对着一个不存在的判据空着急。
 */
function heatTodayTag(row: StreakRow): { color: string; text: string } {
  const v = row.heat_today
  if (v === null || v === undefined) return { color: 'default', text: '无数据' }
  if (v > 0) return { color: 'green', text: `已点亮 ${v}` }
  return { color: 'red', text: '未点亮' }
}

/** 检查结论 → 标签。`unlit` 用红色（要用户动手），`no-cell` 用灰色（无判据，不是问题）。 */
function activeMapActionTag(r: ActiveMapResult): { color: string; text: string } {
  if (r.action === 'lit') return { color: 'green', text: '已点亮' }
  if (r.action === 'unlit') return { color: 'red', text: '未点亮' }
  if (r.action === 'no-cell') return { color: 'default', text: '无判据' }
  if (r.action === 'query-failed') return { color: 'orange', text: '查询失败' }
  return { color: 'default', text: '跳过' }
}
</script>

<template>
  <a-spin :spinning="loading">
    <a-card :bordered="false">
      <a-tabs v-model:activeKey="activeTab">
        <a-tab-pane key="updates" tab="系统更新">
          <SystemUpdates />
        </a-tab-pane>
        <!-- ---------- 区域与网络 ---------- -->
        <a-tab-pane key="region">
          <template #tab><span><GlobalOutlined /> 区域与网络</span></template>
          <a-form layout="vertical" style="max-width: 640px">
            <a-form-item label="账号池区域分布">
              <a-space wrap>
                <a-tag v-for="r in poolRegions" :key="r.id" :color="r.id === 'global' ? 'purple' : 'blue'">
                  {{ r.label }} {{ r.count }} 个
                </a-tag>
                <span v-if="!poolRegions.length" class="hint">暂无账号</span>
              </a-space>
              <div class="hint" style="margin-top: 4px">
                区域由每个账号 auth 文件里的 domain 自动判定，无需手动指定。两个区域的模型集大部分不重叠，请求会自动路由到对应区域的账号。
              </div>
            </a-form-item>
            <a-form-item label="BACKEND（强制上游 host，高级）">
              <a-input
                v-model:value="backend"
                allow-clear
                :placeholder="envBackend ? `环境变量当前为 ${envBackend}（留空即用它）` : '留空 = 按账号区域自动选择（推荐）'"
              />
              <div class="hint" style="margin-top: 4px">
                <span style="color: #d97706">留空即可，这是推荐配置。</span>填写后会对<b>所有</b>账号生效，强制它们打同一个上游 host——仅单区域自建部署才需要；国内外混池时填了会让另一区域的账号全部不可用。
              </div>
            </a-form-item>
            <a-form-item label="PROXY（出站代理，高级）">
              <a-input
                v-model:value="proxy"
                allow-clear
                :placeholder="envProxy ? `环境变量当前为 ${envProxy}（留空即用它）` : '留空 = 直连（推荐）'"
              />
              <div class="hint" style="margin-top: 4px">
                仅当上游必须走代理才能访问时填写，支持 http:// 与 socks5://。默认直连，且<b>不会</b>读取系统的 HTTP_PROXY 环境变量——Docker/内网里那些值经常无效，会把本该直连的请求带偏。
              </div>
            </a-form-item>
            <a-form-item label="WORKBUDDY_EXE（官方客户端路径，高级）">
              <a-input
                v-model:value="workbuddyExe"
                allow-clear
                :placeholder="envWorkbuddyExe ? `环境变量当前为 ${envWorkbuddyExe}（留空即用它）` : '留空 = 按平台默认位置探测'"
              />
              <div class="hint" style="margin-top: 4px">
                仅用于解密 WorkBuddy 桌面端 5.6.0+ 的加密登录态。留空时会自动探测常见安装路径（如 %LOCALAPPDATA%\Programs\WorkBuddy\WorkBuddy.exe），本机没装官方客户端时才需要手动填。
              </div>
            </a-form-item>
            <a-form-item
              label="单账号在途并发上限"
              :validate-status="inFlightErr ? 'error' : ''"
              :help="inFlightErr || undefined"
            >
              <a-input
                v-model:value="maxInFlight"
                allow-clear
                style="max-width: 320px"
                :placeholder="`留空 = 用环境变量（当前 ${envMaxInFlight || '3'}）`"
                @change="inFlightErr = ''"
              />
              <div class="hint" style="margin-top: 4px">
                上游按「同一账号同时打到它的连接数」做风控：多个客户端并发过来时，选号器会把它们分到同一个健康账号上，
                瞬间形成并发尖峰 → WAF 403 / 429。<b>0 = 不限制</b>；留空 = 用环境变量（当前 {{ envMaxInFlight || '3' }}）。
                只约束「建立连接 → 首字节到达」这段窗口，不影响长回答的流式输出。
              </div>
            </a-form-item>
            <a-form-item label="国际版在途并发上限">
              <a-input
                v-model:value="maxInFlightGlobal"
                allow-clear
                style="max-width: 320px"
                :placeholder="`留空 = 用环境变量（当前 ${envMaxInFlightGlobal || '2'}）`"
                @change="inFlightErr = ''"
              />
              <div class="hint" style="margin-top: 4px">
                国际版单独一档：<b>global 域的风控更严</b>（实测同一账号在国际版更容易触发 403 / 11140），
                所以默认比国内版更低（{{ envMaxInFlightGlobal || '2' }} vs {{ envMaxInFlight || '3' }}）。
              </div>
            </a-form-item>
            <a-button type="primary" :loading="savingGroup === 'region'" @click="saveGroup('region')">保存本组</a-button>
          </a-form>
        </a-tab-pane>

        <!-- ---------- 定时任务 ---------- -->
        <a-tab-pane key="schedule">
          <template #tab><span><ClockCircleOutlined /> 定时任务</span></template>
          <a-form layout="vertical" style="max-width: 640px">
            <a-form-item
              label="每日自动签到时间（小时，逗号分隔，0-23）"
              :validate-status="checkinErr ? 'error' : ''"
              :help="checkinErr || undefined"
            >
              <a-input v-model:value="settings.checkin_hours" placeholder="例如 9,21" @change="checkinErr=''" />
              <div class="hint" style="margin-top: 4px">
                到达设定的小时且当日未签到即自动签到一次，例如 9,21 表示每天 9 点和 21 点各检查一次
              </div>
            </a-form-item>
            <a-form-item label="额度刷新间隔（分钟，1-1440）">
              <a-input-number v-model:value="creditMinutes" :min="1" :max="1440" style="width: 100%" />
            </a-form-item>
            <a-form-item label="每日 AA 评测刷新时间（小时，0-23）">
              <a-input-number v-model:value="aaRefreshHour" :min="0" :max="23" style="width: 100%" />
              <div class="hint" style="margin-top: 4px">
                每天该小时自动刷新 Artificial Analysis 评测数据（intelligence / coding / agentic 指数）。需先在「通知与密钥」里配置 AA API Key。
              </div>
            </a-form-item>
            <a-form-item label="每日 token 保活">
              <a-space direction="vertical" style="width: 100%">
                <a-switch v-model:checked="keepaliveEnabled" checked-children="开启" un-checked-children="关闭" />
                <div class="hint">
                  开启后每天定时自动刷新账号 token（防止长期不用过期）。关闭则完全不保活。
                </div>
              </a-space>
            </a-form-item>
            <a-form-item label="每日 token 保活时间（小时，0-23）">
              <a-input-number v-model:value="keepaliveHour" :min="0" :max="23" style="width: 100%" :disabled="!keepaliveEnabled" />
              <div class="hint" style="margin-top: 4px">
                每天该小时自动刷新账号 token；连续多次失败（session 失效）的账号才会被自动停用
              </div>
            </a-form-item>
            <a-form-item label="补签卡保连登（试点）">
              <a-space direction="vertical" style="width: 100%">
                <a-space>
                  <a-switch v-model:checked="makeupEnabled" checked-children="开启" un-checked-children="关闭" />
                  <span class="hint">总开关（默认关）</span>
                </a-space>
                <a-space>
                  <a-switch v-model:checked="makeupDryRun" checked-children="演练" un-checked-children="实补" :disabled="!makeupEnabled" />
                  <span class="hint">演练模式（默认开）：只判定、不发请求</span>
                </a-space>
                <a-space wrap>
                  <a-button size="small" @click="openStreak">查看连登状态</a-button>
                  <a-button size="small" :loading="makeupRunning" @click="runMakeup(true)">立即检查（演练）</a-button>
                </a-space>
                <div class="hint">
                  连登断档只可能发生在「上一个自然日」，所以每天检查昨日：昨日无对话活动、有补签卡、且没补过，就用一张卡把连登续上。
                  <br />
                  <b style="color: #d97706">默认保持「演练」</b>——补签花的是你自己的卡，先确认判据无误再关掉演练。
                  <br />
                  补签卡余额一直是 0 时，先看上面的<b>档位状态</b>：补签卡唯一的常规来源是连登档位奖励
                  （7d/14d/28d 每档每月各送 1 张），三个档位都是「未达标」就说明还没到 7 天，不是功能坏了。
                </div>
              </a-space>
            </a-form-item>
            <a-form-item label="猫猫旅行（国内版专属）">
              <a-space direction="vertical" style="width: 100%">
                <a-space>
                  <a-switch v-model:checked="travelEnabled" checked-children="开启" un-checked-children="关闭" />
                  <span class="hint">总开关（默认关）</span>
                </a-space>
                <a-space>
                  <a-switch v-model:checked="travelDryRun" checked-children="演练" un-checked-children="实跑" :disabled="!travelEnabled" />
                  <span class="hint">演练模式（默认开）：只看状态、不发写请求</span>
                </a-space>
                <a-space wrap>
                  <a-button size="small" @click="openStreak">查看连登状态</a-button>
                  <a-button size="small" :loading="travelRunning" @click="runTravel(true)">检查旅行（演练）</a-button>
                </a-space>
                <div class="hint">
                  猫猫旅行是<b>纯收益</b>活动（不消耗任何资产）：每天派出 1 趟，猫走 1~4 小时后到站，
                  领取随机 <b>5~10 积分</b>。四个地点（咖啡馆 / 商场店铺 / 健身房 / 古镇客栈）
                  参数<b>完全相同</b>，没有更优解。
                  <br />
                  <b style="color: #d97706">只有国内版账号有这套体系</b>，国际版会被自动跳过
                  （上游没有这个活动，打了也是空响应）。
                  <br />
                  默认保持「演练」——不是为了省钱（本来就不花钱），而是先让你确认
                  <b>状态机判断得对不对</b>：无猫时会先领养，未到门槛（对话量不够）会被上游拒绝，
                  那是<b>预期行为</b>，第二天会自动再试一次。
                </div>
              </a-space>
            </a-form-item>
            <a-form-item label="活跃地图每日提醒">
              <a-space direction="vertical" style="width: 100%">
                <a-space>
                  <a-switch v-model:checked="activeMapEnabled" checked-children="开启" un-checked-children="关闭" />
                  <span class="hint">总开关（默认开）</span>
                </a-space>
                <a-space>
                  <span class="hint">检查时间</span>
                  <a-input-number v-model:value="activeMapHour" :min="0" :max="23" :disabled="!activeMapEnabled" />
                  <span class="hint">点（0-23）</span>
                </a-space>
                <a-space wrap>
                  <a-button size="small" :loading="activeMapRunning" @click="runActiveMapCheck">立即检查</a-button>
                  <a-button size="small" @click="openStreak">查看连登状态</a-button>
                </a-space>
                <div class="hint">
                  到点检查每个账号<b>今天</b>有没有点亮活跃地图，没点亮就提醒你。
                  <br />
                  <b>本任务只读</b>——不写上游、不花积分、不动账号状态，最坏结果就是一条你本来就要的提醒，
                  所以它是全项目唯一<b>默认开启</b>的定时任务，也没有演练档。
                  <br />
                  <b style="color: #d97706">为什么只提醒、不代你发对话</b>：实测（2026-09-22 受控实验）证明
                  <b>本网关的对话请求根本点不亮活跃地图</b>——给当天 score=0 的账号发两次真实请求（都 HTTP 200）
                  后格子仍是 0；历史数据里 31 次请求 / 419 积分的那天同样是 0。活跃地图<b>只由官方客户端驱动</b>，
                  所以收到提醒后请<b>自己到官方客户端聊一句</b>（当天 24:00 前有效）。
                  <br />
                  用 <b>≥</b> 判定时间：服务在检查点之后才启动（本机开发常态）也会补跑一次，
                  否则 23:05 重启就白等一天、连登直接断。当天只跑一次由日期槽位保证。
                  <br />
                  <b>两个提醒渠道</b>：① <b>概览页横幅</b>（零配置，打开 WebUI 就能看到）；
                  ② <b>webhook 推送</b>——地址在「通知与密钥」标签里填，<b>随时可填</b>
                  （那个输入框不受「积分预警 webhook 推送」开关影响，地址是两处共用的）。
                  没配 webhook 也能用，只是不会主动推到你手机上。
                </div>
              </a-space>
            </a-form-item>
            <a-button type="primary" :loading="savingGroup === 'schedule'" @click="saveGroup('schedule')">保存本组</a-button>
          </a-form>
        </a-tab-pane>

        <!-- ---------- 模型 ---------- -->
        <a-tab-pane key="model">
          <template #tab><span><DatabaseOutlined /> 模型</span></template>
          <a-form layout="vertical" style="max-width: 640px">
            <a-form-item label="每日模型目录刷新时间（小时，0-23）">
              <a-input-number v-model:value="modelRefreshHour" :min="0" :max="23" style="width: 100%" />
              <div class="hint" style="margin-top: 4px">
                每天该小时自动从上游拉取可用模型列表（供 /v1/models 与 WebUI 展示）
              </div>
            </a-form-item>
            <a-form-item label="模型缓存 TTL（分钟，1-1440）">
              <a-input-number v-model:value="modelTtlMin" :min="1" :max="1440" style="width: 100%" />
              <div class="hint" style="margin-top: 4px">
                推理端点 /v1/models 在缓存超过该时长后才会惰性刷新（默认 60 分钟）。值越大上游调用越少、但模型列表越旧；WebUI 展示不受此影响（由上方定时刷新控制）。
              </div>
            </a-form-item>
            <a-form-item label="模型别名映射（可选，每行一条：别名=真实模型）">
              <a-textarea
                v-model:value="modelAliases"
                :rows="5"
                :placeholder="`gpt-4o=deepseek-v4-pro
claude-sonnet=glm-5.3`"
              />
              <div class="hint" style="margin-top: 4px">
                让硬编码熟名字的客户端开箱即用：别名会出现在 /v1/models 列表中，请求别名即路由到真实模型（自动套用其思考/上限配置）。
              </div>
            </a-form-item>
            <a-form-item label="系统提示词模式">
              <a-select v-model:value="promptMode" style="width: 100%">
                <a-select-option value="passthrough">不改动（默认）</a-select-option>
                <a-select-option value="append">追加到开头（保留客户端项目规则）</a-select-option>
                <a-select-option value="custom">整段替换</a-select-option>
              </a-select>
              <div class="hint" style="margin-top: 4px">
                「不改动」= 原样透传，本网关不碰请求体，行为与不加这个功能时完全一致。<br />
                「追加到开头」= 把你的提示词插在<b>开头那几条</b> system / developer 消息之后——这是唯一能保住客户端自带项目规则的插法
                （有些客户端会把项目规则塞在开头几条 system 消息里，直接替换会把它们一起吃掉）。<br />
                「整段替换」= 删掉请求里所有 system 消息，只留你写的这段。适合要完全接管模型人格的场景。
              </div>
            </a-form-item>
            <a-form-item label="系统提示词内容">
              <a-textarea
                v-model:value="promptText"
                :rows="6"
                :disabled="promptMode === 'passthrough'"
                placeholder="选择「不改动」时此项不生效；选择另外两种模式后在此填写要注入的内容"
              />
            </a-form-item>
            <a-button type="primary" :loading="savingGroup === 'model'" @click="saveGroup('model')">保存本组</a-button>
          </a-form>
        </a-tab-pane>

        <!-- ---------- 通知与密钥 ---------- -->
        <a-tab-pane key="notify">
          <template #tab><span><BellOutlined /> 通知与密钥</span></template>
          <a-form layout="vertical" style="max-width: 640px">
            <a-form-item label="积分预警 webhook 推送">
              <a-space direction="vertical" style="width: 100%">
                <a-switch v-model:checked="alertEnabled" checked-children="开启" un-checked-children="关闭" />
                <a-input
                  v-model:value="alertWebhookUrl"
                  placeholder="Bark / 企业微信 / 飞书 webhook 地址（自动识别）"
                />
                <a-space>
                  <span class="hint">余额低于</span>
                  <a-input-number v-model:value="alertThreshold" :min="1" :max="90" :disabled="!alertEnabled" style="width: 90px" />
                  <span class="hint">% 或积分</span>
                  <a-input-number v-model:value="alertExpiryDays" :min="1" :max="90" :disabled="!alertEnabled" style="width: 90px" />
                  <span class="hint">天内到期时推送</span>
                </a-space>
                <div class="hint">
                  每 30 分钟随额度刷新检查一次，同一警报 6 小时内只推送一次。Bark 直接粘贴完整地址（含 device key）。
                  <br />
                  <b>这个地址是共用的</b>：「定时任务」里的<b>活跃地图每日提醒</b>也推到它
                  （那个开关独立，但地址只有这一个）。所以这个输入框<b>不受上面开关影响</b>——
                  地址随时可填可改，只是「积分预警」那一路要开了开关才会推。
                </div>
              </a-space>
            </a-form-item>
            <a-form-item label="Artificial Analysis API Key（可选）">
              <a-input-password
                v-model:value="aaKey"
                :placeholder="aaEnabled ? `已配置：${aaKeyMasked}（留空不修改）` : '填入 AA API Key 启用评测数据'"
                autocomplete="new-password"
                style="width: 100%"
                @change="aaClear = false"
              />
              <div class="hint" style="margin-top: 4px">
                用于获取各模型的权威评测（智能 intelligence / 编码 coding / agentic 指数）。在 artificialanalysis.ai 注册生成，免费档 1000 次/天。key 仅存于后端，不暴露给前端。
              </div>
              <a-checkbox
                v-if="aaEnabled"
                v-model:checked="aaClear"
                style="margin-top: 6px"
                @change="onAAClearChange"
              >清除已配置的 Key</a-checkbox>
            </a-form-item>
            <a-button type="primary" :loading="savingGroup === 'notify'" @click="saveGroup('notify')">保存本组</a-button>
          </a-form>
        </a-tab-pane>
      </a-tabs>
    </a-card>

    <!-- 活跃连登 / 补签卡状态：补签判据的唯一界面观察入口。
         演练模式的日志在容器里，没有这个弹窗就只能盲猜判据对不对。 -->
    <a-modal
      v-model:open="streakOpen"
      title="活跃连登与补签卡"
      :footer="null"
      :width="880"
    >
      <div class="hint" style="margin-bottom: 10px">
        上游有<b>两条独立的连登</b>：账号页那个是<b>签到连登</b>（每天签到），
        这张表里的是<b>活跃连登</b>（每天有对话才续上）。
        <b>补签卡属于后者</b>——补的是"昨天没对话"，不是"昨天没签到"。
        <br />
        判据针对的是<b>昨日</b>：昨日活跃地图分数为 0、有补签卡、且没补过，才满足补签条件。
        上游<b>只允许补当月</b>，跨月断档补不了。
      </div>
      <a-table
        :data-source="streakRows"
        :loading="streakLoading"
        row-key="uid"
        size="small"
        :pagination="false"
        :scroll="{ x: 960 }"
      >
        <a-table-column title="账号" key="uid" :width="140">
          <template #default="{ record }">
            <a-tooltip :title="record.uid"><code>{{ record.uid.slice(0, 12) }}…</code></a-tooltip>
          </template>
        </a-table-column>
        <a-table-column title="活跃连登" data-index="streak_days" key="streak_days" :width="90" />
        <a-table-column title="本月活跃" data-index="month_total_days" key="month_total_days" :width="90" />
        <a-table-column title="今日地图" key="heat_today" :width="110">
          <template #default="{ record }">
            <!-- `无数据` 与 `未点亮` 必须分开：前者是"地图里没有今天这一格"（上游换了
                 窗口口径，无判据），后者才是"今天真没活跃"。混为一谈会让人对着一个
                 不存在的判据空着急。 -->
            <a-tooltip
              :title="record.heat_today === null || record.heat_today === undefined
                ? '活跃地图里没有今天这一格（无判据，可能上游换了窗口口径）'
                : (record.heat_today > 0
                  ? '今天已有对话活跃，活跃连登今天不会断'
                  : '今天还没有任何对话活跃，活跃连登今天会断——到官方客户端聊一句即可点亮')"
            >
              <a-tag :color="heatTodayTag(record).color">{{ heatTodayTag(record).text }}</a-tag>
            </a-tooltip>
          </template>
        </a-table-column>
        <a-table-column title="补签卡" key="cards" :width="100">
          <template #default="{ record }">
            <span v-if="record.error">-</span>
            <span v-else>{{ record.makeup_balance ?? 0 }} / {{ record.makeup_max ?? 0 }}</span>
          </template>
        </a-table-column>
        <a-table-column title="连登档位奖励" key="tiers" :width="210">
          <template #default="{ record }">
            <span v-if="record.error">-</span>
            <a-tooltip v-else :title="tierSummary(record)">
              <a-space :size="4" wrap>
                <a-tag
                  v-for="t in ['7d', '14d', '28d']"
                  :key="t"
                  :color="tierTag(record.redemption?.status?.[t]).color"
                >{{ t }} {{ tierTag(record.redemption?.status?.[t]).text }}</a-tag>
              </a-space>
            </a-tooltip>
          </template>
        </a-table-column>
        <a-table-column title="猫猫旅行" key="travel" :width="120">
          <template #default="{ record }">
            <span v-if="record.error">-</span>
            <a-tooltip v-else :title="travelSummary(record)">
              <a-tag :color="travelTag(record).color">{{ travelTag(record).text }}</a-tag>
            </a-tooltip>
          </template>
        </a-table-column>
        <a-table-column title="昨日" data-index="yesterday" key="yesterday" :width="110" />
        <a-table-column title="昨日活跃分" key="yesterday_score" :width="110">
          <template #default="{ record }">
            <!-- 分数为 0 = 那天没有任何对话活动，这是补签的前提条件 -->
            <a-tag v-if="record.yesterday_score === 0" color="blue">0（无活动）</a-tag>
            <a-tag v-else-if="record.yesterday_score === null || record.yesterday_score === undefined" color="default">无数据</a-tag>
            <a-tag v-else color="default">{{ record.yesterday_score }}</a-tag>
          </template>
        </a-table-column>
        <a-table-column title="可补当月" key="allowed" :width="100">
          <template #default="{ record }">
            <a-tag v-if="record.error" color="red">查询失败</a-tag>
            <a-tag v-else-if="record.makeup_allowed" color="green">是</a-tag>
            <a-tag v-else color="orange">跨月</a-tag>
          </template>
        </a-table-column>
        <a-table-column title="已补签日期" key="makeup_dates">
          <template #default="{ record }">
            <span v-if="record.error" style="color: #fb7185; font-size: 12px">{{ record.error }}</span>
            <span v-else-if="record.makeup_dates?.length">{{ record.makeup_dates.join('、') }}</span>
            <span v-else class="hint">无</span>
          </template>
        </a-table-column>
      </a-table>

      <a-divider style="margin: 16px 0 12px" />
      <a-space wrap>
        <a-button size="small" :loading="makeupRunning" @click="runMakeup(true)">检查一次（演练）</a-button>
        <a-popconfirm
          title="确定要实际补签吗？这会花掉账号里的补签卡。"
          ok-text="确认实际补签"
          cancel-text="取消"
          @confirm="runMakeup(false)"
        >
          <a-button size="small" danger :loading="makeupRunning">实际补签</a-button>
        </a-popconfirm>
      </a-space>

      <a-table
        v-if="makeupResults.length"
        :data-source="makeupResults"
        row-key="uid"
        size="small"
        style="margin-top: 12px"
        :pagination="false"
      >
        <a-table-column title="账号" key="uid" :width="140">
          <template #default="{ record }"><code>{{ record.uid.slice(0, 12) }}…</code></template>
        </a-table-column>
        <a-table-column title="结论" key="action" :width="110">
          <template #default="{ record }">
            <a-tag :color="makeupTag(record).color">{{ makeupTag(record).text }}</a-tag>
          </template>
        </a-table-column>
        <a-table-column title="原因" data-index="reason" key="reason" />
      </a-table>
      <div v-if="makeupResults.length" class="hint" style="margin-top: 8px">
        {{ makeupDryRunUsed ? '本次为演练，没有花掉任何补签卡。' : '本次为实际补签。' }}
      </div>

      <a-divider style="margin: 20px 0 12px" />
      <div class="hint" style="margin-bottom: 10px">
        <b>连登档位奖励</b>：活跃连登达到 7 / 14 / 28 天时各可领一次（每月每档一次），
        奖励里有积分、能量和 <b>补签卡</b>。
        这里是<b>补签卡唯一的常规来源</b>——补签卡余额一直是 0，通常就是档位还没达标。
        <br />
        与补签不同，<b>领奖不消耗任何东西</b>；同月重复领同一档位上游会返回"已领取"，无副作用。
      </div>
      <a-space wrap>
        <a-button size="small" :loading="redeemRunning" @click="runRedeem(true, false)">
          查看可领档位（演练）
        </a-button>
        <a-popconfirm
          title="确定要实际领取吗？会把当前可领的档位都领掉。"
          ok-text="确认领取"
          cancel-text="取消"
          @confirm="runRedeem(false, false)"
        >
          <a-button size="small" type="primary" :loading="redeemRunning">实际领取</a-button>
        </a-popconfirm>
        <a-checkbox v-model:checked="redeemDraw">顺带把抽奖次数用掉</a-checkbox>
      </a-space>

      <a-table
        v-if="redeemResults.length"
        :data-source="redeemResults"
        row-key="uid"
        size="small"
        style="margin-top: 12px"
        :pagination="false"
      >
        <a-table-column title="账号" key="uid" :width="140">
          <template #default="{ record }"><code>{{ record.uid.slice(0, 12) }}…</code></template>
        </a-table-column>
        <a-table-column title="结论" key="action" :width="110">
          <template #default="{ record }">
            <a-tag v-if="record.action === 'redeem'" color="green">已发起领取</a-tag>
            <a-tag v-else-if="record.action === 'would-redeem'" color="blue">演练：可领</a-tag>
            <a-tag v-else color="default">跳过</a-tag>
          </template>
        </a-table-column>
        <a-table-column title="档位回执" key="claimed" :width="220">
          <template #default="{ record }">
            <span v-if="!record.claimed?.length" class="hint">—</span>
            <a-space v-else :size="4" wrap>
              <a-tag
                v-for="c in record.claimed"
                :key="c.tier"
                :color="c.ok ? 'green' : (c.normal ? 'default' : 'red')"
              >{{ c.tier }} {{ c.ok ? '已领' : (c.normal ? '正常拒绝' : '失败') }}</a-tag>
            </a-space>
          </template>
        </a-table-column>
        <a-table-column title="原因 / 回执" key="reason">
          <template #default="{ record }">
            <span>{{ record.reason }}</span>
            <div v-if="record.claimed?.length" class="hint">{{ claimedSummary(record) }}</div>
            <div v-if="record.draw" class="hint">{{ drawSummary(record) }}</div>
          </template>
        </a-table-column>
      </a-table>
      <div v-if="redeemResults.length" class="hint" style="margin-top: 8px">
        {{ redeemDryRunUsed ? '本次为演练，没有实际领取任何档位。' : '本次为实际领取。' }}
      </div>

      <a-divider style="margin: 20px 0 12px" />
      <div class="hint" style="margin-bottom: 10px">
        <b>猫猫旅行</b>（<b>只有国内版账号有</b>，国际版会被自动跳过）：
        每天派出 1 趟，猫走 1~4 小时后到站，领取随机 <b>5~10 积分</b>。
        四个地点参数完全相同，没有更优解。
        <br />
        状态机：<b>未领养</b>（先领养，对话量不够会被上游拒绝——那是<b>预期行为</b>，
        第二天自动再试）→ <b>待派出</b> → <b>旅行中</b>（等下一轮）→ <b>已到站</b>（领取）。
        <br />
        与补签/领奖都不同：旅行<b>不消耗任何东西</b>，默认演练只是为了让先你看清状态机判断得对不对。
      </div>
      <a-space wrap>
        <a-button size="small" :loading="travelRunning" @click="runTravel(true)">
          检查旅行（演练）
        </a-button>
        <a-popconfirm
          title="确定要实际执行吗？会向上游真实派出 / 领取猫猫旅行（纯收益，不消耗任何东西）。"
          ok-text="确认执行"
          cancel-text="取消"
          @confirm="runTravel(false)"
        >
          <a-button size="small" type="primary" :loading="travelRunning">实际执行</a-button>
        </a-popconfirm>
      </a-space>

      <a-table
        v-if="travelResults.length"
        :data-source="travelResults"
        row-key="uid"
        size="small"
        style="margin-top: 12px"
        :pagination="false"
      >
        <a-table-column title="账号" key="uid" :width="140">
          <template #default="{ record }"><code>{{ record.uid.slice(0, 12) }}…</code></template>
        </a-table-column>
        <a-table-column title="结论" key="action" :width="130">
          <template #default="{ record }">
            <a-tag :color="travelActionTag(record).color">{{ travelActionTag(record).text }}</a-tag>
          </template>
        </a-table-column>
        <a-table-column title="原因" data-index="reason" key="reason" />
      </a-table>
      <div v-if="travelResults.length" class="hint" style="margin-top: 8px">
        {{ travelDryRunUsed ? '本次为演练，没有发出任何写请求。' : '本次为实际执行。' }}
        <span v-if="!travelDryRunUsed">
          （"跳过"多为正常态：今日已派出过 / 行程进行中 / 对话量未达领养门槛。）
        </span>
      </div>

      <a-divider style="margin: 20px 0 12px" />
      <div class="hint" style="margin-bottom: 10px">
        <b>活跃地图</b>（决定<b>活跃连登</b>能不能续上的东西）：每天在官方客户端有对话才算点亮。
        <br />
        <b style="color: #d97706">本网关的对话请求点不亮它</b>——实测给当天 score=0 的账号发两次真实请求
        （都 HTTP 200）后格子仍是 0，所以这里<b>只能检查 + 提醒</b>，不能代你点亮。
        收到提醒后<b>自己去官方客户端聊一句</b>即可（当天 24:00 前有效）。
      </div>
      <a-space wrap>
        <a-button size="small" :loading="activeMapRunning" @click="runActiveMapCheck">
          立即检查今天点亮没有
        </a-button>
      </a-space>

      <a-table
        v-if="activeMapResults.length"
        :data-source="activeMapResults"
        row-key="uid"
        size="small"
        style="margin-top: 12px"
        :pagination="false"
      >
        <a-table-column title="账号" key="uid" :width="140">
          <template #default="{ record }"><code>{{ record.uid.slice(0, 12) }}…</code></template>
        </a-table-column>
        <a-table-column title="结论" key="action" :width="110">
          <template #default="{ record }">
            <a-tag :color="activeMapActionTag(record).color">{{ activeMapActionTag(record).text }}</a-tag>
          </template>
        </a-table-column>
        <a-table-column title="活跃连登" data-index="streak_days" key="streak_days" :width="90" />
        <a-table-column title="原因" data-index="reason" key="reason" />
      </a-table>
      <div v-if="activeMapResults.length" class="hint" style="margin-top: 8px">
        本次<b>没有</b>推送 webhook（手动检查不通知，免得点一次骚扰一条）；定时任务才会推。
        结果里的「无判据」= 活跃地图里没有今天这一格，不是"未点亮"。
      </div>
    </a-modal>
  </a-spin>
</template>

<style scoped>
/* 说明文字统一色阶：与项目既有次级色一致（#8a94a6 是说明文字的既有取值） */
.hint {
  color: #8a94a6;
  font-size: 12px;
}
</style>
