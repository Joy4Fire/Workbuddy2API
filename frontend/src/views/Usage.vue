<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import dayjs from 'dayjs'
import { api } from '@/api/client'
import type { UsageSummary, UsagePoint, CostRow } from '@/types'
import { ThunderboltOutlined, FileTextOutlined, DatabaseOutlined, ApartmentOutlined, ReloadOutlined } from '@ant-design/icons-vue'
import * as echarts from 'echarts/core'
import { LineChart } from 'echarts/charts'
import { GridComponent, TooltipComponent, LegendComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

echarts.use([LineChart, GridComponent, TooltipComponent, LegendComponent, CanvasRenderer])

const summary = ref<UsageSummary>({
  total_requests: 0, total_tokens: 0, today_requests: 0, today_tokens: 0,
  by_protocol: [], by_model: [],
})

const chartEl = ref<HTMLDivElement | null>(null)
let chart: echarts.ECharts | null = null
const granularity = ref<'hour' | 'day'>('hour')
const series = ref<'count' | 'tokens'>('count')
const tsData = ref<UsagePoint[]>([])

// KPI 概览（k3 建议：先结论后趋势）
const kpis = computed(() => [
  { title: '总调用', value: fmt(summary.value.total_requests), icon: ThunderboltOutlined, accent: '#0ea5e9' },
  { title: '总 Tokens', value: fmtK(summary.value.total_tokens), icon: FileTextOutlined, accent: '#8b5cf6' },
  { title: '涉及模型', value: fmt(summary.value.by_model.length), icon: DatabaseOutlined, accent: '#14b8a6' },
  { title: '协议数', value: fmt(summary.value.by_protocol.length), icon: ApartmentOutlined, accent: '#f59e0b' },
])

// 协议/模型统计占比
const protoTotal = computed(() => summary.value.by_protocol.reduce((s, p) => s + p.count, 0))
const modelTotal = computed(() => summary.value.by_model.reduce((s, m) => s + m.count, 0))
const protoMax = computed(() => Math.max(1, ...summary.value.by_protocol.map((p) => p.count)))
const modelMax = computed(() => Math.max(1, ...summary.value.by_model.map((m) => m.count)))
const apps = computed(() => summary.value.by_app ?? [])
const appTotal = computed(() => apps.value.reduce((s, a) => s + a.count, 0))
const appMax = computed(() => Math.max(1, ...apps.value.map((a) => a.count)))

// 模型列表渲染策略：Top N + 「其他」聚合（可展开），避免几十行把卡片拉得过长
const MODEL_TOP_N = 10
const showAllModels = ref(false)
const sortedModels = computed(() => [...summary.value.by_model].sort((a, b) => b.count - a.count))
interface ModelRow { model: string; count: number; tokens: number; dim?: boolean }
const modelRows = computed<ModelRow[]>(() => {
  const all = sortedModels.value
  if (showAllModels.value || all.length <= MODEL_TOP_N) return all
  const top: ModelRow[] = all.slice(0, MODEL_TOP_N)
  const rest = all.slice(MODEL_TOP_N)
  const agg = rest.reduce((s, m) => ({ count: s.count + m.count, tokens: s.tokens + m.tokens }), { count: 0, tokens: 0 })
  return [...top, { model: `其他 ${rest.length} 个模型`, count: agg.count, tokens: agg.tokens, dim: true }]
})

function fmt(n: number) {
  return (n || 0).toLocaleString()
}
function fmtK(v: number) {
  if (v >= 1000000) return `${(v / 1000000).toFixed(1)}M`
  if (v >= 1000) return `${(v / 1000).toFixed(1)}K`
  return String(v)
}

async function loadSummary() {
  summary.value = await api.usageSummary()
}

async function loadTs() {
  const res = await api.usageTimeseries(granularity.value, granularity.value === 'hour' ? 24 : 14)
  tsData.value = res.data
  renderChart()
}

function renderChart() {
  if (!chartEl.value) return
  if (!chart) chart = echarts.init(chartEl.value)
  const labels = tsData.value.map((d) => d.bucket)
  const values = tsData.value.map((d) => (series.value === 'count' ? d.count : d.tokens))
  const name = series.value === 'count' ? '调用次数' : 'Tokens'
  chart.setOption({
    tooltip: {
      trigger: 'axis',
      backgroundColor: '#1a2140', borderColor: 'rgba(99,179,237,0.3)',
      textStyle: { color: '#e6edf7' },
      valueFormatter: (v: number) => (series.value === 'tokens' ? fmt(v) : String(v)),
    },
    legend: { textStyle: { color: '#a5b8d8' }, top: 0, right: 0 },
    grid: { left: 50, right: 20, top: 36, bottom: 28 },
    xAxis: {
      type: 'category', data: labels,
      axisLine: { lineStyle: { color: 'rgba(255,255,255,0.15)' } },
      // k3 建议：抽稀刻度，避免全部旋转
      axisLabel: { color: '#8a94a6', interval: granularity.value === 'hour' ? 3 : 1, rotate: 30 },
    },
    yAxis: {
      type: 'value',
      splitLine: { lineStyle: { color: 'rgba(255,255,255,0.06)' } },
      axisLabel: { color: '#8a94a6', formatter: (v: number) => (series.value === 'tokens' ? fmtK(v) : String(v)) },
    },
    series: [{
      name, type: 'line', data: values, smooth: true, symbol: 'circle', symbolSize: 5,
      lineStyle: { width: 2.5, color: '#63b3ed', shadowColor: 'rgba(99,179,237,0.4)', shadowBlur: 8 },
      itemStyle: { color: '#63b3ed' },
      areaStyle: { color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
        { offset: 0, color: 'rgba(99,179,237,0.35)' },
        { offset: 1, color: 'rgba(99,179,237,0.02)' },
      ]) },
    }],
  })
}

function onResize() { chart?.resize() }

// ---- 实测积分单价（(账号, 模型) 维度的真实消耗台账）----
//
// 与模型页那个「成本」是两个不同的东西：模型页的 `credits` 是**上游标称的成本系数
// （倍率）**，这里是**我们自己按真实请求算出来的** `积分 / token × 1000`。
// 所以"这个模型到底烧多少额度"只能看这张表。
//
// 台账是 (账号, 模型) 维度 → 账号天然归属区域，所以它同时也是国内版/国际版各自的实测值。
// 只含最近一个窗口内的观测：过期价格比没有价格更误导（上游调价后旧值会一直骗人）。
type CostRowView = CostRow & { rowKey: string }
const costs = ref<CostRowView[]>([])
const costLoading = ref(false)
const costTtl = ref(0)
const costRegion = ref<'' | 'cn' | 'global'>('')
const costSearch = ref('')

const costRows = computed(() => {
  let list = costs.value
  if (costRegion.value) list = list.filter((r) => r.region === costRegion.value)
  const q = costSearch.value.trim().toLowerCase()
  if (q) list = list.filter((r) => r.model.toLowerCase().includes(q) || r.uid.toLowerCase().includes(q))
  return list
})

// 两个区域各有几行——区域筛选按钮上直接标数量，省得用户点进去发现是空的
const costRegionCount = computed(() => ({
  cn: costs.value.filter((r) => r.region === 'cn').length,
  global: costs.value.filter((r) => r.region === 'global').length,
}))

async function loadCosts() {
  costLoading.value = true
  try {
    const res = await api.usageCosts()
    // 表格 row-key 要唯一：台账本身就是 (账号, 模型) 维度，直接拿它当 key
    costs.value = (res.costs || []).map((r) => ({ ...r, rowKey: `${r.uid}|${r.model}` }))
    costTtl.value = res.ttl_seconds || 0
  } catch { /* 拦截器已提示 */ } finally {
    costLoading.value = false
  }
}

function costTtlText(): string {
  if (!costTtl.value) return ''
  const h = costTtl.value / 3600
  return h >= 1 ? `${Math.round(h)} 小时` : `${Math.round(costTtl.value / 60)} 分钟`
}

/** 单价文案：tier 0 是"观测到消耗为 0"（免费额度包），显示成 0 会被误读成"没数据" */
function costText(r: CostRow): string {
  if (r.tier === 0) return '免费'
  return r.cost_per_1k.toFixed(4)
}

function fmtDateTime(ts: number): string {
  if (!ts) return '-'
  return dayjs(ts * 1000).format('MM-DD HH:mm:ss')
}

function shortUid(uid: string): string {
  return uid && uid.length > 10 ? uid.slice(0, 8) + '…' : (uid || '-')
}

onMounted(async () => {
  await loadSummary()
  await loadTs()
  await loadCosts()
  window.addEventListener('resize', onResize)
})

onUnmounted(() => {
  window.removeEventListener('resize', onResize)
  chart?.dispose()
  chart = null
})
</script>

<template>
  <div style="padding-bottom: 16px">
    <!-- KPI 概览卡 -->
    <a-row :gutter="[16, 16]" style="margin-bottom: 16px">
      <a-col v-for="k in kpis" :key="k.title" :xs="12" :sm="6">
        <div class="kpi-card">
          <div class="kpi-icon" :style="{ background: `linear-gradient(135deg, ${k.accent}, ${k.accent}cc)` }">
            <component :is="k.icon" />
          </div>
          <div class="kpi-info">
            <div class="kpi-title">{{ k.title }}</div>
            <div class="kpi-value">{{ k.value }}</div>
          </div>
        </div>
      </a-col>
    </a-row>

    <!-- 折线图卡片 -->
    <a-card title="调用趋势" style="margin-bottom: 16px">
      <div style="display: flex; gap: 12px; align-items: center; margin-bottom: 12px; flex-wrap: wrap">
        <a-radio-group v-model:value="granularity" @change="loadTs">
          <a-radio-button value="hour">按小时</a-radio-button>
          <a-radio-button value="day">按天</a-radio-button>
        </a-radio-group>
        <a-radio-group v-model:value="series" @change="renderChart">
          <a-radio-button value="count">调用次数</a-radio-button>
          <a-radio-button value="tokens">Tokens</a-radio-button>
        </a-radio-group>
      </div>
      <div ref="chartEl" style="width: 100%; height: 200px"></div>
    </a-card>

    <!-- 协议 + 应用 / 模型统计：两列高度均衡（模型多时 Top N + 其他聚合，可展开全部） -->
    <a-row :gutter="[16, 16]">
      <a-col :xs="24" :lg="12">
        <a-card title="按协议统计" style="margin-bottom: 16px">
          <div v-if="!summary.by_protocol.length" style="color: #8a94a6; padding: 12px">暂无数据</div>
          <div v-for="p in summary.by_protocol" :key="p.protocol" class="bar-row">
            <div class="bar-label">{{ p.protocol }}</div>
            <div class="bar-track">
              <div class="bar-fill" :style="{ width: (p.count / protoMax * 100).toFixed(1) + '%' }"></div>
            </div>
            <div class="bar-val">
              {{ p.count }} 次 · {{ fmtK(p.tokens) }} tok
              <span class="bar-pct">{{ protoTotal ? Math.round(p.count / protoTotal * 100) : 0 }}%</span>
            </div>
          </div>
        </a-card>
        <a-card v-if="apps.length" title="按应用统计">
          <div v-for="a in apps" :key="a.app" class="bar-row">
            <div class="bar-label">{{ a.app }}</div>
            <div class="bar-track">
              <div class="bar-fill" :style="{ width: (a.count / appMax * 100).toFixed(1) + '%' }"></div>
            </div>
            <div class="bar-val">
              {{ a.count }} 次 · {{ fmtK(a.tokens) }} tok
              <span class="bar-pct">{{ appTotal ? Math.round(a.count / appTotal * 100) : 0 }}%</span>
            </div>
          </div>
        </a-card>
      </a-col>
      <a-col :xs="24" :lg="12">
        <a-card title="按模型统计">
          <div v-if="!summary.by_model.length" style="color: #8a94a6; padding: 12px">暂无数据</div>
          <div v-for="m in modelRows" :key="m.model" class="bar-row">
            <div class="bar-label mono">{{ m.model }}</div>
            <div class="bar-track">
              <div class="bar-fill" :class="{ dim: m.dim }" :style="{ width: (m.count / modelMax * 100).toFixed(1) + '%' }"></div>
            </div>
            <div class="bar-val">
              {{ m.count }} 次 · {{ fmtK(m.tokens) }} tok
              <span class="bar-pct">{{ modelTotal ? Math.round(m.count / modelTotal * 100) : 0 }}%</span>
            </div>
          </div>
          <div v-if="sortedModels.length > MODEL_TOP_N" style="text-align: center; margin-top: 8px">
            <a-button size="small" type="text" @click="showAllModels = !showAllModels">
              {{ showAllModels ? '收起' : `展开全部 ${sortedModels.length} 个模型` }}
            </a-button>
          </div>
        </a-card>
      </a-col>
    </a-row>

    <!-- 实测积分单价：(账号, 模型) 维度的**真实消耗**。
         与模型页那个「成本」不是一回事——那里是上游标称的倍率，这里是我们自己按
         真实请求算出来的 `积分 / token × 1000`。账号天然归属区域，所以这张表同时
         给出了国内版 / 国际版账号各自的实测值。 -->
    <a-card title="实测积分单价" style="margin-top: 16px">
      <template #extra>
        <a-tooltip title="只显示窗口内测得的观测。过期的价格比没有价格更误导——上游调价后旧值会一直骗人，所以宁可让这一行消失。">
          <span style="font-size: 12px; color: #8a94a6">观测窗口 {{ costTtlText() || '-' }}</span>
        </a-tooltip>
      </template>
      <div style="display: flex; gap: 12px; align-items: center; margin-bottom: 12px; flex-wrap: wrap">
        <a-radio-group v-model:value="costRegion">
          <a-radio-button value="">全部 {{ costs.length }}</a-radio-button>
          <a-radio-button value="cn">国内版 {{ costRegionCount.cn }}</a-radio-button>
          <a-radio-button value="global">国际版 {{ costRegionCount.global }}</a-radio-button>
        </a-radio-group>
        <a-input v-model:value="costSearch" placeholder="按模型 / 账号筛选" allow-clear style="width: 200px" />
        <a-button size="small" :loading="costLoading" @click="loadCosts">
          <template #icon><ReloadOutlined /></template>刷新
        </a-button>
        <span style="font-size: 12px; color: #8a94a6">
          每 1k token 消耗的积分，按真实请求测得（EMA 平滑）；只有成功请求且上游返回了 token 数才会记一笔。
        </span>
      </div>
      <a-table
        :data-source="costRows"
        :loading="costLoading"
        :pagination="{ pageSize: 10, size: 'small', showSizeChanger: false }"
        row-key="rowKey"
        size="small"
      >
        <a-table-column title="账号" key="uid" :width="170">
          <template #default="{ record }">
            <a-tooltip :title="record.uid"><code>{{ shortUid(record.uid) }}</code></a-tooltip>
          </template>
        </a-table-column>
        <a-table-column title="区域" key="region" :width="90">
          <template #default="{ record }">
            <a-tag :color="record.region === 'global' ? 'purple' : 'blue'">
              {{ record.region_label || (record.region === 'global' ? '国际版' : '国内版') }}
            </a-tag>
          </template>
        </a-table-column>
        <a-table-column title="模型" key="model" :ellipsis="true">
          <template #default="{ record }"><code>{{ record.model }}</code></template>
        </a-table-column>
        <a-table-column title="实测单价（积分 / 1k tok）" key="cost" :width="200">
          <template #default="{ record }">
            <a-tooltip :title="record.tier === 0
              ? '观测到的积分消耗为 0（走的是免费额度包）——这是有效观测，不是「没有数据」。'
              : '按真实请求算出的积分消耗（EMA 平滑），样本数见右列。'">
              <span :class="{ 'cost-free': record.tier === 0 }">{{ costText(record) }}</span>
            </a-tooltip>
          </template>
        </a-table-column>
        <a-table-column title="样本" data-index="samples" key="samples" :width="70" />
        <a-table-column title="最近观测" key="updated" :width="130">
          <template #default="{ record }">{{ fmtDateTime(record.updated_at) }}</template>
        </a-table-column>
      </a-table>
    </a-card>
  </div>
</template>

<style scoped>
/* KPI 卡 */
.kpi-card {
  display: flex; align-items: center; gap: 12px;
  border-radius: 14px; padding: 12px 16px;
  background: linear-gradient(150deg, #1b2038 0%, #232a4a 100%);
  border: 1px solid rgba(99,179,237,0.15);
}
.kpi-icon {
  width: 42px; height: 42px; border-radius: 12px;
  color: #fff; display: flex; align-items: center; justify-content: center; font-size: 19px;
}
.kpi-info { display: flex; flex-direction: column; min-width: 0; }
.kpi-title { font-size: 12px; color: #8a94a6; white-space: nowrap; }
.kpi-value { font-size: 20px; font-weight: 700; color: #e6edf7; line-height: 1.2; white-space: nowrap; font-variant-numeric: tabular-nums; }

/* 进度条（统一色系） */
.bar-row {
  display: flex; align-items: center; gap: 12px;
  padding: 5px 0;
}
.bar-label { width: 130px; font-size: 13px; color: #cdd6e8; flex-shrink: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.bar-label.mono { font-family: 'Consolas', 'Menlo', monospace; }
.bar-track { flex: 1; height: 10px; border-radius: 5px; background: rgba(255,255,255,0.06); overflow: hidden; }
.bar-fill {
  height: 100%; border-radius: 5px;
  background: linear-gradient(90deg, #6366f1, #8b5cf6);
  transition: width 0.5s ease;
}
.bar-val { width: 180px; font-size: 12px; color: #8a94a6; text-align: right; flex-shrink: 0; }
.bar-pct { color: #a5b8d8; font-weight: 600; margin-left: 6px; }
.bar-fill.dim { background: #3a4266; }

/* 「免费」是有效观测（消耗为 0），别让它看起来像缺数据 */
.cost-free { color: #14b8a6; font-weight: 600; }
</style>
