<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { message } from 'ant-design-vue'
import { ReloadOutlined, FilterOutlined, DownloadOutlined } from '@ant-design/icons-vue'
import { api } from '@/api/client'
import type { UsageRecord } from '@/types'
import dayjs from 'dayjs'
import RecordDetailModal from '@/components/RecordDetailModal.vue'

const records = ref<UsageRecord[]>([])
const loading = ref(false)

// 服务端分页：总数由后端返回，翻页时向后端取对应页
// （旧版是本地分页：只翻一次性拉回来的那批，拉 100 条就只有 5 页）
const pagination = reactive({
  current: 1,
  pageSize: 10,
  total: 0,
  showSizeChanger: true,
  pageSizeOptions: ['10', '20', '50'],
  showTotal: (t: number) => `共 ${t} 条`,
})

// 筛选项（协议 / 模型 / 应用 / 状态 / 内容关键字）
const filters = ref<{ protocol?: string; model?: string; app_name?: string; status?: string; search?: string }>({})
const searchText = ref('')
const filterOptions = ref<{
  protocols: string[]
  models: string[]
  apps: string[]
  apps_history: string[]
  has_unnamed?: boolean
  statuses: string[]
}>({ protocols: [], models: [], apps: [], apps_history: [], statuses: [] })

const detailVisible = ref(false)
const lightRecord = ref<UsageRecord | null>(null)
const exporting = ref(false)

async function load() {
  loading.value = true
  try {
    const res = await api.usageRecent(pagination.current, pagination.pageSize, filters.value)
    records.value = res.records
    pagination.total = res.total
    // 当前页超出总页数（如筛选后记录变少）时回退到最后一页
    const lastPage = Math.max(1, Math.ceil(res.total / pagination.pageSize))
    if (pagination.current > lastPage) {
      pagination.current = lastPage
      return load()
    }
  } finally {
    loading.value = false
  }
}

function onTableChange(p: { current?: number; pageSize?: number }) {
  pagination.current = p.current || 1
  pagination.pageSize = p.pageSize || 20
  load()
}

async function loadFilters() {
  filterOptions.value = await api.usageFilters()
}

function onFilterChange() {
  // 筛选变化后回到第 1 页
  pagination.current = 1
  load()
}

function onSearch() {
  // 内容关键字：去空格后为空视同清除
  filters.value.search = searchText.value.trim() || undefined
  pagination.current = 1
  load()
}

function clearFilters() {
  filters.value = {}
  searchText.value = ''
  pagination.current = 1
  load()
}

// 是否有生效中的筛选（app_name 允许空字符串=未记录应用，故按 != null 判断）
const hasFilter = () => Object.values(filters.value).some((v) => v != null)

function openDetail(r: UsageRecord) {
  // 列表是 light 投影（不含大文本 content）：完整内容由详情弹窗按需拉取
  lightRecord.value = r
  detailVisible.value = true
}

function fmtTime(ts: number) {
  return dayjs(ts * 1000).format('YYYY-MM-DD HH:mm:ss')
}

function fmtLatency(ms: number | null | undefined) {
  const v = ms ?? 0
  if (v >= 60000) return `${(v / 60000).toFixed(1)}m`
  return `${(v / 1000).toFixed(1)}s`
}

// 导出当前筛选下的全部记录为 CSV（含 BOM 防 Excel 中文乱码）
// 服务端分页后列表里只有当前页，这里循环拉取所有页（light 投影，只含导出所需的元数据列）
async function exportCsv() {
  if (!pagination.total) {
    message.warning('没有可导出的记录')
    return
  }
  exporting.value = true
  try {
    const all: UsageRecord[] = []
    const pageSize = 200
    const maxPages = 50 // 封顶 1 万条，防止误操作拖垮后端
    for (let p = 1; p <= maxPages && all.length < pagination.total; p++) {
      const res = await api.usageRecent(p, pageSize, filters.value)
      all.push(...res.records)
      if (res.records.length < pageSize) break
    }
    if (!all.length) {
      message.warning('没有可导出的记录')
      return
    }
    writeCsv(all)
    message.success(`已导出 ${all.length} 条记录${hasFilter() ? '（按当前筛选条件）' : '（全部记录）'}`)
  } catch { /* 拦截器已提示 */ } finally {
    exporting.value = false
  }
}

function writeCsv(rowsAll: UsageRecord[]) {
  const headers = ['时间', '协议', '模型', '应用', '输入Tokens', '输出Tokens', '积分', '耗时ms', '状态', '错误', '账号']
  const rows = rowsAll.map((r) => [
    fmtTime(r.ts),
    r.protocol || '',
    r.model || '',
    r.app_name || '',
    r.input_tokens || 0,
    r.output_tokens || 0,
    r.credits ?? '',
    Math.round(r.latency_ms || 0),
    r.status || '',
    (r.error || '').replace(/[\r\n]+/g, ' '),
    r.account_uid || '',
  ])
  const esc = (v: unknown) => `"${String(v ?? '').replace(/"/g, '""')}"`
  const csv = [headers, ...rows].map((row) => row.map(esc).join(',')).join('\r\n')
  const blob = new Blob(['\uFEFF' + csv], { type: 'text/csv;charset=utf-8' })
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = `usage_${dayjs().format('YYYY-MM-DD_HH-mm')}.csv`
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(a.href)
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
        <!-- 只列现存应用（与应用页一致）；已删除应用的历史记录名单独分组，避免对不上 -->
        <a-select-opt-group v-if="filterOptions.apps.length" label="应用">
          <a-select-option v-for="a in filterOptions.apps" :key="a" :value="a">{{ a }}</a-select-option>
        </a-select-opt-group>
        <a-select-opt-group v-if="filterOptions.apps_history.length" label="历史应用（已删除）">
          <a-select-option v-for="a in filterOptions.apps_history" :key="a" :value="a">{{ a }}</a-select-option>
        </a-select-opt-group>
        <a-select-option v-if="filterOptions.has_unnamed" value="">（未记录应用）</a-select-option>
      </a-select>
      <a-select v-model:value="filters.status" placeholder="全部状态" allow-clear style="width: 120px" @change="onFilterChange">
        <a-select-option v-for="s in filterOptions.statuses" :key="s" :value="s">{{ s === 'ok' ? '成功' : s }}</a-select-option>
      </a-select>
      <a-input-search
        v-model:value="searchText"
        placeholder="搜索内容（输入/输出/思考链）"
        allow-clear
        style="width: 220px"
        @search="onSearch"
      />
      <a-button v-if="hasFilter()" size="small" @click="clearFilters">清除筛选</a-button>
      <span style="flex: 1"></span>
      <a-button @click="load"><ReloadOutlined />刷新</a-button>
      <a-button @click="exportCsv" :loading="exporting" :disabled="!pagination.total"><DownloadOutlined />导出 CSV</a-button>
    </div>
    <a-card title="使用记录">
      <a-table
        :data-source="records"
        :loading="loading"
        row-key="id"
        :pagination="pagination"
        @change="onTableChange"
        :scroll="{ x: 840 }"
        size="small"
        table-layout="fixed"
      >
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

    <!-- 详情弹窗（内容按需拉取，独立组件） -->
    <RecordDetailModal v-model:open="detailVisible" :record="lightRecord" />
  </div>
</template>
