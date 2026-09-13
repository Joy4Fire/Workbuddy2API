<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  DashboardOutlined, TeamOutlined, DatabaseOutlined, BarChartOutlined,
  FileTextOutlined, ApiOutlined,
} from '@ant-design/icons-vue'

const route = useRoute()
const router = useRouter()

const menuItems = [
  { key: '/overview', label: '概览', icon: DashboardOutlined, desc: '全局状态' },
  { key: '/accounts', label: '账号', icon: TeamOutlined, desc: '签到与设置' },
  { key: '/models', label: '模型', icon: DatabaseOutlined, desc: '目录与评测' },
  { key: '/usage', label: '用量', icon: BarChartOutlined, desc: '统计报表' },
  { key: '/apps', label: '应用', icon: ApiOutlined, desc: 'API Key 管理' },
  { key: '/records', label: '使用记录', icon: FileTextOutlined, desc: '调用日志' },
]

const selectedKeys = computed(() => [route.path])

function onMenuClick({ key }: { key: string }) {
  router.push(key)
}

// 侧边栏健康账号数指示（每 30s 刷新）
const healthyCount = ref<number | null>(null)
let healthTimer: ReturnType<typeof setInterval> | null = null
async function refreshHealth() {
  try {
    const res = await fetch('/admin/accounts', {
      headers: { 'X-Admin-Token': localStorage.getItem('workbuddy_admin_token') || '' },
    })
    if (!res.ok) { healthyCount.value = null; return }
    const data = await res.json()
    healthyCount.value = (data.accounts || []).filter((a: { healthy?: boolean }) => a.healthy).length
  } catch {
    healthyCount.value = null
  }
}
onMounted(() => {
  refreshHealth()
  healthTimer = setInterval(refreshHealth, 30000)
})
onUnmounted(() => {
  if (healthTimer) clearInterval(healthTimer)
})
</script>

<template>
  <a-layout-sider theme="dark" width="228" class="app-sider">
    <div class="brand">
      <img class="brand-logo" src="/logo.svg" alt="Workbuddy2API" />
      <div class="brand-text">
        <div class="brand-name">Workbuddy2API</div>
        <div class="brand-sub">本地一体化网关</div>
      </div>
    </div>

    <div class="menu-title">导航</div>
    <a-menu
      theme="dark"
      mode="inline"
      :selected-keys="selectedKeys"
      @click="onMenuClick"
      class="app-menu"
    >
      <a-menu-item v-for="item in menuItems" :key="item.key">
        <template #icon>
          <component :is="item.icon" class="menu-icon" />
        </template>
        <span class="menu-text">
          <span class="menu-label">{{ item.label }}</span>
          <span class="menu-desc">{{ item.desc }}</span>
        </span>
      </a-menu-item>
    </a-menu>

    <div class="sider-footer">
      <span v-if="healthyCount !== null" class="health-badge" :class="healthyCount > 0 ? 'ok' : 'bad'">
        <i class="health-dot"></i>{{ healthyCount }} 健康
      </span>
      <span v-else class="health-badge loading"><i class="health-dot"></i>…</span>
      <span class="version-tag">v0.4.1</span>
    </div>
  </a-layout-sider>
</template>
