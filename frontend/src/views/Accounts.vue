<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { message } from 'ant-design-vue'
import { CheckCircleOutlined } from '@ant-design/icons-vue'
import { api, qrUrl } from '@/api/client'
import type { AccountInfo, Settings } from '@/types'

const REFRESH_MS = 20000 // 每 20s 自动刷新
let timer: ReturnType<typeof setInterval> | null = null

const accounts = ref<AccountInfo[]>([])
const loading = ref(false)

// 上传 auth 文件
const fileInput = ref<HTMLInputElement | null>(null)
const uploading = ref(false)

// 扫码登录
const qrOpen = ref(false)
const qrLoading = ref(false)
const qrImg = ref('')
const qrAuthUrl = ref('')
let pollTimer: ReturnType<typeof setInterval> | null = null

// 自动签到设置
const settingsOpen = ref(false)
const settingsSaving = ref(false)
const settings = ref<Settings>({ checkin_hours: '9,21', credit_refresh_min: '30', model_refresh_hour: '6', keepalive_hour: '22' })
const creditMinutes = ref(30)
const modelRefreshHour = ref(6)
const keepaliveHour = ref(22)
const aaKey = ref('')
const aaKeyMasked = ref('')
const aaEnabled = ref(false)
const aaClear = ref(false)

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

// ---------- 扫码登录 ----------
async function openQr() {
  qrOpen.value = true
  qrLoading.value = true
  qrImg.value = ''
  qrAuthUrl.value = ''
  try {
    const res = await api.oauthStart()
    qrAuthUrl.value = res.authUrl
    qrImg.value = qrUrl(res.authUrl)
    startPolling(res.state)
  } catch {
    qrOpen.value = false
  } finally {
    qrLoading.value = false
  }
}

function startPolling(state: string) {
  stopPolling()
  pollTimer = setInterval(async () => {
    try {
      const res = await api.oauthStatus(state)
      if (res.status === 'ready') {
        stopPolling()
        qrOpen.value = false
        message.success('扫码登录成功')
        load()
      }
    } catch { /* 继续轮询 */ }
  }, 2000)
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

function closeQr() {
  stopPolling()
  qrOpen.value = false
}

function onAAClearChange() {
  if (aaClear.value) aaKey.value = ''
}

// ---------- 自动签到设置 ----------
const checkinErr = ref('')

function validateCheckinHours(s: string): boolean {
  const parts = s.split(',').map((p) => p.trim()).filter((p) => p !== '')
  if (parts.length === 0) return false
  for (const p of parts) {
    const n = Number(p)
    if (!/^\d{1,2}$/.test(p) || n < 0 || n > 23) return false
  }
  return true
}

async function openSettings() {
  settingsOpen.value = true
  checkinErr.value = ''
  try {
    const res = await api.getSettings()
    settings.value = res
    creditMinutes.value = parseInt(res.credit_refresh_min, 10) || 30
    modelRefreshHour.value = parseInt(res.model_refresh_hour, 10) || 6
    keepaliveHour.value = parseInt(res.keepalive_hour, 10) || 22
    aaKeyMasked.value = res.aa_api_key_masked || ''
    aaEnabled.value = !!res.aa_enabled
    aaKey.value = ''
  } catch { /* 拦截器已提示 */ }
}

async function saveSettings() {
  const hrs = settings.value.checkin_hours
  if (!validateCheckinHours(hrs)) {
    checkinErr.value = '格式无效：请输入 0-23 之间的整数，逗号分隔（如 9,21）'
    return
  }
  checkinErr.value = ''
  settingsSaving.value = true
  const payload: Partial<Settings> = {
    checkin_hours: settings.value.checkin_hours,
    credit_refresh_min: String(creditMinutes.value),
    model_refresh_hour: String(modelRefreshHour.value),
    keepalive_hour: String(keepaliveHour.value),
  }
  if (aaKey.value) {
    payload.aa_api_key = aaKey.value.trim()
  } else if (aaClear.value) {
    payload.aa_api_key = ''
    payload.clear_aa_api_key = true
  }
  try {
    await api.saveSettings(payload)
    message.success('设置已保存，将按新配置生效')
    settingsOpen.value = false
  } catch { /* 拦截器已提示 */ } finally {
    settingsSaving.value = false
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

// 积分到期倒计时（秒时间戳 → "x 天后 / 今天到期 / 已到期 / -"）
function expiryText(ts: number | null | undefined): string {
  if (!ts) return '-'
  const diffDays = (ts - Date.now() / 1000) / 86400
  if (diffDays <= 0) return '已到期'
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
})

onUnmounted(() => {
  if (timer) clearInterval(timer)
})
</script>

<template>
  <div>
    <div style="margin-bottom: 12px; display: flex; gap: 8px; flex-wrap: wrap; align-items: center">
      <a-button type="primary" :loading="uploading" @click="pickFile">上传 auth 文件</a-button>
      <a-button :loading="qrLoading" @click="openQr">扫码登录</a-button>
      <a-button :disabled="!anyUnchecked" @click="checkin">手动签到</a-button>
      <span style="width: 1px; height: 24px; background: rgba(255,255,255,0.12)"></span>
      <a-button @click="refreshCredits">刷新额度</a-button>
      <a-button @click="load">刷新列表</a-button>
      <a-button @click="openSettings" style="margin-left: 4px">自动签到设置</a-button>
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
      <a-table :data-source="accounts" :loading="loading" row-key="uid" :pagination="false" table-layout="fixed">
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
        <a-table-column title="状态" key="healthy" :width="80">
          <template #default="{ record }">
            <a-tag :color="record.healthy ? 'green' : 'red'">{{ record.healthy ? '健康' : '不可用' }}</a-tag>
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
            <a-tooltip v-if="record.credits_expire_at" :title="new Date(record.credits_expire_at * 1000).toLocaleString()">
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
        <a-table-column title="操作" key="action" :width="150">
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

    <!-- 扫码登录弹窗 -->
    <a-modal
      :open="qrOpen"
      title="扫码登录"
      :footer="null"
      :closable="true"
      @cancel="closeQr"
    >
      <div style="text-align: center; padding: 12px 0">
        <a-spin :spinning="qrLoading">
          <div v-if="qrImg" style="display: inline-block; background: #fff; padding: 8px; border: 1px solid #eee; border-radius: 8px">
            <img :src="qrImg" alt="登录二维码" style="width: 220px; height: 220px" />
          </div>
          <div v-else-if="!qrLoading" style="color: #999">正在获取二维码…</div>
        </a-spin>
        <p style="margin-top: 12px; color: #666">请用 WorkBuddy / CodeBuddy 扫码登录</p>
        <a v-if="qrAuthUrl" :href="qrAuthUrl" target="_blank" rel="noopener">无法扫码？点此在浏览器打开登录</a>
      </div>
    </a-modal>

    <!-- 自动签到设置弹窗 -->
    <a-modal
      :open="settingsOpen"
      title="自动签到设置"
      :confirm-loading="settingsSaving"
      ok-text="保存"
      cancel-text="取消"
      @ok="saveSettings"
      @cancel="settingsOpen = false"
    >
      <a-form layout="vertical">
        <a-form-item
          label="每日自动签到时间（小时，逗号分隔，0-23）"
          :validate-status="checkinErr ? 'error' : ''"
          :help="checkinErr || undefined"
        >
          <a-input v-model:value="settings.checkin_hours" placeholder="例如 9,21" @change="checkinErr=''" />
          <div style="color: #999; font-size: 12px; margin-top: 4px">
            到达设定的小时且当日未签到即自动签到一次，例如 9,21 表示每天 9 点和 21 点各检查一次
          </div>
        </a-form-item>
        <a-form-item label="额度刷新间隔（分钟，1-1440）">
          <a-input-number v-model:value="creditMinutes" :min="1" :max="1440" style="width: 100%" />
        </a-form-item>
        <a-form-item label="每日模型目录刷新时间（小时，0-23）">
          <a-input-number v-model:value="modelRefreshHour" :min="0" :max="23" style="width: 100%" />
          <div style="color: #999; font-size: 12px; margin-top: 4px">
            每天该小时自动从上游拉取可用模型列表（供 /v1/models 与 WebUI 展示）
          </div>
        </a-form-item>
        <a-form-item label="每日 token 保活时间（小时，0-23）">
          <a-input-number v-model:value="keepaliveHour" :min="0" :max="23" style="width: 100%" />
          <div style="color: #999; font-size: 12px; margin-top: 4px">
            每天该小时自动刷新账号 token；session 失效的账号会被自动停用
          </div>
        </a-form-item>
        <a-form-item label="Artificial Analysis API Key（可选）">
          <a-input-password
            v-model:value="aaKey"
            :placeholder="aaEnabled ? `已配置：${aaKeyMasked}（留空不修改）` : '填入 AA API Key 启用评测数据'"
            autocomplete="new-password"
            style="width: 100%"
            @change="aaClear = false"
          />
          <div style="color: #999; font-size: 12px; margin-top: 4px">
            用于获取各模型的权威评测（智能/编码/数学指数、MMLU-Pro、速度、价格）。在 artificialanalysis.ai 注册生成，免费档 1000 次/天。key 仅存于后端，不暴露给前端。
          </div>
          <a-checkbox
            v-if="aaEnabled"
            v-model:checked="aaClear"
            style="margin-top: 6px"
            @change="onAAClearChange"
          >清除已配置的 Key</a-checkbox>        </a-form-item>
      </a-form>
    </a-modal>
  </div>
</template>
