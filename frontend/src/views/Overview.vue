<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '@/api/client'
import type { Overview, UsagePoint } from '@/types'
import dayjs from 'dayjs'
import { TeamOutlined, ThunderboltOutlined, CalendarOutlined, DollarOutlined, DatabaseOutlined, ArrowRightOutlined, RiseOutlined } from '@ant-design/icons-vue'
import * as echarts from 'echarts/core'
import { LineChart } from 'echarts/charts'
import { GridComponent, TooltipComponent, LegendComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

echarts.use([LineChart, GridComponent, TooltipComponent, LegendComponent, CanvasRenderer])

const router = useRouter()
const REFRESH_MS = 20000 // 每 20s 自动刷新
let timer: ReturnType<typeof setInterval> | null = null

const data = ref<Overview>({
  accounts: [],
  models: [],
  usage: {
    total_requests: 0, total_tokens: 0, today_requests: 0, today_tokens: 0,
    by_protocol: [], by_model: [],
  },
  recent: [],
})
const loading = ref(false)

const healthyCount = computed(() => data.value.accounts.filter((a) => a.healthy).length)

// 积分预警横幅（后端按阈值计算：余额不足/积分即将到期）
const alerts = computed(() => data.value.alerts ?? [])

// 最近记录限条数：概览页只作速览（表格自身也限高滚动），完整列表去「使用记录」页
const RECENT_LIMIT = 6
const recentLimited = computed(() => (data.value.recent ?? []).slice(0, RECENT_LIMIT))

// 统计卡：统一深色底 + 品牌色点缀（k3 建议降噪）
const accentColor = (color: string) => `linear-gradient(135deg, ${color}, ${color}cc)`
const statCards = computed(() => [
  {
    title: '账号健康', icon: TeamOutlined, accent: '#22c55e',
    value: `${healthyCount.value} / ${data.value.accounts.length}`,
  },
  { title: '总请求', icon: ThunderboltOutlined, accent: '#0ea5e9', value: fmt(data.value.usage.total_requests) },
  { title: '今日请求', icon: CalendarOutlined, accent: '#f59e0b', value: fmt(data.value.usage.today_requests) },
  { title: '今日 Tokens', icon: RiseOutlined, accent: '#ec4899', value: fmtK(data.value.usage.today_tokens) },
  { title: '总 Tokens', icon: DollarOutlined, accent: '#8b5cf6', value: fmtK(data.value.usage.total_tokens) },
  { title: '模型数', icon: DatabaseOutlined, accent: '#14b8a6', value: fmt(data.value.models.length) },
])

// 积分 → 预测 token
const prediction = computed(() => data.value.prediction)
function fmtPredict(n: number | null | undefined) {
  if (n == null || n === 0) return '-'
  if (n >= 1000000) return `${(n / 1000000).toFixed(1)}M`
  if (n >= 1000) return `${(n / 1000).toFixed(1)}K`
  return n.toLocaleString()
}

// 趋势折线图
const chartEl = ref<HTMLDivElement | null>(null)
let chart: echarts.ECharts | null = null
const tsData = ref<UsagePoint[]>([])

function fmt(n: number) {
  return n.toLocaleString()
}
function fmtK(v: number) {
  if (v >= 1000000) return `${(v / 1000000).toFixed(1)}M`
  if (v >= 1000) return `${(v / 1000).toFixed(1)}K`
  return String(v)
}
function fmtTime(ts?: number) {
  if (!ts) return '-'
  const d = dayjs(ts * 1000)
  const now = dayjs()
  if (d.isSame(now, 'day')) return `今天 ${d.format('HH:mm:ss')}`
  if (d.isSame(now.subtract(1, 'day'), 'day')) return `昨天 ${d.format('HH:mm:ss')}`
  return d.format('MM-DD HH:mm')
}

async function load() {
  loading.value = true
  // 兜底：即使请求挂起，也强制在超时后结束 loading，避免整页被遮罩覆盖成空白
  const safety = setTimeout(() => { loading.value = false }, 8000)
  try {
    data.value = await api.overview()
  } catch { /* 拦截器已提示 */ } finally {
    clearTimeout(safety)
    loading.value = false
  }
}

async function loadTs() {
  const res = await api.usageTimeseries('hour', 24)
  tsData.value = res.data
  renderChart()
}

function renderChart() {
  if (!chartEl.value) return
  if (!chart) chart = echarts.init(chartEl.value)
  chart.setOption({
    tooltip: { trigger: 'axis', backgroundColor: '#1a2140', borderColor: 'rgba(99,179,237,0.3)', textStyle: { color: '#e6edf7' } },
    grid: { left: 50, right: 20, top: 24, bottom: 28 },
    xAxis: {
      type: 'category', data: tsData.value.map((d) => d.bucket),
      axisLine: { lineStyle: { color: 'rgba(255,255,255,0.15)' } },
      axisLabel: { color: '#8a94a6', interval: 2, rotate: 45 },
    },
    yAxis: {
      type: 'value',
      splitLine: { lineStyle: { color: 'rgba(255,255,255,0.06)' } },
      axisLabel: { color: '#8a94a6' },
    },
    series: [{
      name: '调用次数', type: 'line', data: tsData.value.map((d) => d.count), smooth: true,
      symbol: 'circle', symbolSize: 5,
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
  // 概览数据与趋势图并行加载（串行会叠加两个接口的耗时）
  await Promise.allSettled([load(), loadTs()])
  // 趋势图也要随轮询刷新：只刷 load() 会让折线图永远停在打开时刻，
  // 用户误以为"没有请求"；Promise.allSettled 保证一个失败不影响另一个
  timer = setInterval(() => { Promise.allSettled([load(), loadTs()]) }, REFRESH_MS)
  window.addEventListener('resize', onResize)
})

onUnmounted(() => {
  if (timer) clearInterval(timer)
  window.removeEventListener('resize', onResize)
  chart?.dispose()
  chart = null
})
</script>

<template>
  <div>
    <a-spin :spinning="loading" tip="加载中…">
      <!-- 积分预警横幅（余额不足/即将到期） -->
      <a-alert
        v-for="(a, i) in alerts"
        :key="i"
        :type="a.level === 'warning' ? 'warning' : 'info'"
        show-icon
        style="margin-bottom: 12px"
        :message="a.message"
      />
      <!-- 积分 → 预测 token 横幅 -->
      <div class="predict-banner">
        <div class="predict-icon"><DollarOutlined /></div>
        <div class="predict-main">
          <div class="predict-title">积分额度预测</div>
          <div class="predict-num" v-if="prediction?.predicted_tokens">
            当前剩余约 <span class="highlight">{{ prediction.remaining_credits }}</span> 积分，
            预计可兑换约 <span class="highlight token">{{ fmtPredict(prediction.predicted_tokens) }}</span> Token
          </div>
          <div class="predict-num" v-else>
            当前剩余约 <span class="highlight">{{ prediction?.remaining_credits ?? '-' }}</span> 积分，
            历史消耗数据不足，暂无法预测可兑换 Token
          </div>
        </div>
        <div class="predict-note">
          <template v-if="prediction?.tokens_per_credit">按模型加权平均每 1 积分 ≈ {{ fmt(prediction.tokens_per_credit) }} Token</template>
          <template v-else>需积累更多带积分消耗的记录</template>
        </div>
      </div>

      <!-- 各模型消耗预测（不同模型消耗速度不同；最多展示 5 个，避免主页超一屏） -->
      <div v-if="prediction?.models?.length" class="panel" style="margin-top: 12px">
        <div class="panel-title">各模型消耗预测</div>
        <div style="margin-top: 12px; display: flex; flex-direction: column; gap: 10px">
          <div v-for="m in prediction.models.slice(0, 5)" :key="m.model" class="model-predict">
            <div class="model-predict-head">
              <span class="model-name">{{ m.model }}</span>
              <span class="model-meta">权重 {{ (m.weight * 100).toFixed(0) }}% · 每 1 积分 ≈ {{ fmt(m.tokens_per_credit) }} Token</span>
              <span class="model-val">{{ fmtPredict(m.predicted_tokens) }} Token</span>
            </div>
            <a-progress
              :percent="Math.round(m.weight * 100)"
              :show-info="false"
              :stroke-color="{ '0%': '#6366f1', '100%': '#8b5cf6' }"
              trail-color="rgba(255,255,255,0.08)"
              style="margin: 0"
            />
          </div>
        </div>
      </div>

      <div class="stat-grid">
        <div v-for="card in statCards" :key="card.title" class="stat-card">
          <div class="stat-icon" :style="{ background: accentColor(card.accent) }">
            <component :is="card.icon" />
          </div>
          <div class="stat-info">
            <div class="stat-title">{{ card.title }}</div>
            <div class="stat-value">{{ card.value }}</div>
          </div>
        </div>
      </div>

      <!-- 趋势折线图（k3 建议概览页补趋势） -->
      <div class="panel" style="margin-top: 16px">
        <div class="panel-title">请求趋势（近 24 小时）</div>
        <div ref="chartEl" style="width: 100%; height: 180px"></div>
      </div>

      <div class="panel" style="margin-top: 16px">
        <div class="panel-head">
          <div class="panel-title" style="margin-bottom: 0">最近使用记录</div>
          <a class="panel-more" @click="router.push('/records')">
            查看全部 <ArrowRightOutlined />
          </a>
        </div>
        <a-table
          :data-source="recentLimited"
          :pagination="false"
          size="small"
          row-key="id"
          :locale="{ emptyText: '暂无记录' }"
          :scroll="{ y: 260 }"
        >
          <a-table-column title="时间" data-index="ts" key="ts" :width="130">
            <template #default="{ record }">{{ fmtTime(record.ts) }}</template>
          </a-table-column>
          <a-table-column title="协议" data-index="protocol" key="protocol" align="left" :width="90" />
          <a-table-column title="模型" data-index="model" key="model" align="left" />
          <a-table-column title="Tokens" data-index="total_tokens" key="total_tokens" align="right" :width="100">
            <template #default="{ record }">{{ fmt(record.total_tokens) }}</template>
          </a-table-column>
          <a-table-column title="积分" data-index="credits" key="credits" align="right" :width="70">
            <template #default="{ record }">
              <span v-if="record.credits" style="color: #fbbf24">{{ record.credits.toFixed(2) }}</span>
              <span v-else style="color: #555">-</span>
            </template>
          </a-table-column>
          <a-table-column title="状态" data-index="status" key="status" align="left" :width="80">
            <template #default="{ record }">
              <a-tag :color="record.status === 'ok' ? 'green' : 'red'">{{ record.status }}</a-tag>
            </template>
          </a-table-column>
        </a-table>
      </div>
    </a-spin>
  </div>
</template>

<style scoped>
/* 统计卡：6 等分网格单行铺满（3×2 断点降级），右侧不留空 */
.stat-grid {
  display: grid;
  grid-template-columns: repeat(6, 1fr);
  gap: 16px;
  margin-top: 16px;
}
@media (max-width: 1400px) { .stat-grid { grid-template-columns: repeat(3, 1fr); } }
@media (max-width: 720px)  { .stat-grid { grid-template-columns: repeat(2, 1fr); } }

.stat-card {
  position: relative;
  display: flex; align-items: center; gap: 12px;
  border-radius: 14px; padding: 14px 16px; color: #fff;
  background: linear-gradient(150deg, #1b2038 0%, #232a4a 100%);
  border: 1px solid rgba(99, 179, 237, 0.15);
  box-shadow: 0 6px 20px rgba(0,0,0,0.18);
  transition: transform 0.18s ease, box-shadow 0.18s ease, border-color 0.18s ease;
  min-height: 68px;
}
.stat-card:hover {
  transform: translateY(-3px);
  border-color: rgba(99,179,237,0.4);
  box-shadow: 0 12px 28px rgba(0,0,0,0.28);
}
.stat-icon {
  width: 44px; height: 44px; border-radius: 12px;
  color: #fff;
  display: flex; align-items: center; justify-content: center;
  font-size: 20px;
}
.stat-info { display: flex; flex-direction: column; }
.stat-title { font-size: 12px; color: #8a94a6; }
.stat-value { font-size: 22px; font-weight: 700; line-height: 1.2; color: #e6edf7; }

.panel {
  background: linear-gradient(150deg, #1b2038 0%, #232a4a 100%);
  border: 1px solid rgba(99, 179, 237, 0.18);
  border-radius: 14px; padding: 16px;
}
.panel-head {
  display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px;
}
.panel-title { font-size: 16px; font-weight: 700; color: #e6edf7; }

/* 积分预测横幅 */
.predict-banner {
  display: flex; align-items: center; gap: 16px;
  padding: 14px 22px;
  border-radius: 14px;
  background:
    radial-gradient(600px 120px at 0% 0%, rgba(99,102,241,0.18), transparent 60%),
    linear-gradient(135deg, #1b2038 0%, #232a4a 100%);
  border: 1px solid rgba(139,92,246,0.35);
  box-shadow: 0 6px 20px rgba(0,0,0,0.18);
}
.predict-icon {
  width: 42px; height: 42px; border-radius: 12px; flex-shrink: 0;
  background: linear-gradient(135deg, #6366f1, #8b5cf6);
  color: #fff; display: flex; align-items: center; justify-content: center; font-size: 20px;
  box-shadow: 0 4px 14px rgba(99,102,241,0.45);
}
.predict-main { flex: 1; display: flex; flex-direction: column; }
.predict-title { font-size: 12px; color: #8a94a6; letter-spacing: 1px; }
.predict-num { font-size: 16px; font-weight: 700; color: #e6edf7; margin-top: 2px; }
.predict-num .highlight { color: #c4b5fd; white-space: nowrap; }
.predict-num .highlight.token { color: #7cc0f5; font-size: 20px; white-space: nowrap; }
.predict-note { font-size: 12px; color: #8a94a6; text-align: right; flex-shrink: 0; white-space: nowrap; max-width: 300px; }

/* 各模型消耗预测 */
.model-predict { display: flex; flex-direction: column; gap: 6px; }
.model-predict-head { display: flex; align-items: center; gap: 12px; font-size: 12.5px; }
.model-name { font-weight: 700; color: #e6edf7; min-width: 120px; }
.model-meta { color: #8a94a6; flex: 1; }
.model-val { color: #7cc0f5; font-weight: 700; }
.panel-more { color: #63b3ed; font-size: 13px; cursor: pointer; }
.panel-more:hover { color: #7cc0f5; }
</style>
