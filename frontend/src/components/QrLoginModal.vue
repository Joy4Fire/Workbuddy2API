<script setup lang="ts">
// 扫码登录弹窗：打开发起 OAuth、每 2s 轮询状态；过期明确提示、成功发 success 事件。
// 区域（国内版/国际版）必须显式选择：两者控制面 host 不同，选错拿不到有效二维码。
import { ref, watch, onUnmounted } from 'vue'
import { message } from 'ant-design-vue'
import { api, qrUrl } from '@/api/client'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'update:open', v: boolean): void; (e: 'success'): void }>()

const qrLoading = ref(false)
const qrImg = ref('')
const qrAuthUrl = ref('')
// 默认国内版：老用户（CodeBuddy 账号）无感，国际版用户切一下即可
const region = ref<'cn' | 'global'>('cn')
let pollTimer: ReturnType<typeof setInterval> | null = null

watch(() => props.open, (open) => {
  if (open) start()
  else stopPolling()
})
// 切换区域时重新取码：区域变了二维码就失效了
watch(region, () => { if (props.open) start() })
onUnmounted(stopPolling)

async function start() {
  qrLoading.value = true
  qrImg.value = ''
  qrAuthUrl.value = ''
  try {
    const res = await api.oauthStart(region.value)
    qrAuthUrl.value = res.authUrl
    qrImg.value = qrUrl(res.authUrl)
    // 以服务端回显的 region 为准发起轮询，保证 start / status 打到同一控制面
    startPolling(res.state, res.region === 'global' ? 'global' : 'cn')
  } catch {
    emit('update:open', false)
  } finally {
    qrLoading.value = false
  }
}

function startPolling(state: string, regionId: 'cn' | 'global') {
  stopPolling()
  pollTimer = setInterval(async () => {
    try {
      const res = await api.oauthStatus(state, regionId)
      if (res.status === 'expired') {
        // state 失效（超时/已在别处完成登录）：停止轮询，明确告知而不是永远转圈
        close()
        message.warning('二维码已过期，请重新点击「扫码登录」获取')
        return
      }
      if (res.status === 'ready') {
        close()
        message.success(regionId === 'global' ? '已添加国际版账号' : '已添加国内版账号')
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
    <div style="padding: 4px 0 12px">
      <a-radio-group v-model:value="region" button-style="solid" :disabled="qrLoading">
        <a-radio-button value="cn">国内版</a-radio-button>
        <a-radio-button value="global">国际版</a-radio-button>
      </a-radio-group>
      <!-- 颜色用主题里既有的次级/三级文字色。别用 #999/#666 这类中性灰：
           它们是为浅色底设计的，叠在弹窗底色 #1a2140 上分别只有 5.1:1 / 2.74:1，
           后者连 AA 的 4.5:1 都不到——内联色不受 dark-theme.css 的 !important 影响，
           写错了没有任何地方能兜住。 -->
      <div style="color: #8a94a6; font-size: 12px; margin-top: 6px; line-height: 1.6">
        国内版账号（CodeBuddy / codebuddy.cn）选「国内版」，国际版账号（WorkBuddy / workbuddy.ai）选「国际版」。
        两者控制面不同，选错会拿不到有效二维码。
      </div>
    </div>
    <div style="text-align: center; padding: 12px 0">
      <a-spin :spinning="qrLoading">
        <!-- 这里的白底是**功能性**的，不是漏改深色：二维码识别依赖深色模块/白色静区，
             换成深色底会导致部分手机扫不出来。同理边框保持浅色。 -->
        <div v-if="qrImg" style="display: inline-block; background: #fff; padding: 8px; border: 1px solid #eee; border-radius: 8px">
          <img :src="qrImg" alt="登录二维码" style="width: 220px; height: 220px" />
        </div>
        <div v-else-if="!qrLoading" style="color: #8a94a6">正在获取二维码…</div>
      </a-spin>
      <p style="margin-top: 12px; color: #a5b8d8">
        请用 {{ region === 'global' ? 'WorkBuddy' : 'CodeBuddy' }} 手机端扫码登录
      </p>
      <a v-if="qrAuthUrl" :href="qrAuthUrl" target="_blank" rel="noopener">无法扫码？点此在浏览器打开登录</a>
    </div>
  </a-modal>
</template>
