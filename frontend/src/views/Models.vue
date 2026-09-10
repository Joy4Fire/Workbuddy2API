<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { message } from 'ant-design-vue'
import { SearchOutlined, ReloadOutlined, PictureOutlined, FileTextOutlined, ToolOutlined, ThunderboltOutlined } from '@ant-design/icons-vue'
import { api } from '@/api/client'
import type { ModelInfo } from '@/types'

const models = ref<ModelInfo[]>([])
const loading = ref(false)
const source = ref<'dynamic' | 'static'>('dynamic')
const search = ref('')
const refreshing = ref(false)
const aaRefreshing = ref(false)
const aaConfigured = ref(false)
const aaLoading = ref(false)

const filtered = computed(() => {
  const q = search.value.trim().toLowerCase()
  if (!q) return models.value
  return models.value.filter(
    (m) =>
      m.id.toLowerCase().includes(q) ||
      (m.name || '').toLowerCase().includes(q),
  )
})

const stats = computed(() => {
  const hasAA = models.value.filter((m) => m.benchmark).length
  const multimodal = models.value.filter((m) => m.modality === 'multimodal').length
  const tool = models.value.filter((m) => m.supportsToolCall).length
  return { hasAA, multimodal, tool }
})

async function load() {
  loading.value = true
  try {
    const res = await api.models()
    models.value = res.models
    source.value = res.source === 'static' ? 'static' : 'dynamic'
    loadAA()
  } finally {
    loading.value = false
  }
}

async function loadAA() {
  aaLoading.value = true
  try {
    const res = await api.benchmarks()
    aaConfigured.value = res.configured
    if (res.configured) {
      // 把 AA 数据合并进各模型
      for (const m of models.value) {
        if (res.models[m.id]) m.benchmark = res.models[m.id]
      }
    }
  } catch { /* 拦截器已提示 */ } finally {
    aaLoading.value = false
  }
}

function fmtTokens(v?: number) {
  if (!v || v <= 0) return '-'
  if (v >= 1000000) return `${(v / 1000000).toFixed(1)}M`
  return `${Math.round(v / 1000)}K`
}

function fmtName(m: ModelInfo) {
  return m.name && m.name !== m.id ? m.name : m.id
}

function fmtNum(v?: number, digits = 1) {
  if (v === undefined || v === null || Number.isNaN(v)) return '-'
  return Number(v).toFixed(digits)
}

function reasoningEfforts(m: ModelInfo): string[] {
  const r = m.reasoning
  if (!r?.supportedEfforts?.length) {
    if (r?.defaultEffort) return [r.defaultEffort]
    return []
  }
  const efforts = [...r.supportedEfforts]
  if (r.canDisableThinking && !efforts.includes('off')) efforts.push('off')
  return efforts
}

function reasoningTag(m: ModelInfo) {
  const r = m.reasoning
  if (!r || !r.supportsReasoning) return null
  if (r.canDisableThinking) return { text: '可关思考', color: 'green' }
  if (r.onlyReasoning) return { text: '仅思考', color: 'blue' }
  return { text: '思考', color: 'cyan' }
}

async function refresh() {
  refreshing.value = true
  try {
    await api.modelsRefresh()
    await load()
  } catch { /* 拦截器已提示 */ } finally {
    refreshing.value = false
  }
}

async function refreshAA() {
  aaRefreshing.value = true
  try {
    await api.benchmarksRefresh()
    message.success('AA 评测数据已刷新')
    await loadAA()
  } catch { /* 拦截器已提示 */ } finally {
    aaRefreshing.value = false
  }
}

onMounted(load)
</script>

<template>
  <div style="padding-bottom: 36px">
    <!-- 顶部工具栏 -->
    <div class="toolbar">
      <a-input
        v-model:value="search"
        placeholder="搜索模型 ID 或名称"
        allow-clear
        class="search"
      >
        <template #prefix><SearchOutlined style="color: #8b93a5" /></template>
      </a-input>
      <a-button :loading="refreshing" @click="refresh" class="action-btn"><ReloadOutlined />刷新模型</a-button>
      <a-button
        v-if="aaConfigured"
        :loading="aaRefreshing"
        @click="refreshAA"
        class="action-btn"
      ><ReloadOutlined />刷新评测</a-button>
      <a-tag class="src-tag" :class="source === 'dynamic' ? 'src-dyn' : 'src-static'">
        {{ source === 'dynamic' ? '动态（来自上游）' : '静态兜底' }}
      </a-tag>
      <span class="count">共 {{ filtered.length }} 个模型</span>
    </div>

    <!-- 概览指标条 -->
    <div class="metric-strip">
      <div class="metric-chip"><span class="m-num">{{ models.length }}</span><span class="m-label">模型</span></div>
      <div class="metric-chip"><span class="m-num">{{ stats.multimodal }}</span><span class="m-label">多模态</span></div>
      <div class="metric-chip"><span class="m-num">{{ stats.tool }}</span><span class="m-label">支持工具</span></div>
      <div class="metric-chip">
        <span class="m-num">{{ stats.hasAA }}</span><span class="m-label">已评测模型</span>
      </div>
    </div>

    <a-spin :spinning="loading || aaLoading">
      <div v-if="!models.length" class="empty">暂无模型</div>
      <div class="card-grid">
        <div v-for="m in filtered" :key="m.id" class="model-card" :class="m.modality">
          <div class="card-head">
            <div class="card-title-row">
              <span class="model-name">{{ fmtName(m) }}</span>
              <a-tag :color="m.modality === 'multimodal' ? 'purple' : 'default'" class="modality-tag">
                <PictureOutlined v-if="m.modality === 'multimodal'" style="margin-right: 4px" /><FileTextOutlined v-else style="margin-right: 4px" />{{ m.modality === 'multimodal' ? '多模态' : '纯文本' }}
              </a-tag>
            </div>
            <code class="model-id">{{ m.id }}</code>
          </div>

          <!-- 能力标签 -->
          <div class="tag-row">
            <a-tag v-if="reasoningTag(m)" :color="reasoningTag(m)!.color">{{ reasoningTag(m)!.text }}</a-tag>
            <a-tag v-if="m.supportsToolCall" color="gold"><ToolOutlined style="margin-right: 4px" />工具调用</a-tag>
            <a-tag v-for="e in reasoningEfforts(m)" :key="e" color="purple" class="effort-tag">{{ e }}</a-tag>
          </div>

          <!-- 指标区 -->
          <div class="metric-grid">
            <div class="mi"><span class="mi-k">成本</span><span class="mi-v">{{ m.credits !== undefined && m.credits !== null ? `x${m.credits.toFixed(2)}` : '-' }}</span></div>
            <div class="mi"><span class="mi-k">上下文</span><span class="mi-v">{{ fmtTokens(m.context_length) }}</span></div>
            <div class="mi"><span class="mi-k">最大输出</span><span class="mi-v">{{ fmtTokens(m.max_output_tokens) }}</span></div>
            <div class="mi"><span class="mi-k">温度</span><span class="mi-v">{{ m.temperature !== undefined ? m.temperature : '-' }}</span></div>
          </div>

          <!-- 描述 -->
          <div v-if="m.description" class="desc">{{ m.description }}</div>

          <!-- AA 评测 -->
          <div v-if="m.benchmark" class="aa-box">
            <div class="aa-head">
              <span class="aa-title"><ThunderboltOutlined style="margin-right: 4px" /> Artificial Analysis</span>
              <a :href="m.benchmark.aa_url" target="_blank" rel="noopener" class="aa-link">榜单 ↗</a>
            </div>
            <div class="aa-scores">
              <div class="aa-score"><span class="as-v">{{ fmtNum(m.benchmark.intelligence_index) }}</span><span class="as-k">智能</span></div>
              <div class="aa-score"><span class="as-v">{{ fmtNum(m.benchmark.coding_index) }}</span><span class="as-k">编码</span></div>
              <div class="aa-score"><span class="as-v">{{ fmtNum(m.benchmark.agentic_index) }}</span><span class="as-k">Agentic</span></div>
            </div>
          </div>
        </div>
      </div>
    </a-spin>

    <!-- 未配置 AA key 提示 -->
    <a-alert
      v-if="!aaConfigured"
      type="info"
      show-icon
      style="margin-top: 16px"
      message="未配置 Artificial Analysis 评测"
      description="在「账号 → 自动签到设置」中填入 AA API Key 后，将在此展示各模型的权威评测数据（智能/编码/数学指数、MMLU-Pro、速度、价格）。"
    />
  </div>
</template>

<style scoped>
.toolbar {
  display: flex; gap: 10px; flex-wrap: wrap; align-items: center; margin-bottom: 14px;
}
.search { width: 260px; }
.src-tag { margin-left: 4px; }
.src-dyn { background: rgba(99,179,237,0.16) !important; color: #7cc0f5 !important; border-color: rgba(99,179,237,0.4) !important; }
.src-static { background: rgba(245,158,11,0.16) !important; color: #fbbf24 !important; border-color: rgba(245,158,11,0.4) !important; }
.count { color: #8a94a6; font-size: 13px; margin-left: auto; }

.metric-strip {
  display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 16px;
}
.metric-chip {
  display: flex; align-items: baseline; gap: 6px;
  background: linear-gradient(135deg, #1a1f36 0%, #232946 100%);
  border: 1px solid rgba(99, 179, 237, 0.2);
  border-radius: 10px; padding: 8px 16px; color: #c8d3e8;
  box-shadow: 0 2px 10px rgba(0,0,0,0.15);
}
.m-num { font-size: 20px; font-weight: 700; color: #63b3ed; }
.m-label { font-size: 12px; color: #8a94a6; }

.empty { padding: 40px; text-align: center; color: #8a94a6; }

.card-grid {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: 16px;
}
.model-card {
  position: relative; border-radius: 14px; padding: 16px;
  background: linear-gradient(150deg, #1b2038 0%, #232a4a 100%);
  border: 1px solid rgba(99, 179, 237, 0.18);
  color: #e6edf7;
  transition: transform 0.18s ease, box-shadow 0.18s ease, border-color 0.18s ease;
  box-shadow: 0 4px 18px rgba(0,0,0,0.18);
  overflow: hidden;
}
.model-card::before {
  content: ''; position: absolute; top: 0; left: 0; right: 0; height: 3px;
  background: linear-gradient(90deg, #63b3ed, #8b5cf6, #ec4899);
  opacity: 0.85;
}
.model-card:hover {
  transform: translateY(-4px);
  border-color: rgba(99, 179, 237, 0.45);
  box-shadow: 0 10px 30px rgba(20, 30, 60, 0.35);
}
.model-card.text::before { background: linear-gradient(90deg, #60a5fa, #818cf8); }

.card-head { margin-bottom: 10px; }
.card-title-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.model-name { font-size: 17px; font-weight: 700; }
.modality-tag { margin-left: auto; }
.modality-tag.ant-tag { background: rgba(148,163,184,0.14) !important; border-color: rgba(148,163,184,0.35) !important; color: #cbd5e1 !important; }
.model-card.multimodal .modality-tag.ant-tag { background: rgba(167,139,250,0.16) !important; border-color: rgba(167,139,250,0.4) !important; color: #c4b5fd !important; }
.model-id { font-size: 12px; color: #8a94a6; }

.tag-row { display: flex; flex-wrap: wrap; gap: 4px; margin-bottom: 12px; }
.effort-tag { margin-right: 0 !important; }

.metric-grid {
  display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px;
  background: rgba(0,0,0,0.18); border-radius: 10px; padding: 10px 14px 10px 12px; margin-bottom: 10px;
}
.mi { display: flex; justify-content: space-between; align-items: center; min-width: 0; }
.mi-k { font-size: 12px; color: #8a94a6; flex-shrink: 0; }
.mi-v { font-size: 14px; font-weight: 600; color: #63b3ed; padding-left: 8px; white-space: nowrap; }

.desc { font-size: 12px; color: #9aa5bd; line-height: 1.5; margin-bottom: 10px; }

.aa-box {
  border: 1px solid rgba(139, 92, 246, 0.35);
  background: linear-gradient(135deg, rgba(139,92,246,0.12), rgba(99,179,237,0.10));
  border-radius: 10px; padding: 10px 12px;
}
.aa-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
.aa-title { font-size: 12px; font-weight: 700; color: #c4b5fd; }
.aa-link { font-size: 12px; color: #63b3ed; }
.aa-scores { display: grid; grid-template-columns: repeat(5, 1fr); gap: 4px; text-align: center; }
.aa-score { display: flex; flex-direction: column; align-items: center; }
.as-v { font-size: 15px; font-weight: 700; color: #a5f3fc; }
.as-k { font-size: 10px; color: #8a94a6; margin-top: 2px; }
.aa-price { font-size: 11px; color: #9aa5bd; margin-top: 8px; }
</style>
