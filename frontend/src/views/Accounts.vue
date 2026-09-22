<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import { CheckCircleOutlined } from '@ant-design/icons-vue'
import { api } from '@/api/client'
import type { AccountInfo } from '@/types'
import dayjs from 'dayjs'
import QrLoginModal from '@/components/QrLoginModal.vue'

const REFRESH_MS = 20000 // 每 20s 自动刷新
let timer: ReturnType<typeof setInterval> | null = null
let nowTimer: ReturnType<typeof setInterval> | null = null

const accounts = ref<AccountInfo[]>([])
const loading = ref(false)

// 当前时间（秒，每秒刷新），用于冷却倒计时与状态实时判定
const now = ref(Date.now() / 1000)

// 弹窗开关（内容与轮询逻辑在各自的组件内）
const qrOpen = ref(false)
// 设置已独立成页面（/settings），这里只负责跳转
const router = useRouter()

// 账号状态细分：已禁用 / 登录态已加密 / 系统已停用 / 冷却中 / 余额不足 / 健康
//
// 注意「已禁用」与「系统已停用」是**两个独立状态位**，不能合并：
// 前者是运维手动停用（`enabled=false`），后者是系统判定故障（session 失效 / 被上游封禁，
// `auto_disabled_reason` 非空）——系统停用的账号 `enabled` 仍是 true，
// 所以这里必须单独判一次，否则会显示成「健康」。
function statusOf(record: AccountInfo): { label: string; color: string; countdown?: string } {
  if (!record.enabled) return { label: '已禁用', color: 'default' }
  // 加密登录态排在冷却之前：冷却会自愈，加密不会——必须先让用户看到真正的原因
  if (isEncrypted(record)) return { label: '登录态已加密', color: 'orange' }
  // 系统自动禁用排在冷却之前：它不会自愈，且需要用户重新登录才能恢复
  if (record.auto_disabled_reason) return { label: '系统已停用', color: 'red' }
  if (record.cooldown_until && record.cooldown_until > now.value) {
    const sec = Math.ceil(record.cooldown_until - now.value)
    const countdown = sec < 60 ? `${sec} 秒` : `${Math.ceil(sec / 60)} 分钟`
    return { label: '冷却中', color: 'orange', countdown }
  }
  if (record.credits_remaining !== null && record.credits_remaining !== undefined && record.credits_remaining <= 0) {
    return { label: '余额不足', color: 'red' }
  }
  return { label: '健康', color: 'green' }
}

/**
 * 「今日签到」列的三态展示。
 *
 * 关键：`checkin_active === false`（本期活动未开启）必须与「未签到」区分开 ——
 * 前者是"签不了"，后者才是"没签"，混在一起就是用户看到的那个说不清的状态。
 * tooltip 说明数据来源，避免用户以为这里的"未签到"等于官方客户端的状态。
 */
function checkinOf(record: AccountInfo): { label: string; color: string; tip: string } {
  const srcTip =
    record.checkin_source === 'upstream'
      ? '来自上游实时查询'
      : '上游状态暂不可得，此处显示的是本网关的签到记录（不是官方客户端的状态）'
  if (record.checkin_active === false) {
    return { label: '活动未开启', color: 'default', tip: `本期签到活动未开启，该账号暂时无法签到（${srcTip}）` }
  }
  if (record.checkin_today) return { label: '已签到', color: 'green', tip: srcTip }
  return { label: '未签到', color: 'orange', tip: srcTip }
}

// 登录态是否被 $wbEncrypted 加密（桌面端 5.6.0+）。加密的账号当前不可用，
// 且**不能**靠"刷新额度/token 保活"恢复——必须换明文 auth 或提供官方客户端。
function isEncrypted(record: AccountInfo): boolean {
  return !!record.auth_encrypted_fields?.length
}

// ---- 模型级冷却（P0-2）----
// 与账号级冷却不同：账号整体仍可用，只是**某几个模型**暂时不能打。
// 典型来源：6004 模型级限流、11102 该模型对该账号不可用。
function modelCooldownCount(record: AccountInfo): number {
  return Object.keys(record.model_cooldowns || {}).length
}

function modelCooldownTip(record: AccountInfo): string {
  const nowSec = now.value
  const parts = Object.entries(record.model_cooldowns || {}).map(([model, until]) => {
    const sec = Math.max(0, Math.ceil(until - nowSec))
    const left = sec < 60 ? `${sec} 秒` : `${Math.ceil(sec / 60)} 分钟`
    return `${model}（${left}后恢复）`
  })
  return `该账号对这些模型暂时不可用，换模型或换账号可立即使用：\n${parts.join('\n')}`
}

function encryptedTip(record: AccountInfo): string {
  const fields = (record.auth_encrypted_fields || []).join('、')
  return `WorkBuddy 桌面端 5.6.0+ 加密了登录态（${fields}），本机无法解密。`
    + '请改用「上传 auth 文件」导入明文凭据，或配置 WORKBUDDY_EXE 指向官方客户端安装路径。'
}

const encryptedAccounts = computed(() => accounts.value.filter(isEncrypted))

// 区域标签：国内版（蓝）/ 国际版（紫）。区域由 auth 文件的 domain 自动判定，
// 不做成用户可改的字段——避免与真实凭据不一致。
function regionTag(record: AccountInfo): { text: string; color: string } {
  if (record.region === 'global') return { text: record.region_label || '国际版', color: 'purple' }
  return { text: record.region_label || '国内版', color: 'blue' }
}

// 上传 auth 文件
const fileInput = ref<HTMLInputElement | null>(null)
const uploading = ref(false)

// 是否有"还能签"的账号 —— 决定「手动签到」按钮是否可点。
// 活动未开启（checkin_active === false）的账号签不了，必须排除，
// 否则这种账号会让按钮永远亮着，点了又必然"跳过"，很误导。
const anyUnchecked = computed(() =>
  accounts.value.some((a) => !a.checkin_today && a.checkin_active !== false))

// 顶部状态徽标。区分"真的都签了"和"剩下的都签不了"——
// 两种情况都不该显示成"今日已全部签到"，否则用户会以为签到被漏掉了。
const checkinSummary = computed<{ text: string; ok: boolean } | null>(() => {
  if (!accounts.value.length || anyUnchecked.value) return null
  if (accounts.value.every((a) => a.checkin_today)) return { text: '今日已全部签到', ok: true }
  return { text: '无待签到账号（活动未开启）', ok: false }
})

async function load() {
  loading.value = true
  try {
    const res = await api.accounts()
    accounts.value = res.accounts
  } finally {
    loading.value = false
  }
}

async function toggle(acc: AccountInfo) {
  try {
    const res = await api.setEnabled(acc.uid, !acc.enabled)
    if (!acc.enabled && res.auto_disabled_reason) {
      // 人工位已清，但系统位还在 —— 必须显式告诉用户，否则"点了启用还是用不了"
      // 会被当成 bug。两个位独立清除，解除系统位是另一个按钮。
      message.warning(`已解除手动停用，但系统停用仍生效：${res.auto_disabled_reason}`)
    } else {
      message.success(`${acc.enabled ? '已停用' : '已启用'}`)
    }
    load()
  } catch { /* 错误已在拦截器提示 */ }
}

/**
 * 只解除**系统**自动停用位（不动手动停用位）。
 *
 * 与「启用」分开的意义：一个账号可能同时被人为停用和被系统停用，用户可能只想解除其中一个。
 * 两个位独立清除，语义才不互相污染。
 */
async function clearAutoDisable(acc: AccountInfo) {
  try {
    await api.clearAutoDisable(acc.uid)
    message.success('已解除系统停用')
    load()
  } catch { /* 拦截器已提示 */ }
}

async function refreshCredits() {
  try {
    const res = await api.refreshCredits()
    accounts.value = res.accounts
    message.success('额度已刷新')
  } catch { /* 拦截器已提示 */ }
}

async function checkin() {
  try {
    const res = await api.checkin()
    const parts = (res.results || []).map((r) => {
      // inactive（活动未开启）既不是成功也不是失败，单独标出来，
      // 否则用户会以为是签到出错了
      const state = r.already ? '已签到' : r.inactive ? '活动未开启' : r.ok ? '成功' : '失败'
      return `${r.uid.slice(0, 8)} ${state}`
    })
    const skippedCount = (res.skipped || []).length
    const skipMsg = skippedCount ? `（跳过 ${skippedCount} 个已签到/活动未开启账号）` : ''
    message.success(`签到结果：${parts.join(', ') || '无账号'}${skipMsg}`)
    load()
  } catch { /* 拦截器已提示 */ }
}

// ---------- 上传 auth 文件 ----------
function pickFile() {
  fileInput.value?.click()
}

async function onFileChange(e: Event) {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  uploading.value = true
  try {
    const res = await api.uploadAuth(file)
    message.success(`已导入账号 ${res.account.uid.slice(0, 8)}…`)
    load()
  } catch { /* 拦截器已提示 */ } finally {
    uploading.value = false
    input.value = ''
  }
}

// ---------- 删除账号 ----------
const deletingUid = ref('')
async function removeAccount(acc: AccountInfo) {
  deletingUid.value = acc.uid
  try {
    await api.deleteAccount(acc.uid)
    message.success(`已删除账号 ${short(acc.uid)}`)
    load()
  } catch { /* 拦截器已提示 */ } finally {
    deletingUid.value = ''
  }
}

function short(s: string | null | undefined, n = 8) {
  const str = s ? String(s) : ''
  return str.length > n ? str.slice(0, n) + '…' : (str || '-')
}

// 积分到期倒计时（秒时间戳 → "x 天后 / 今天到期 / 已于 MM-DD 到期 / -"）
function expiryText(ts: number | null | undefined): string {
  if (!ts) return '-'
  const diffDays = (ts - Date.now() / 1000) / 86400
  if (diffDays <= 0) return `已于 ${dayjs(ts * 1000).format('MM-DD')} 到期`
  if (diffDays < 1) return '今天到期'
  if (diffDays < 30) return `${Math.ceil(diffDays)} 天后`
  return `${Math.round(diffDays / 30)} 个月后`
}
function expiryColor(ts: number | null | undefined): string {
  if (!ts) return ''
  const diffDays = (ts - Date.now() / 1000) / 86400
  if (diffDays <= 3) return '#ef4444'
  if (diffDays <= 7) return '#f59e0b'
  return '#4ade80'
}

// 积分构成明细（悬浮弹层用）：与官方控制台"积分明细"同口径
function fmtNum(n: number | null | undefined): string {
  return Number(n ?? 0).toLocaleString('zh-CN', { maximumFractionDigits: 1 })
}
function remainPct(remain: number, total: number): number {
  if (!total) return 0
  return Math.max(0, Math.min(100, Math.round((remain / total) * 100)))
}
function fmtDateTime(ts: number | null | undefined): string {
  if (!ts) return '-'
  return dayjs(ts * 1000).format('YYYY/MM/DD HH:mm:ss')
}

// 优先级编辑
const editingPriority = ref<Record<string, number>>({})
async function savePriority(acc: AccountInfo) {
  const val = Math.max(0, Math.min(100, Math.round(editingPriority.value[acc.uid] ?? acc.priority ?? 0)))
  editingPriority.value[acc.uid] = val
  try {
    await api.setPriority(acc.uid, val)
    message.success(`已设置优先级 ${val}`)
    load()
  } catch { /* 拦截器已提示 */ }
}

onMounted(() => {
  load()
  timer = setInterval(load, REFRESH_MS)
  // 每秒刷新 now，驱动冷却倒计时与状态实时变化
  nowTimer = setInterval(() => { now.value = Date.now() / 1000 }, 1000)
})

onUnmounted(() => {
  if (timer) clearInterval(timer)
  if (nowTimer) clearInterval(nowTimer)
})
</script>

<template>
  <div>
    <div style="margin-bottom: 12px; display: flex; gap: 8px; flex-wrap: wrap; align-items: center">
      <a-button type="primary" :loading="uploading" @click="pickFile">上传 auth 文件</a-button>
      <a-button @click="qrOpen = true">扫码登录</a-button>
      <a-button :disabled="!anyUnchecked" @click="checkin">手动签到</a-button>
      <span style="width: 1px; height: 24px; background: rgba(255,255,255,0.12)"></span>
      <a-button @click="refreshCredits">刷新额度</a-button>
      <a-button @click="load">刷新列表</a-button>
      <a-button @click="router.push('/settings')" style="margin-left: 4px">设置</a-button>
      <span
        v-if="checkinSummary"
        :style="{
          marginLeft: 'auto', display: 'inline-flex', alignItems: 'center', gap: '5px',
          fontSize: '12px', padding: '3px 10px', borderRadius: '20px',
          color: checkinSummary.ok ? '#4ade80' : '#a5b8d8',
          background: checkinSummary.ok ? 'rgba(34,197,94,0.12)' : 'rgba(148,163,184,0.14)',
          border: checkinSummary.ok ? '1px solid rgba(34,197,94,0.3)' : '1px solid rgba(148,163,184,0.35)',
        }"
      >
        <CheckCircleOutlined />{{ checkinSummary.text }}
      </span>
    </div>
    <input
      ref="fileInput"
      type="file"
      accept=".info,.json"
      style="display: none"
      @change="onFileChange"
    />

    <a-alert
      v-if="encryptedAccounts.length"
      type="warning"
      show-icon
      style="margin-bottom: 12px"
      :message="`${encryptedAccounts.length} 个账号的登录态已被加密，当前无法使用`"
      description="WorkBuddy 桌面端 5.6.0+ 会把 accessToken / refreshToken 加密后落盘，密钥只存在于官方客户端里。请上传明文 auth 文件，或配置 WORKBUDDY_EXE 指向官方客户端安装路径。加密文件不会被本服务回写（写回明文会让官方客户端认不出自己的登录态）。"
    />

    <a-alert
      v-if="!loading && accounts.length === 0"
      type="info"
      show-icon
      style="margin-bottom: 12px"
      message="尚未配置任何账号"
      description="点击上方「扫码登录」用 WorkBuddy/CodeBuddy 手机扫码登录，或「上传 auth 文件」导入本地已登录的账号文件。配置后即可开始使用。"
    />

    <a-card title="账号列表">
      <!-- 列宽合计 ~1420px，窄窗口下容器放不下：开横向滚动并固定操作列，
           否则操作按钮会被卡片右缘裁掉（无滚动条可拉） -->
      <a-table
        :data-source="accounts"
        :loading="loading"
        row-key="uid"
        :pagination="false"
        :scroll="{ x: 1420 }"
        table-layout="fixed"
      >
        <a-table-column title="UID" key="uid" :width="200">
          <template #default="{ record }"><a-tooltip :title="record.uid"><code>{{ short(record.uid, 24) }}</code></a-tooltip></template>
        </a-table-column>
        <a-table-column title="区域" key="region" :width="110">
          <template #default="{ record }">
            <!-- 区域由 auth 文件的 domain 自动判定，鼠标悬浮可看到原始域名 -->
            <a-tooltip :title="record.domain ? `域名：${record.domain}` : '域名未知（按国内版处理）'">
              <a-tag :color="regionTag(record).color">{{ regionTag(record).text }}</a-tag>
            </a-tooltip>
          </template>
        </a-table-column>
        <a-table-column title="来源" key="source" :width="130">
          <template #default="{ record }">
            <a-tag v-if="record.source === 'project'" color="purple">项目 auths</a-tag>
            <a-tag v-else-if="record.source === 'local'" color="geekblue">本机 CodeBuddy</a-tag>
            <a-tag v-else color="default">未知</a-tag>
          </template>
        </a-table-column>
        <a-table-column title="状态" key="healthy" :width="170">
          <template #default="{ record }">
            <template v-if="statusOf(record).label === '冷却中'">
              <a-tooltip :title="`${statusOf(record).countdown}后恢复`">
                <a-tag color="orange">{{ statusOf(record).label }}</a-tag>
              </a-tooltip>
              <span style="font-size: 12px; color: #d97706">{{ statusOf(record).countdown }}后恢复</span>
            </template>
            <template v-else-if="!record.enabled">
              <a-tooltip :title="record.disabled_reason || '已禁用'">
                <a-tag color="default">已禁用</a-tag>
              </a-tooltip>
              <div v-if="record.disabled_reason" style="font-size: 11px; color: #8a94a6; line-height: 1.3; margin-top: 2px">
                {{ record.disabled_reason }}
              </div>
            </template>
            <template v-else-if="isEncrypted(record)">
              <a-tooltip :title="encryptedTip(record)">
                <a-tag color="orange">登录态已加密</a-tag>
              </a-tooltip>
              <div style="font-size: 11px; color: #8a94a6; line-height: 1.3; margin-top: 2px">
                需明文 auth 文件
              </div>
            </template>
            <!-- 系统自动停用（session 失效 / 被上游封禁）。enabled 仍为 true，
                 所以必须单独判一次，否则会落到下面显示成「健康」。 -->
            <template v-else-if="record.auto_disabled_reason">
              <a-tooltip :title="`系统自动停用：${record.auto_disabled_reason}。需要重新登录或手动解除。`">
                <a-tag color="red">系统已停用</a-tag>
              </a-tooltip>
              <div style="font-size: 11px; color: #8a94a6; line-height: 1.3; margin-top: 2px">
                {{ record.auto_disabled_reason }}
              </div>
            </template>
            <a-tag v-else :color="statusOf(record).color">{{ statusOf(record).label }}</a-tag>
            <!-- 模型级冷却：账号整体是健康的，但对**某个模型**暂时不可用。
                 不显示出来用户只会看到"某个模型时好时坏"，无从解释。
                 与账号级冷却分开表达——它们的恢复时刻与范围都不同。 -->
            <div
              v-if="modelCooldownCount(record)"
              style="font-size: 11px; color: #d97706; line-height: 1.3; margin-top: 2px; cursor: help"
              :title="modelCooldownTip(record)"
            >
              {{ modelCooldownCount(record) }} 个模型冷却中
            </div>
            <!-- 在途请求数：并发上限（P1-1）的可见入口，否则这个限制是隐形的 -->
            <div
              v-if="record.in_flight"
              style="font-size: 11px; color: #8a94a6; line-height: 1.3; margin-top: 2px"
            >
              在途 {{ record.in_flight }}
            </div>
          </template>
        </a-table-column>
        <a-table-column title="今日签到" key="checkin" :width="104">
          <template #default="{ record }">
            <a-tooltip :title="checkinOf(record).tip">
              <a-tag :color="checkinOf(record).color">{{ checkinOf(record).label }}</a-tag>
            </a-tooltip>
          </template>
        </a-table-column>
        <a-table-column title="额度剩余" data-index="credits_remaining" key="credits_remaining" :width="90">
          <template #default="{ record }">{{ record.credits_remaining ?? '-' }}</template>
        </a-table-column>
        <a-table-column title="额度总量" data-index="credits_total" key="credits_total" :width="90">
          <template #default="{ record }">{{ record.credits_total ?? '-' }}</template>
        </a-table-column>
        <a-table-column title="积分到期" key="expiry" :width="110">
          <template #default="{ record }">
            <!-- 有积分构成明细时悬浮展示（与官方控制台"积分明细"同口径） -->
            <a-popover
              v-if="record.credit_packages && record.credit_packages.length"
              trigger="hover"
              placement="left"
            >
              <template #content>
                <div style="min-width: 300px">
                  <div style="font-weight: 700; color: #e6edf7; margin-bottom: 10px">积分构成</div>
                  <div v-for="p in record.credit_packages" :key="p.name" style="margin-bottom: 12px">
                    <div style="display: flex; justify-content: space-between; gap: 12px; font-size: 12px; color: #cdd6e8">
                      <span>{{ p.name }}</span>
                      <span style="color: #8a94a6; white-space: nowrap">剩余 {{ remainPct(p.remain, p.total) }}%</span>
                    </div>
                    <a-progress
                      :percent="remainPct(p.remain, p.total)"
                      :show-info="false"
                      size="small"
                      stroke-color="#63b3ed"
                      trail-color="rgba(255,255,255,0.08)"
                      style="margin: 2px 0 4px"
                    />
                    <div style="font-size: 12px; color: #8a94a6">
                      已使用 {{ fmtNum(p.used) }} / {{ fmtNum(p.total) }}
                      <template v-if="p.expire_at"> · 最早到期 {{ fmtDateTime(p.expire_at) }}</template>
                    </div>
                  </div>
                  <div style="font-size: 11px; color: #7d8aa5">已用完的批次不计入最早到期时间</div>
                </div>
              </template>
              <span
                v-if="record.credits_expire_at"
                :style="{ color: expiryColor(record.credits_expire_at), fontWeight: 600, cursor: 'help', borderBottom: '1px dashed rgba(255,255,255,0.25)' }"
              >{{ expiryText(record.credits_expire_at) }}</span>
              <span v-else style="cursor: help; border-bottom: 1px dashed rgba(255,255,255,0.25)">
                {{ fmtNum(record.credits_remaining ?? 0) }} 积分
              </span>
            </a-popover>
            <a-tooltip v-else-if="record.credits_expire_at" :title="fmtDateTime(record.credits_expire_at)">
              <span :style="{ color: expiryColor(record.credits_expire_at), fontWeight: 600 }">{{ expiryText(record.credits_expire_at) }}</span>
            </a-tooltip>
            <span v-else>-</span>
          </template>
        </a-table-column>
        <a-table-column title="优先级" key="priority" :width="170">
          <template #default="{ record }">
            <a-input-number
              :value="editingPriority[record.uid] ?? record.priority ?? 0"
              :min="0"
              :max="100"
              :step="1"
              size="small"
              style="width: 90px"
              @change="(v: number | null) => { editingPriority[record.uid] = v ?? 0 }"
            />
            <a-button size="small" type="link" style="padding: 0 4px" @click="savePriority(record)">保存</a-button>
          </template>
        </a-table-column>
        <a-table-column title="失败数" data-index="failure_count" key="failure_count" :width="70" />
        <a-table-column title="操作" key="action" :width="210" fixed="right">
          <template #default="{ record }">
            <a-space wrap>
              <a-button size="small" @click="toggle(record)">
                {{ record.enabled ? '停用' : '启用' }}
              </a-button>
              <!-- 只在系统位真的置上时才出现：没有这个位就没有"解除"的语义 -->
              <a-popconfirm
                v-if="record.auto_disabled_reason"
                title="解除系统停用？请确认故障已恢复（如已重新登录），否则下一个请求会立刻再次触发。"
                ok-text="解除"
                cancel-text="取消"
                @confirm="clearAutoDisable(record)"
              >
                <a-button size="small">解除系统停用</a-button>
              </a-popconfirm>
              <a-popconfirm
                title="确认删除该账号？"
                ok-text="删除"
                cancel-text="取消"
                @confirm="removeAccount(record)"
              >
                <a-button size="small" danger :loading="deletingUid === record.uid">删除</a-button>
              </a-popconfirm>
            </a-space>
          </template>
        </a-table-column>
      </a-table>
    </a-card>

    <QrLoginModal v-model:open="qrOpen" @success="load" />
  </div>
</template>
