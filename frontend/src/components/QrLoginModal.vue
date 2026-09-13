<script setup lang="ts">
// 扫码登录弹窗：打开发起 OAuth、每 2s 轮询状态；过期明确提示、成功发 success 事件。
import { ref, watch, onUnmounted } from 'vue'
import { message } from 'ant-design-vue'
import { api, qrUrl } from '@/api/client'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'update:open', v: boolean): void; (e: 'success'): void }>()

const qrLoading = ref(false)
const qrImg = ref('')
const qrAuthUrl = ref('')
let pollTimer: ReturnType<typeof setInterval> | null = null

watch(() => props.open, (open) => {
  if (open) start()
  else stopPolling()
})
onUnmounted(stopPolling)

async function start() {
  qrLoading.value = true
  qrImg.value = ''
  qrAuthUrl.value = ''
  try {
    const res = await api.oauthStart()
    qrAuthUrl.value = res.authUrl
    qrImg.value = qrUrl(res.authUrl)
    startPolling(res.state)
  } catch {
    emit('update:open', false)
  } finally {
    qrLoading.value = false
  }
}

function startPolling(state: string) {
  stopPolling()
  pollTimer = setInterval(async () => {
    try {
      const res = await api.oauthStatus(state)
      if (res.status === 'expired') {
        // state 失效（超时/已在别处完成登录）：停止轮询，明确告知而不是永远转圈
        close()
        message.warning('二维码已过期，请重新点击「扫码登录」获取')
        return
      }
      if (res.status === 'ready') {
        close()
        message.success('扫码登录成功')
        emit('success')
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

function close() {
  stopPolling()
  emit('update:open', false)
}
</script>

<template>
  <a-modal
    :open="props.open"
    title="扫码登录"
    :footer="null"
    :closable="true"
    @cancel="close"
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
</template>
