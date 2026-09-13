<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { message } from 'ant-design-vue'
import { CheckCircleOutlined } from '@ant-design/icons-vue'
import { api } from '@/api/client'
import type { AccountInfo } from '@/types'
import dayjs from 'dayjs'
import CheckinSettingsModal from '@/components/CheckinSettingsModal.vue'
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
const settingsOpen = ref(false)

// 账号状态细分：已禁用 / 冷却中 / 余额不足 / 健康
function statusOf(record: AccountInfo): { label: string; color: string; countdown?: string } {
  if (!record.enabled) return { label: '已禁用', color: 'default' }
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

// 上传 auth 文件
const fileInput = ref<HTMLInputElement | null>(null)
const uploading = ref(false)

const anyUnchecked = computed(() => accounts.value.some((a) => !a.checkin_today))

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
    await api.setEnabled(acc.uid, !acc.enabled)
    message.success(`${acc.enabled ? '已停用' : '已启用'}`)
    load()
  } catch { /* 错误已在拦截器提示 */ }
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
      const state = r.already ? '已签到' : r.ok ? '成功' : '失败'
      return `${r.uid.slice(0, 8)} ${state}`
    })
    const skippedCount = (res.skipped || []).length
    const skipMsg = skippedCount ? `（跳过 ${skippedCount} 个已签到账号）` : ''
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
      <a-button @click="settingsOpen = true" style="margin-left: 4px">自动签到设置</a-button>
      <span v-if="!anyUnchecked && accounts.length" style="margin-left: auto; display: inline-flex; align-items: center; gap: 5px; color: #4ade80; font-size: 12px; background: rgba(34,197,94,0.12); padding: 3px 10px; border-radius: 20px; border: 1px solid rgba(34,197,94,0.3)">
        <CheckCircleOutlined />今日已全部签到
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
      v-if="!loading && accounts.length === 0"
      type="info"
      show-icon
      style="margin-bottom: 12px"
      message="尚未配置任何账号"
      description="点击上方「扫码登录」用 WorkBuddy/CodeBuddy 手机扫码登录，或「上传 auth 文件」导入本地已登录的账号文件。配置后即可开始使用。"
    />

    <a-card title="账号列表">
      <!-- 列宽合计 ~1210px，窄窗口下容器放不下：开横向滚动并固定操作列，
           否则操作按钮会被卡片右缘裁掉（无滚动条可拉） -->
      <a-table
        :data-source="accounts"
        :loading="loading"
        row-key="uid"
        :pagination="false"
        :scroll="{ x: 1210 }"
        table-layout="fixed"
      >
        <a-table-column title="UID" key="uid" :width="200">
          <template #default="{ record }"><a-tooltip :title="record.uid"><code>{{ short(record.uid, 24) }}</code></a-tooltip></template>
        </a-table-column>
        <a-table-column title="来源" key="source" :width="130">
          <template #default="{ record }">
            <a-tag v-if="record.source === 'project'" color="purple">项目 auths</a-tag>
            <a-tag v-else-if="record.source === 'local'" color="geekblue">本机 CodeBuddy</a-tag>
            <a-tag v-else color="default">未知</a-tag>
          </template>
        </a-table-column>
        <a-table-column title="状态" key="healthy" :width="130">
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
            <a-tag v-else :color="statusOf(record).color">{{ statusOf(record).label }}</a-tag>
          </template>
        </a-table-column>
        <a-table-column title="今日签到" key="checkin" :width="90">
          <template #default="{ record }">
            <a-tag :color="record.checkin_today ? 'green' : 'default'">
              {{ record.checkin_today ? '已签到' : '未签到' }}
            </a-tag>
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
                  <div style="font-size: 11px; color: #5c6a8a">已用完的批次不计入最早到期时间</div>
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
        <a-table-column title="优先级" key="priority" :width="150">
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
        <a-table-column title="操作" key="action" :width="150" fixed="right">
          <template #default="{ record }">
            <a-space>
              <a-button size="small" @click="toggle(record)">
                {{ record.enabled ? '停用' : '启用' }}
              </a-button>
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
    <CheckinSettingsModal v-model:open="settingsOpen" />
  </div>
</template>
