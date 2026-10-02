<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import dayjs from 'dayjs'
import { api } from '@/api/client'
import type { UpdateInfo } from '@/types'

const info = ref<UpdateInfo | null>(null)
const checking = ref(false)
const labels = {
  unchecked: '尚未检查', update_available: '发现新版本', local_ahead: '本地版本领先源仓库',
  up_to_date: '与源仓库版本一致', unavailable: '本次检查未成功',
}
const statusLabel = computed(() => info.value ? labels[info.value.status] : '正在读取版本信息')
async function check() {
  checking.value = true
  try { info.value = await api.checkUpdates() } catch { /* 拦截器已提示 */ }
  finally { checking.value = false }
}
onMounted(async () => {
  try { info.value = await api.getUpdates() } catch { /* 拦截器已提示 */ }
})
</script>

<template>
  <div class="updates-panel">
    <div class="updates-title">网关版本</div>
    <p>当前版本 <b>{{ info ? `v${info.current_version}` : '…' }}</b></p>
    <p aria-live="polite" :class="{ 'updates-warning': info?.status === 'unavailable' }">{{ statusLabel }}</p>
    <p v-if="info?.latest_version">{{ info.status === 'unavailable' ? '上次查到的源仓库版本' : '源仓库版本' }}：v{{ info.latest_version }}</p>
    <p v-if="info?.checked_at" class="updates-hint">检查时间：{{ dayjs(info.checked_at * 1000).format('YYYY-MM-DD HH:mm:ss') }}</p>
    <p v-if="info?.error" class="updates-warning">{{ info.error }}</p>
    <a-space wrap>
      <a-button :loading="checking" @click="check">检查更新</a-button>
      <a v-if="info" :href="info.project_url" target="_blank" rel="noopener noreferrer">查看源仓库</a>
    </a-space>
    <p class="updates-hint" style="margin-top: 16px">点击后核对源仓库主分支的稳定版本号。成功结果缓存 6 小时，失败后 1 分钟可重试。</p>
    <p class="updates-hint">Docker 部署：更新项目代码后重新构建并启动容器，已有账号与使用记录会保留。</p>
  </div>
</template>

<style scoped>
.updates-panel { max-width: 640px; color: #cdd6e8; }
.updates-title { font-size: 16px; font-weight: 600; margin-bottom: 16px; color: #e6edf7; }
.updates-hint { color: #8a94a6; font-size: 12px; }
.updates-warning { color: #fbbf24; }
</style>
