<script setup lang="ts">
import { ref, h, computed, onMounted, onUnmounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import {
  DashboardOutlined, TeamOutlined, DatabaseOutlined, BarChartOutlined,
  FileTextOutlined, KeyOutlined, ThunderboltOutlined, ApiOutlined,
} from '@ant-design/icons-vue'
import { setAdminToken } from '@/api/client'

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

// 管理 Token（局域网访问时后端设了 ADMIN_TOKEN 才需要）
const tokenInput = ref('')
const tokenVisible = ref(false)

// Token 格式校验：字母数字、下划线、连字符，至少 8 位
function validateToken(t: string): boolean {
  return /^[a-zA-Z0-9_-]{8,}$/.test(t)
}

function saveToken() {
  const t = tokenInput.value.trim()
  if (t && !validateToken(t)) {
    message.error('Token 格式不正确（至少 8 位字母/数字/_-）')
    return
  }
  setAdminToken(t)
  tokenInput.value = ''
  tokenVisible.value = false
  message.success(t ? '已保存管理 Token' : '已清除管理 Token')
  router.go(0) // 重新加载，让后续请求带上 Token
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
  <a-layout class="app-shell">
    <!-- 侧边栏 -->
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

    <a-layout class="main-col">
      <!-- 顶栏 -->
      <a-layout-header class="app-header">
        <div class="header-left">
          <ThunderboltOutlined class="header-bolt" />
          <span>模型网关控制台</span>
        </div>
        <div class="header-right">
          <span class="status-tag"><i class="status-dot"></i>运行中</span>
          <a-popover v-model:open="tokenVisible" title="管理 Token（可选）" trigger="click" placement="bottomRight">
            <template #content>
              <div style="width: 260px">
                <p style="font-size: 12px; color: #888; margin-bottom: 8px">
                  仅当后端设置了 <code>ADMIN_TOKEN</code>（如从局域网访问）时才需要填写。
                </p>
                <a-input
                  v-model:value="tokenInput"
                  placeholder="输入 ADMIN_TOKEN"
                  style="margin-bottom: 8px"
                  @pressEnter="saveToken"
                />
                <a-button type="primary" block size="small" @click="saveToken">保存</a-button>
                <a-button size="small" block style="margin-top: 4px" @click="tokenInput=''; saveToken()">清除</a-button>
              </div>
            </template>
            <a-button size="small" :icon="h(KeyOutlined)" title="管理 Token" class="token-btn">Token 管理</a-button>
          </a-popover>
        </div>
      </a-layout-header>

      <a-layout-content class="page-content">
        <router-view />
      </a-layout-content>
    </a-layout>
  </a-layout>
</template>

<style>
html, body { margin: 0; }
body { background: #0d1120; }
#app { min-height: 100vh; }

/* ---------- 全局暗色滚动条 ----------
   原生白色滚动条在深色主题里非常刺眼（内容区/表格/弹窗/下拉全受影响），
   这里统一覆盖为半透明品牌色细条。 */
::-webkit-scrollbar { width: 10px; height: 10px; }
::-webkit-scrollbar-thumb {
  background: rgba(99, 179, 237, 0.22);
  border-radius: 6px;
  border: 2px solid transparent;
  background-clip: padding-box;
}
::-webkit-scrollbar-thumb:hover { background-color: rgba(99, 179, 237, 0.4); }
::-webkit-scrollbar-track, ::-webkit-scrollbar-corner { background: transparent; }
html { scrollbar-color: rgba(99, 179, 237, 0.3) transparent; scrollbar-width: thin; }
.app-shell {
  height: 100vh;
  overflow: hidden;
  background: #0d1120;
}
.main-col {
  height: 100vh;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}

/* ---------- 侧边栏 ---------- */
.app-sider {
  background: linear-gradient(180deg, #161d38 0%, #0f1428 100%) !important;
  border-right: 1px solid rgba(99, 179, 237, 0.12);
  position: relative;
  overflow: hidden;
}
.app-sider::before {
  content: '';
  position: absolute; top: -60px; right: -60px; width: 180px; height: 180px;
  background: radial-gradient(circle, rgba(99,179,237,0.18), transparent 70%);
  pointer-events: none;
}
.app-sider::after {
  content: '';
  position: absolute; bottom: 90px; left: -50px; width: 160px; height: 160px;
  background: radial-gradient(circle, rgba(139,92,246,0.15), transparent 70%);
  pointer-events: none;
}

.brand {
  display: flex; align-items: center; gap: 12px;
  padding: 22px 20px 18px;
  border-bottom: 1px solid rgba(255,255,255,0.06);
  position: relative; z-index: 1;
}
.brand-logo {
  width: 44px; height: 44px; border-radius: 12px;
  object-fit: cover; display: block;
  box-shadow: 0 4px 14px rgba(99,102,241,0.45);
}
.brand-text { display: flex; flex-direction: column; }
.brand-name {
  font-weight: 800; font-size: 14.5px; letter-spacing: 0.2px; white-space: nowrap;
  background: linear-gradient(90deg, #fff, #c4b5fd);
  -webkit-background-clip: text; background-clip: text; -webkit-text-fill-color: transparent;
}
.brand-sub { font-size: 11px; color: #7d8aa5; margin-top: 2px; }

.menu-title {
  font-size: 11px; color: #5c6a8a; letter-spacing: 2px;
  padding: 16px 24px 8px; font-weight: 600;
  position: relative; z-index: 1;
}

.app-menu {
  background: transparent !important;
  border-right: none !important;
  position: relative; z-index: 1;
  /* 矮窗口下菜单可纵向滚动（底部留出 sider-footer 的高度，避免最后一项被压住）。
     必须显式 overflow-x: hidden：antd inline 菜单的固有宽度比容器宽 ~12px，
     overflow-y: auto 会连带把 overflow-x 也变成 auto，侧边栏底部会冒出横向滚动条 */
  flex: 1;
  overflow-y: auto;
  overflow-x: hidden;
  padding-bottom: 64px;
}
.app-sider .ant-layout-sider-children { display: flex; flex-direction: column; height: 100%; }
.app-menu .ant-menu-item {
  height: 52px; line-height: 1.2;
  /* antd 默认 item 宽度按 4px 边距计算（calc(100% - 8px)）；这里自定义 10px 边距，
     宽度必须同步改为 calc(100% - 20px)，否则每项右溢 12px：
     选中高亮会顶到侧边栏边缘、菜单底部还会冒出横向滚动条 */
  margin: 4px 10px; border-radius: 10px;
  width: calc(100% - 20px);
  color: #9aa5bd;
  transition: all 0.2s ease;
  position: relative;
  display: flex; align-items: center;
  padding: 0 14px !important;
}
.app-menu .ant-menu-item .ant-menu-item-icon { font-size: 17px; }
.app-menu .ant-menu-item .menu-text {
  display: flex; flex-direction: column; line-height: 1.2;
}
.app-menu .ant-menu-item .menu-label { font-size: 14px; font-weight: 600; }
.app-menu .ant-menu-item .menu-desc {
  font-size: 11px; color: #8794ad;
  margin-top: 2px;
}
.app-menu .ant-menu-item:hover {
  color: #fff !important;
  background: rgba(99,179,237,0.10) !important;
}
.app-menu .ant-menu-item-selected {
  color: #fff !important;
  background: linear-gradient(90deg, rgba(99,179,237,0.22), rgba(139,92,246,0.22)) !important;
  box-shadow: inset 0 0 0 1px rgba(99,179,237,0.35), 0 4px 14px rgba(99,179,237,0.15);
}
.app-menu .ant-menu-item-selected::before {
  content: '';
  position: absolute; left: 0; top: 10px; bottom: 10px; width: 3px;
  border-radius: 3px;
  background: linear-gradient(180deg, #63b3ed, #8b5cf6);
  box-shadow: 0 0 8px rgba(99,179,237,0.8);
}
.app-menu .ant-menu-item-selected .menu-desc { color: #a5b8d8; }
.app-menu .ant-menu-item-selected .ant-menu-item-icon { color: #c4b5fd; }

.sider-footer {
  position: absolute; bottom: 0; left: 0; right: 0; z-index: 1;
  display: flex; align-items: center; gap: 8px;
  padding: 16px 22px;
  border-top: 1px solid rgba(255,255,255,0.06);
  font-size: 12px; color: #7d8aa5;
}
.health-badge {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 3px 10px; border-radius: 20px; font-size: 12px; font-weight: 600;
}
.health-badge.ok { color: #4ade80; background: rgba(34,197,94,0.14); border: 1px solid rgba(34,197,94,0.35); }
.health-badge.bad { color: #fb7185; background: rgba(244,63,94,0.14); border: 1px solid rgba(244,63,94,0.35); }
.health-badge.loading { color: #8a94a6; background: rgba(148,163,184,0.12); border: 1px solid rgba(148,163,184,0.3); }
.health-dot { width: 7px; height: 7px; border-radius: 50%; background: currentColor; display: inline-block; }
.version-tag { margin-left: auto; color: #5c6a8a; }
.status-dot {
  width: 8px; height: 8px; border-radius: 50%;
  background: #22c55e;
  box-shadow: 0 0 8px rgba(34,197,94,0.9);
  animation: pulse 2s infinite;
}
@keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.4; } }

/* ---------- 顶栏 ---------- */
.app-header {
  background: linear-gradient(90deg, #12182e 0%, #1a2140 100%) !important;
  border-bottom: 1px solid rgba(99,179,237,0.12);
  display: flex; align-items: center; justify-content: space-between;
  padding: 0 24px; height: 56px; line-height: 56px;
}
.header-left { display: flex; align-items: center; gap: 8px; color: #e6edf7; font-weight: 600; font-size: 15px; }
.header-bolt { color: #8b5cf6; font-size: 18px; }
.header-right { display: flex; align-items: center; gap: 14px; }
/* 非交互状态标签（圆点 + 文字，无边框） */
.status-tag {
  display: inline-flex; align-items: center; gap: 6px;
  height: 30px; box-sizing: border-box;
  font-size: 12px; color: #86efac;
  user-select: none;
  background: rgba(34,197,94,0.12);
  border: 1px solid rgba(34,197,94,0.3);
  border-radius: 8px;
  padding: 0 10px;
}
.status-tag .status-dot { width: 7px; height: 7px; background: #4ade80; }
.token-btn {
  color: #e6edf7 !important;
  border-color: rgba(99,179,237,0.45) !important;
  background: rgba(99,179,237,0.12) !important;
  border-radius: 8px !important;
}
.token-btn:hover { border-color: #63b3ed !important; color: #63b3ed !important; }

/* ---------- 内容区（独立滚动，侧边栏/顶栏固定） ---------- */
.page-content {
  flex: 1;
  overflow: auto;
  padding: 24px;
  background:
    radial-gradient(1000px 400px at 15% -10%, rgba(99, 179, 237, 0.12), transparent 60%),
    radial-gradient(900px 400px at 100% 0%, rgba(139, 92, 246, 0.12), transparent 55%),
    linear-gradient(160deg, #0d1120 0%, #141a30 100%);
}
.page-content .ant-card {
  border-radius: 12px;
  box-shadow: 0 4px 16px rgba(0,0,0,0.25);
  border: 1px solid rgba(255,255,255,0.06);
}
.page-content .ant-card-head { font-weight: 700; }

/* ================= 全局深色主题（统一 antd 组件到深色科技风） =================
   注意：非 scoped 全局样式，直接类选择器 + !important，确保覆盖 antd 默认浅色。 */
/* 外层布局背景：彻底深色，杜绝"白套黑" */
.ant-layout { background: #0d1120 !important; }
.ant-layout-content { background: transparent; }

.page-content .ant-card,
body .ant-popover,
body .ant-select-dropdown,
body .ant-picker-dropdown {
  background: #1a2140;
  color: #e6edf7;
}
/* 注意：.ant-modal 外层容器不设背景——它比内容体多 24px padding-bottom，
   上背景会在圆角外露出一条同色矩形，看起来像内容溢出边框（bug 已修）。 */
.page-content .ant-card-head,
body .ant-modal-header,
body .ant-popover-title {
  color: #e6edf7;
  border-bottom-color: rgba(255,255,255,0.08);
}

/* 表格深色化（核心：antd 默认白底 #fff / 表头 #fafafa，必须强制覆盖） */
.page-content .ant-table,
.page-content .ant-table-container,
.page-content .ant-table-content,
.page-content .ant-table-body {
  background: transparent !important;
  color: #cdd6e8;
}
.page-content .ant-table-thead > tr > th {
  /* 必须不透明：横向滚动时表头固定列不能透出底下经过的列（rgba 半透明会露馅） */
  background: #213051 !important;
  color: #a5b8d8 !important;
  border-bottom: 1px solid rgba(255,255,255,0.08) !important;
}
.page-content .ant-table-tbody > tr > td {
  border-bottom: 1px solid rgba(255,255,255,0.06) !important;
  color: #cdd6e8 !important;
}
/* 普通单元格透明；固定列必须保持不透明——否则横向滚动时，
   经过固定列下方的其它列内容会透出来（曾致操作列旁浮现优先级输入框残影） */
.page-content .ant-table-tbody > tr > td:not(.ant-table-cell-fix-left):not(.ant-table-cell-fix-right) {
  background: transparent !important;
}
.page-content .ant-table-tbody > tr:hover > td:not(.ant-table-cell-fix-left):not(.ant-table-cell-fix-right) {
  background: rgba(99,179,237,0.08) !important;
}
.page-content .ant-table-tbody > tr:hover > td.ant-table-cell-fix-left,
.page-content .ant-table-tbody > tr:hover > td.ant-table-cell-fix-right {
  background: #213051 !important;
}
.page-content .ant-table-placeholder,
.page-content .ant-table-expanded-row-fixed {
  background: transparent !important;
}
.page-content .ant-table-cell { color: #cdd6e8 !important; }
/* 固定列（sticky）底色：与卡片背景一致的不透明色 */
.page-content .ant-table-cell-fix-left,
.page-content .ant-table-cell-fix-right {
  background: #1a2140 !important;
}

/* 输入框 / 选择器 / 数字输入 */
.page-content .ant-input,
.page-content .ant-input-affix-wrapper,
.page-content .ant-input-number,
.page-content .ant-select-selector,
.page-content .ant-input-password {
  background: #121a30 !important;
  border-color: rgba(99,179,237,0.25) !important;
  color: #e6edf7 !important;
}
.page-content .ant-input::placeholder { color: #6b7794; }
.page-content .ant-input-number-input { color: #e6edf7 !important; }
.page-content .ant-select-selection-item { color: #e6edf7 !important; }
/* antd v5 默认 placeholder 是深灰（浅色主题值），深色底上几乎看不见——必须显式覆盖 */
.page-content .ant-select-selection-placeholder,
.ant-modal .ant-select-selection-placeholder,
body .ant-select-dropdown .ant-select-selection-placeholder { color: #8a94a6 !important; }
.page-content .ant-select-arrow { color: #8a94a6; }

/* 按钮 */
.page-content .ant-btn {
  background: #1c2545;
  border-color: rgba(99,179,237,0.25);
  color: #cdd6e8;
}
.page-content .ant-btn:hover {
  border-color: #63b3ed; color: #63b3ed;
}
.page-content .ant-btn-primary {
  background: linear-gradient(135deg, #6366f1, #8b5cf6) !important;
  border: none; color: #fff !important;
}
.page-content .ant-btn-primary:hover {
  background: linear-gradient(135deg, #4f52e0, #7c4ce0) !important;
}
.page-content .ant-btn-dangerous { color: #ff6b6b; border-color: rgba(255,107,107,0.4); }

/* 状态标签：实心化，提升深色下识别度 */
.page-content .ant-tag-green,
.page-content .ant-tag-success { background: rgba(34,197,94,0.18) !important; color: #4ade80 !important; border-color: rgba(34,197,94,0.4) !important; }
.page-content .ant-tag-red,
.page-content .ant-tag-error { background: rgba(244,63,94,0.18) !important; color: #fb7185 !important; border-color: rgba(244,63,94,0.4) !important; }
.page-content .ant-tag-blue { background: rgba(99,179,237,0.16) !important; color: #7cc0f5 !important; border-color: rgba(99,179,237,0.4) !important; }
.page-content .ant-tag-purple { background: rgba(167,139,250,0.16) !important; color: #c4b5fd !important; border-color: rgba(167,139,250,0.4) !important; }
.page-content .ant-tag-gold { background: rgba(245,158,11,0.16) !important; color: #fbbf24 !important; border-color: rgba(245,158,11,0.4) !important; }
.page-content .ant-tag-geekblue { background: rgba(79,111,230,0.16) !important; color: #94a6f0 !important; border-color: rgba(79,111,230,0.4) !important; }
.page-content .ant-tag-orange { background: rgba(249,115,22,0.16) !important; color: #fdba74 !important; border-color: rgba(249,115,22,0.4) !important; }
.page-content .ant-tag-cyan { background: rgba(34,211,238,0.16) !important; color: #67e8f9 !important; border-color: rgba(34,211,238,0.4) !important; }
.page-content .ant-tag-default { background: rgba(148,163,184,0.16) !important; color: #cbd5e1 !important; border-color: rgba(148,163,184,0.4) !important; }
.page-content .ant-tag { color: #cdd6e8 !important; border-radius: 6px; }

/* 分页 */
/* 总数文字（"共 N 条"）antd 默认深灰，深色底上看不清 */
.page-content .ant-pagination-total-text { color: #cdd6e8 !important; }
.page-content .ant-pagination-item,
.page-content .ant-pagination-prev .ant-pagination-item-link,
.page-content .ant-pagination-next .ant-pagination-item-link {
  background: #1c2545; border-color: rgba(99,179,237,0.2); color: #cdd6e8;
}
.page-content .ant-pagination-item a { color: #cdd6e8; }
.page-content .ant-pagination-item-active {
  border-color: #6366f1; background: rgba(99,102,241,0.2);
}
.page-content .ant-pagination-item-active a { color: #fff; }

/* 空状态 */
.page-content .ant-empty-description { color: #8a94a6; }

/* 统计数字 */
.page-content .ant-statistic-title { color: #8a94a6; }
.page-content .ant-statistic-content { color: #e6edf7; }

/* 弹窗：antd v5 CSS-in-JS 用 .ant-modal .ant-modal-content(0,2,0) 设白底，
   压过 body .ant-modal-content(0,1,1)；必须用同结构 + !important 才能覆盖。
   弹窗 portal 到 body 下、不在 .page-content 内，内部控件需单独深色化。 */
.ant-modal .ant-modal-content {
  background: #1a2140 !important;
  border: 1px solid rgba(99, 179, 237, 0.18);
  border-radius: 14px;
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.5);
}
.ant-modal .ant-modal-header { background: transparent !important; }
.ant-modal .ant-modal-title { color: #e6edf7 !important; }
.ant-modal .ant-modal-close { color: #8a94a6 !important; }
.ant-modal .ant-modal-close:hover { color: #e6edf7 !important; background: rgba(255,255,255,0.08) !important; }
.ant-modal .ant-modal-body { color: #e6edf7 !important; }
.ant-modal .ant-form-item-label > label { color: #cdd6e8 !important; }

/* 弹窗内输入类控件 */
.ant-modal .ant-input,
.ant-modal .ant-input-affix-wrapper,
.ant-modal .ant-input-number,
.ant-modal .ant-select-selector,
.ant-modal .ant-input-textarea textarea {
  background: #121a30 !important;
  border-color: rgba(99, 179, 237, 0.25) !important;
  color: #e6edf7 !important;
}
.ant-modal .ant-input-number-input { color: #e6edf7 !important; }
.ant-modal .ant-input::placeholder { color: #6b7794 !important; }

/* 弹窗内按钮（.page-content 前缀不生效，需单独深色化） */
.ant-modal .ant-btn-default,
.ant-modal .ant-btn:not(.ant-btn-primary):not(.ant-btn-dangerous):not(.ant-btn-text) {
  background: #1c2545 !important;
  border-color: rgba(99, 179, 237, 0.25) !important;
  color: #cdd6e8 !important;
}
.ant-modal .ant-btn:not(.ant-btn-primary):not(.ant-btn-dangerous):not(.ant-btn-text):hover {
  border-color: #63b3ed !important;
  color: #63b3ed !important;
}
.ant-modal .ant-btn-primary {
  background: linear-gradient(135deg, #6366f1, #8b5cf6) !important;
  border: none !important;
  color: #fff !important;
}
.ant-modal .ant-btn-text { color: #7cc0f5 !important; }
.ant-modal .ant-btn-text:hover { background: rgba(99, 179, 237, 0.12) !important; }

/* 弹窗内 code / alert */
.ant-modal code {
  background: rgba(99, 179, 237, 0.12);
  color: #7cc0f5;
  padding: 1px 6px;
  border-radius: 4px;
  word-break: break-all;
}
.ant-modal .ant-alert { background: rgba(99, 179, 237, 0.08); border-color: rgba(99, 179, 237, 0.2); }

/* 概览积分预警横幅深色化（warning=橙 / info=蓝，文字提亮保证可读） */
.page-content .ant-alert-warning {
  background: rgba(245, 158, 11, 0.12) !important;
  border-color: rgba(245, 158, 11, 0.45) !important;
  border-radius: 10px;
}
.page-content .ant-alert-warning .ant-alert-message { color: #fbbf24 !important; font-weight: 600; }
.page-content .ant-alert-info {
  background: rgba(99, 179, 237, 0.1) !important;
  border-color: rgba(99, 179, 237, 0.4) !important;
  border-radius: 10px;
}
.page-content .ant-alert-info .ant-alert-message { color: #7cc0f5 !important; font-weight: 600; }
.page-content .ant-alert-description { color: #cdd6e8 !important; }

/* 提示条 / 选择下拉 */
body .ant-select-dropdown {
  background: #1a2140; border: 1px solid rgba(99,179,237,0.15);
}
/* antd v5 用 CSS-in-JS 给选项设了 rgba(0,0,0,0.88) 黑字，深色底上看不清；
   需用 !important 强制覆盖到浅色。 */
body .ant-select-item-option,
body .ant-select-item-option-content,
body .ant-select-item-empty {
  color: #e6edf7 !important;
}
body .ant-select-item-option-active { background: rgba(99,179,237,0.12) !important; }
body .ant-select-item-option-selected {
  background: rgba(99,102,241,0.25) !important;
  color: #fff !important;
}
body .ant-select-item-option-selected .ant-select-item-option-content { color: #fff !important; }
/* 下拉里的分组标题（opt-group label）默认是深灰，深色底上看不清 */
body .ant-select-item-group { color: #7d8aa5 !important; }

/* 复选 / 单选框 */
.page-content .ant-checkbox-wrapper { color: #cdd6e8; }

/* 分段控件（radio-button）深色化：覆盖 antd 默认白底 */
.page-content .ant-radio-group .ant-radio-button-wrapper {
  background: #121a30 !important;
  border-color: rgba(99,179,237,0.25) !important;
  color: #8a94a6 !important;
}
.page-content .ant-radio-group .ant-radio-button-wrapper:hover {
  color: #7cc0f5 !important;
}
.page-content .ant-radio-group .ant-radio-button-wrapper-checked {
  background: linear-gradient(135deg, rgba(99,102,241,0.55), rgba(139,92,246,0.55)) !important;
  border-color: #818cf8 !important;
  color: #fff !important;
  font-weight: 600;
  box-shadow: -1px 0 0 0 #818cf8;
}
.page-content .ant-radio-group .ant-radio-button-wrapper:not(:first-child)::before {
  background: rgba(99,179,237,0.25);
}
.page-content .ant-radio-group .ant-radio-button-wrapper:first-child { border-radius: 8px 0 0 8px; }
.page-content .ant-radio-group .ant-radio-button-wrapper:last-child { border-radius: 0 8px 8px 0; }

/* popover / popconfirm（portal 到 body 下，需同结构 + !important 覆盖白底） */
.ant-popover .ant-popover-inner {
  background: #1a2140 !important;
  border: 1px solid rgba(99, 179, 237, 0.15) !important;
}
.ant-popover .ant-popover-title { color: #e6edf7 !important; border-bottom-color: rgba(255,255,255,0.08) !important; }
.ant-popover .ant-popover-message-title { color: #cdd6e8 !important; }
.ant-popover .ant-popover-inner-content,
.ant-popover .ant-popover-arrow::before { color: #e6edf7; }
.ant-popconfirm .ant-popconfirm-message { color: #cdd6e8 !important; }
.ant-popconfirm .ant-popconfirm-description { color: #a5b8d8 !important; }
.ant-popover .ant-btn-default,
.ant-popconfirm .ant-btn-default,
.ant-popover .ant-btn:not(.ant-btn-primary):not(.ant-btn-dangerous):not(.ant-btn-text),
.ant-popconfirm .ant-btn:not(.ant-btn-primary):not(.ant-btn-dangerous):not(.ant-btn-text) {
  background: #1c2545 !important;
  border-color: rgba(99, 179, 237, 0.25) !important;
  color: #cdd6e8 !important;
}
.ant-popover .ant-btn-primary,
.ant-popconfirm .ant-btn-primary {
  background: linear-gradient(135deg, #6366f1, #8b5cf6) !important;
  border: none !important;
  color: #fff !important;
}

/* alert */
.page-content .ant-alert {
  border-radius: 10px;
  background: rgba(99,179,237,0.08);
  border-color: rgba(99,179,237,0.2);
}
.page-content .ant-alert-info .ant-alert-message { color: #cde3f7; }
.page-content .ant-alert-info .ant-alert-description { color: #9db3cf; }

/* message 通知（深色） */
.ant-message .ant-message-notice .ant-message-notice-content {
  background: #1a2140 !important;
  border: 1px solid rgba(99, 179, 237, 0.2) !important;
  border-radius: 10px;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.35);
}
.ant-message .anticon { color: #63b3ed; }
.ant-message .ant-message-notice-content span { color: #e6edf7 !important; }

/* 表格滚动条深色 */
.page-content .ant-table-body::-webkit-scrollbar,
.page-content .ant-table-content::-webkit-scrollbar { height: 8px; width: 8px; }
.page-content .ant-table-body::-webkit-scrollbar-thumb,
.page-content .ant-table-content::-webkit-scrollbar-thumb {
  background: rgba(99,179,237,0.3); border-radius: 4px;
}
.page-content .ant-table-body::-webkit-scrollbar-track,
.page-content .ant-table-content::-webkit-scrollbar-track { background: transparent; }
</style>
