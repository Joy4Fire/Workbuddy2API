<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { BulbOutlined, RobotOutlined, InboxOutlined, WarningOutlined, ReloadOutlined, FilterOutlined } from '@ant-design/icons-vue'
import { api } from '@/api/client'
import type { UsageRecord } from '@/types'
import dayjs from 'dayjs'

const records = ref<UsageRecord[]>([])
const loading = ref(false)
const limit = ref(100)

// 筛选项（协议 / 模型 / 应用 / 状态）
const filters = ref<{ protocol?: string; model?: string; app_name?: string; status?: string }>({})
const filterOptions = ref<{ protocols: string[]; models: string[]; apps: string[]; statuses: string[] }>(
  { protocols: [], models: [], apps: [], statuses: [] },
)

const detailVisible = ref(false)
const detail = ref<UsageRecord | null>(null)

async function load() {
  loading.value = true
  try {
    const res = await api.usageRecent(limit.value, filters.value)
    records.value = res.records
  } finally {
    loading.value = false
  }
}

async function loadFilters() {
  filterOptions.value = await api.usageFilters()
}

function onFilterChange() {
  load()
}

function clearFilters() {
  filters.value = {}
  load()
}

const hasFilter = () => Object.values(filters.value).some((v) => !!v)

function openDetail(r: UsageRecord) {
  detail.value = r
  detailVisible.value = true
}

function fmtTime(ts: number) {
  return dayjs(ts * 1000).format('YYYY-MM-DD HH:mm:ss')
}

function short(s: string | null | undefined, n = 8) {
  if (!s) return '-'
  const str = String(s).replace(/\s+/g, ' ')
  return str.length > n ? str.slice(0, n) + '…' : str
}

function fmtLatency(ms: number | null | undefined) {
  const v = ms ?? 0
  if (v >= 60000) return `${(v / 60000).toFixed(1)}m`
  return `${(v / 1000).toFixed(1)}s`
}

onMounted(async () => {
  await loadFilters()
  await load()
})
</script>

<template>
  <div>
    <div style="margin-bottom: 12px; display: flex; gap: 8px; align-items: center; flex-wrap: wrap">
      <span style="color: #8a94a6; font-size: 13px; display: inline-flex; align-items: center; gap: 5px">
        <FilterOutlined />筛选
      </span>
      <a-select v-model:value="filters.protocol" placeholder="全部协议" allow-clear style="width: 130px" @change="onFilterChange">
        <a-select-option v-for="p in filterOptions.protocols" :key="p" :value="p">{{ p }}</a-select-option>
      </a-select>
      <a-select v-model:value="filters.model" placeholder="全部模型" allow-clear show-search style="width: 180px" @change="onFilterChange">
        <a-select-option v-for="m in filterOptions.models" :key="m" :value="m">{{ m }}</a-select-option>
      </a-select>
      <a-select v-model:value="filters.app_name" placeholder="全部应用" allow-clear style="width: 150px" @change="onFilterChange">
        <a-select-option v-for="a in filterOptions.apps" :key="a" :value="a">{{ a }}</a-select-option>
      </a-select>
      <a-select v-model:value="filters.status" placeholder="全部状态" allow-clear style="width: 120px" @change="onFilterChange">
        <a-select-option v-for="s in filterOptions.statuses" :key="s" :value="s">{{ s === 'ok' ? '成功' : s }}</a-select-option>
      </a-select>
      <a-button v-if="hasFilter()" size="small" @click="clearFilters">清除筛选</a-button>
      <span style="flex: 1"></span>
      <a-select v-model:value="limit" style="width: 120px" @change="load">
        <a-select-option :value="50">50 条</a-select-option>
        <a-select-option :value="100">100 条</a-select-option>
        <a-select-option :value="300">300 条</a-select-option>
        <a-select-option :value="500">500 条</a-select-option>
      </a-select>
      <a-button @click="load"><ReloadOutlined />刷新</a-button>
    </div>
    <a-card title="使用记录">
      <a-table :data-source="records" :loading="loading" row-key="id" :pagination="{ pageSize: 20 }" size="small" table-layout="fixed">
        <a-table-column title="时间" key="ts" :width="120">
          <template #default="{ record }">{{ fmtTime(record.ts) }}</template>
        </a-table-column>
        <a-table-column title="协议" data-index="protocol" key="protocol" :width="70" />
        <a-table-column title="模型" data-index="model" key="model" :width="150" ellipsis />
        <a-table-column title="应用" key="app" :width="100">
          <template #default="{ record }">
            <a-tag v-if="record.app_name" color="purple" style="max-width: 100%; overflow: hidden; text-overflow: ellipsis">{{ record.app_name }}</a-tag>
            <span v-else style="color: #5b6476">-</span>
          </template>
        </a-table-column>
        <a-table-column title="Tokens" key="tokens" :width="110" align="right">
          <template #default="{ record }">{{ record.input_tokens || 0 }}/{{ record.output_tokens || 0 }}</template>
        </a-table-column>
        <a-table-column title="积分" key="credits" :width="70" align="right">
          <template #default="{ record }">
            <span v-if="record.credits">{{ record.credits.toFixed(2) }}</span>
            <span v-else style="color: #5b6476">-</span>
          </template>
        </a-table-column>
        <a-table-column title="耗时" key="latency_ms" :width="80" align="right">
          <template #default="{ record }">
            <a-tooltip :title="`${Math.round(record.latency_ms || 0)} ms`"><span>{{ fmtLatency(record.latency_ms) }}</span></a-tooltip>
          </template>
        </a-table-column>
        <a-table-column title="状态" key="status" :width="70">
          <template #default="{ record }">
            <a-tag :color="record.status === 'ok' ? 'green' : 'red'">{{ record.status === 'ok' ? '成功' : record.status }}</a-tag>
          </template>
        </a-table-column>
        <a-table-column title="操作" key="action" :width="70">
          <template #default="{ record }">
            <a-button size="small" type="link" @click="openDetail(record)">详情</a-button>
          </template>
        </a-table-column>
      </a-table>
    </a-card>

    <!-- 详情弹窗 -->
    <a-modal
      v-model:open="detailVisible"
      :title="detail ? `请求详情 · ${detail.model} (${detail.protocol})` : '请求详情'"
      :footer="null"
      width="720px"
    >
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
    </a-modal>
  </div>
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
