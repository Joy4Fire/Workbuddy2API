<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  DashboardOutlined, TeamOutlined, DatabaseOutlined, BarChartOutlined,
  FileTextOutlined, ApiOutlined, SettingOutlined,
} from '@ant-design/icons-vue'

const route = useRoute()
const router = useRouter()

const menuItems = [
  { key: '/overview', label: '概览', icon: DashboardOutlined, desc: '全局状态' },
  { key: '/accounts', label: '账号', icon: TeamOutlined, desc: '账号与签到' },
  { key: '/models', label: '模型', icon: DatabaseOutlined, desc: '目录与评测' },
  { key: '/usage', label: '用量', icon: BarChartOutlined, desc: '统计报表' },
  { key: '/apps', label: '应用', icon: ApiOutlined, desc: 'API Key 管理' },
  { key: '/records', label: '使用记录', icon: FileTextOutlined, desc: '调用日志' },
  { key: '/settings', label: '设置', icon: SettingOutlined, desc: '定时与网络' },
]

const selectedKeys = computed(() => [route.path])

function onMenuClick({ key }: { key: string }) {
  router.push(key)
}

// 版本号**不再硬编码**：以前写死在这里，改了后端忘了改前端就会一直显示旧版本，
// 而且没有任何东西会报错。现在从 `/health` 读（后端唯一真源 = workbuddy_one/__init__.py）。
const appVersion = ref('')
const schemaVersion = ref<number | null>(null)
// 非 null = **本次启动真的发生过 schema 迁移**，值是升级前的版本号。
// 迁移成功那条日志是 logger.info，默认日志级别看不到，所以这里把它显示出来——
// 用户才能确认"我的旧数据确实被自动升上来了"。
const migratedFrom = ref<number | null>(null)

const versionLabel = computed(() => {
  if (!appVersion.value) return '…'
  return schemaVersion.value === null
    ? `v${appVersion.value}`
    : `v${appVersion.value} · db v${schemaVersion.value}`
})

const versionTitle = computed(() => {
  const lines = [`网关版本 v${appVersion.value || '?'}`]
  if (schemaVersion.value !== null) lines.push(`数据库 schema：v${schemaVersion.value}`)
  if (migratedFrom.value !== null) {
    lines.push(`本次启动已由 db v${migratedFrom.value} 自动升级到 v${schemaVersion.value}`)
    lines.push('迁移前已自动备份到 data/backups/（保留最近 5 份）')
  } else {
    lines.push('启动时数据库已是最新版本，无需迁移')
  }
  return lines.join('\n')
})

async function refreshVersion() {
  try {
    const res = await fetch('/health')
    if (!res.ok) return
    const data = await res.json()
    appVersion.value = data.version || ''
    schemaVersion.value = typeof data.schema_version === 'number' ? data.schema_version : null
    migratedFrom.value = typeof data.migrated_from === 'number' ? data.migrated_from : null
  } catch {
    /* 版本信息拿不到就不显示，不影响主功能 */
  }
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
  // 版本/迁移状态在进程生命周期内不变，只取一次即可（健康数才需要轮询）
  refreshVersion()
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
      <span v-if="migratedFrom !== null" class="migrated-badge" :title="versionTitle">数据已升级</span>
      <span class="version-tag" :title="versionTitle">{{ versionLabel }}</span>
    </div>
  </a-layout-sider>
</template>
