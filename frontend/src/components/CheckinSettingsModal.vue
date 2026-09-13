<script setup lang="ts">
// 自动签到设置弹窗：表单状态自包含，打开时从后端加载，保存成功后关闭。
import { ref, watch } from 'vue'
import { message } from 'ant-design-vue'
import { api } from '@/api/client'
import type { Settings } from '@/types'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'update:open', v: boolean): void }>()

const settingsSaving = ref(false)
const settings = ref<Settings>({ checkin_hours: '9,21', credit_refresh_min: '30', model_refresh_hour: '6', model_ttl_min: '60', aa_refresh_hour: '7', keepalive_hour: '22' })
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

watch(() => props.open, (open) => { if (open) load() })

async function load() {
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
    keepaliveEnabled.value = res.keepalive_enabled !== '0'
    alertEnabled.value = res.alert_enabled === '1'
    alertWebhookUrl.value = res.alert_webhook_url || ''
    alertThreshold.value = parseInt(res.alert_threshold_percent as any, 10) || 10
    alertExpiryDays.value = parseInt(res.alert_expiry_days as any, 10) || 3
    modelAliases.value = res.model_aliases || ''
  } catch { /* 拦截器已提示 */ }
}

function validateCheckinHours(s: string): boolean {
  const parts = s.split(',').map((p) => p.trim()).filter((p) => p !== '')
  if (parts.length === 0) return false
  for (const p of parts) {
    const n = Number(p)
    if (!/^\d{1,2}$/.test(p) || n < 0 || n > 23) return false
  }
  return true
}

function onAAClearChange() {
  if (aaClear.value) aaKey.value = ''
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
    model_ttl_min: String(modelTtlMin.value),
    aa_refresh_hour: String(aaRefreshHour.value),
    keepalive_hour: String(keepaliveHour.value),
    keepalive_enabled: keepaliveEnabled.value ? '1' : '0',
    alert_enabled: alertEnabled.value ? '1' : '0',
    alert_webhook_url: alertWebhookUrl.value.trim(),
    alert_threshold_percent: String(alertThreshold.value),
    alert_expiry_days: String(alertExpiryDays.value),
    model_aliases: modelAliases.value,
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
    emit('update:open', false)
  } catch { /* 拦截器已提示 */ } finally {
    settingsSaving.value = false
  }
}
</script>

<template>
  <a-modal
    :open="props.open"
    title="自动签到设置"
    :confirm-loading="settingsSaving"
    ok-text="保存"
    cancel-text="取消"
    @ok="saveSettings"
    @cancel="emit('update:open', false)"
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
      <a-form-item label="模型缓存 TTL（分钟，1-1440）">
        <a-input-number v-model:value="modelTtlMin" :min="1" :max="1440" style="width: 100%" />
        <div style="color: #999; font-size: 12px; margin-top: 4px">
          推理端点 /v1/models 在缓存超过该时长后才会惰性刷新（默认 60 分钟）。值越大上游调用越少、但模型列表越旧；WebUI 展示不受此影响（由上方定时刷新控制）。
        </div>
      </a-form-item>
      <a-form-item label="每日 AA 评测刷新时间（小时，0-23）">
        <a-input-number v-model:value="aaRefreshHour" :min="0" :max="23" style="width: 100%" />
        <div style="color: #999; font-size: 12px; margin-top: 4px">
          每天该小时自动刷新 Artificial Analysis 评测数据（intelligence / coding / agentic 指数）。需先配置 AA API Key。
        </div>
      </a-form-item>
      <a-form-item label="每日 token 保活">
        <a-space direction="vertical" style="width: 100%">
          <a-switch v-model:checked="keepaliveEnabled" checked-children="开启" un-checked-children="关闭" />
          <div style="color: #999; font-size: 12px">
            开启后每天定时自动刷新账号 token（防止长期不用过期）。关闭则完全不保活。
          </div>
        </a-space>
      </a-form-item>
      <a-form-item label="每日 token 保活时间（小时，0-23）">
        <a-input-number v-model:value="keepaliveHour" :min="0" :max="23" style="width: 100%" :disabled="!keepaliveEnabled" />
        <div style="color: #999; font-size: 12px; margin-top: 4px">
          每天该小时自动刷新账号 token；连续多次失败（session 失效）的账号才会被自动停用
        </div>
      </a-form-item>
      <a-form-item label="积分预警 webhook 推送">
        <a-space direction="vertical" style="width: 100%">
          <a-switch v-model:checked="alertEnabled" checked-children="开启" un-checked-children="关闭" />
          <a-input
            v-model:value="alertWebhookUrl"
            placeholder="Bark / 企业微信 / 飞书 webhook 地址（自动识别）"
            :disabled="!alertEnabled"
          />
          <a-space>
            <span style="font-size: 12px; color: #999">余额低于</span>
            <a-input-number v-model:value="alertThreshold" :min="1" :max="90" :disabled="!alertEnabled" style="width: 90px" />
            <span style="font-size: 12px; color: #999">% 或积分</span>
            <a-input-number v-model:value="alertExpiryDays" :min="1" :max="90" :disabled="!alertEnabled" style="width: 90px" />
            <span style="font-size: 12px; color: #999">天内到期时推送</span>
          </a-space>
          <div style="color: #999; font-size: 12px">
            每 30 分钟随额度刷新检查一次，同一警报 6 小时内只推送一次。Bark 直接粘贴完整地址（含 device key）。
          </div>
        </a-space>
      </a-form-item>
      <a-form-item label="模型别名映射（可选，每行一条：别名=真实模型）">
        <a-textarea
          v-model:value="modelAliases"
          :rows="3"
          :placeholder="`gpt-4o=deepseek-v4-pro
claude-sonnet=glm-5.3`"
        />
        <div style="color: #999; font-size: 12px; margin-top: 4px">
          让硬编码熟名字的客户端开箱即用：别名会出现在 /v1/models 列表中，请求别名即路由到真实模型（自动套用其思考/上限配置）。
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
          用于获取各模型的权威评测（智能 intelligence / 编码 coding / agentic 指数）。在 artificialanalysis.ai 注册生成，免费档 1000 次/天。key 仅存于后端，不暴露给前端。
        </div>
        <a-checkbox
          v-if="aaEnabled"
          v-model:checked="aaClear"
          style="margin-top: 6px"
          @change="onAAClearChange"
        >清除已配置的 Key</a-checkbox>
      </a-form-item>
    </a-form>
  </a-modal>
</template>
