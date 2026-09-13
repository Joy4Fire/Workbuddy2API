<script setup lang="ts">
// 使用记录详情弹窗：打开时按需拉取完整 content（列表是 light 投影，不含大文本）。
import { ref, watch } from 'vue'
import {
  BulbOutlined, RobotOutlined, InboxOutlined, WarningOutlined,
} from '@ant-design/icons-vue'
import { api } from '@/api/client'
import type { UsageRecord } from '@/types'
import dayjs from 'dayjs'

const props = defineProps<{ open: boolean; record: UsageRecord | null }>()
const emit = defineEmits<{ (e: 'update:open', v: boolean): void }>()

const detail = ref<UsageRecord | null>(null)
const detailLoading = ref(false)

watch(() => [props.open, props.record?.id] as const, ([open, id]) => {
  if (!open || !id) return
  detail.value = props.record  // 先展示列表里的元数据
  detailLoading.value = true
  api.usageDetail(id)
    .then((res) => { detail.value = res.record })
    .catch(() => { /* 拦截器已提示 */ })
    .finally(() => { detailLoading.value = false })
})

function fmtTime(ts: number) {
  return dayjs(ts * 1000).format('YYYY-MM-DD HH:mm:ss')
}

function short(s: string | null | undefined, n = 8) {
  if (!s) return '-'
  const str = String(s).replace(/\s+/g, ' ')
  return str.length > n ? str.slice(0, n) + '…' : str
}
</script>

<template>
  <a-modal
    :open="props.open"
    :title="detail ? `请求详情 · ${detail.model} (${detail.protocol})` : '请求详情'"
    :footer="null"
    width="720px"
    @cancel="emit('update:open', false)"
  >
    <a-spin :spinning="detailLoading" tip="加载详情…">
      <div v-if="detail">
        <div style="margin-bottom: 12px; color: #8a94a6; font-size: 12px">
          {{ fmtTime(detail.ts) }} · {{ detail.input_tokens || 0 }}/{{ detail.output_tokens || 0 }} tokens ·
          {{ Math.round(detail.latency_ms || 0) }}ms
          <template v-if="detail.credits"> · <span style="color: #fbbf24">{{ detail.credits.toFixed(2) }} 积分</span></template>
          · 账号 {{ short(detail.account_uid) }}
          <a-tag :color="detail.status === 'ok' ? 'green' : 'red'" style="margin-left: 6px">{{ detail.status }}</a-tag>
        </div>

        <!-- 思考链 / COT -->
        <div v-if="detail.reasoning_content" class="sec">
          <div class="sec-title reasoning"><BulbOutlined style="margin-right: 5px" />思考链 (COT)</div>
          <pre class="content-box reasoning">{{ detail.reasoning_content }}</pre>
        </div>

        <!-- 输出 -->
        <div class="sec">
          <div class="sec-title output"><RobotOutlined style="margin-right: 5px" />模型输出</div>
          <pre class="content-box" v-if="detail.output_content">{{ detail.output_content }}</pre>
          <div v-else class="empty">（无输出内容）</div>
        </div>

        <!-- 输入 -->
        <div class="sec">
          <div class="sec-title input"><InboxOutlined style="margin-right: 5px" />请求输入</div>
          <pre class="content-box input" v-if="detail.input_content">{{ detail.input_content }}</pre>
          <div v-else class="empty">（无输入内容）</div>
        </div>

        <div v-if="detail.error" class="sec">
          <div class="sec-title" style="color: #ff6b6b"><WarningOutlined style="margin-right: 5px" />错误</div>
          <pre class="content-box error">{{ detail.error }}</pre>
        </div>
      </div>
    </a-spin>
  </a-modal>
</template>

<style scoped>
.sec { margin-top: 14px; }
.sec-title { font-size: 13px; font-weight: 700; margin-bottom: 6px; color: #e6edf7; }
.sec-title.reasoning { color: #c4b5fd; }
.sec-title.output { color: #7cc0f5; }
.sec-title.input { color: #86efac; }
.content-box {
  background: #121a30; border: 1px solid rgba(99,179,237,0.15);
  border-radius: 8px; padding: 12px; max-height: 260px; overflow: auto;
  white-space: pre-wrap; word-break: break-word;
  font-size: 12.5px; line-height: 1.55; color: #cdd6e8; margin: 0;
}
.content-box.reasoning { border-color: rgba(167,139,250,0.3); background: rgba(76,29,149,0.12); color: #ddd6fe; }
.content-box.input { border-color: rgba(34,197,94,0.25); }
.content-box.error { border-color: rgba(244,63,94,0.4); color: #fb7185; }
.empty { color: #6b7794; font-size: 12px; }
</style>
