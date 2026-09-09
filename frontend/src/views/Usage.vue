<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { api } from '@/api/client'
import type { UsageSummary, UsagePoint } from '@/types'
import { ThunderboltOutlined, FileTextOutlined, DatabaseOutlined, ApartmentOutlined } from '@ant-design/icons-vue'
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

onMounted(async () => {
  await loadSummary()
  await loadTs()
  window.addEventListener('resize', onResize)
})

onUnmounted(() => {
  window.removeEventListener('resize', onResize)
  chart?.dispose()
  chart = null
})
</script>

<template>
  <div style="padding-bottom: 36px">
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
      <div ref="chartEl" style="width: 100%; height: 320px"></div>
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
  </div>
</template>

<style scoped>
/* KPI 卡 */
.kpi-card {
  display: flex; align-items: center; gap: 12px;
  border-radius: 14px; padding: 16px;
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
  padding: 8px 0;
}
.bar-label { width: 130px; font-size: 13px; color: #cdd6e8; flex-shrink: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.bar-label.mono { font-family: 'Consolas', 'Menlo', monospace; }
.bar-track { flex: 1; height: 12px; border-radius: 6px; background: rgba(255,255,255,0.06); overflow: hidden; }
.bar-fill {
  height: 100%; border-radius: 6px;
  background: linear-gradient(90deg, #6366f1, #8b5cf6);
  transition: width 0.5s ease;
}
.bar-val { width: 180px; font-size: 12px; color: #8a94a6; text-align: right; flex-shrink: 0; }
.bar-pct { color: #a5b8d8; font-weight: 600; margin-left: 6px; }
.bar-fill.dim { background: #3a4266; }
</style>
