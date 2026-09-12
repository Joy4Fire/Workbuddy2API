<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { message } from 'ant-design-vue'
import { PlusOutlined, CopyOutlined, KeyOutlined } from '@ant-design/icons-vue'
import { api } from '@/api/client'
import type { AppInfo } from '@/types'
import dayjs from 'dayjs'

const apps = ref<AppInfo[]>([])
const loading = ref(false)

// 创建弹窗
const createOpen = ref(false)
const newName = ref('')
const newNote = ref('')
const creating = ref(false)

// 新 key 展示弹窗（明文仅此一次）
const keyVisible = ref(false)
const newKey = ref('')
const newKeyName = ref('')

function fmtTime(ts: number) {
  return dayjs(ts * 1000).format('YYYY-MM-DD HH:mm')
}
function fmtTokens(v: number) {
  if (v >= 1000000) return `${(v / 1000000).toFixed(1)}M`
  if (v >= 1000) return `${(v / 1000).toFixed(1)}K`
  return String(v || 0)
}

async function load() {
  loading.value = true
  try {
    const res = await api.apps()
    apps.value = res.apps
  } finally {
    loading.value = false
  }
}

async function onCreate() {
  const name = newName.value.trim()
  if (!name) {
    message.warning('请输入应用名称')
    return
  }
  creating.value = true
  try {
    const res = await api.createApp(name, newNote.value.trim())
    newKey.value = res.key
    newKeyName.value = res.name
    keyVisible.value = true
    newName.value = ''
    newNote.value = ''
    createOpen.value = false
    await load()
  } finally {
    creating.value = false
  }
}

async function onToggle(a: AppInfo) {
  await api.toggleApp(a.id)
  message.success(a.enabled ? '已停用该应用' : '已启用该应用')
  await load()
}

// 查看明文 Key
const viewOpen = ref(false)
const viewKey = ref('')
const viewKeyName = ref('')
const viewUnavailable = ref(false)
const viewLoading = ref(false)

async function onViewKey(a: AppInfo) {
  viewLoading.value = true
  viewOpen.value = true
  viewKeyName.value = a.name
  viewKey.value = ''
  viewUnavailable.value = false
  try {
    const res = await api.appKey(a.id)
    if (res.unavailable || !res.key) {
      viewUnavailable.value = true
    } else {
      viewKey.value = res.key
    }
  } finally {
    viewLoading.value = false
  }
}

async function onDelete(a: AppInfo) {
  await api.deleteApp(a.id)
  message.success('已删除应用')
  await load()
}

function copyText(text: string, label = 'API Key') {
  navigator.clipboard?.writeText(text).then(
    () => message.success(`已复制 ${label}`),
    () => message.warning('复制失败，请手动复制'),
  )
}

onMounted(load)
</script>

<template>
  <div style="padding-bottom: 36px">
    <!-- 顶部说明 -->
    <a-alert
      type="info"
      show-icon
      style="margin-bottom: 16px"
      message="应用 API Key"
      description="为不同应用/用途创建独立的 API Key（sk-xxx 格式）。调用网关时使用对应 Key，使用记录中即可区分请求来自哪个应用、便于追溯。Key 已加密保存，可随时点「查看 Key」重新查看。"
    />

    <div style="margin-bottom: 12px; display: flex; gap: 8px; align-items: center; justify-content: space-between">
      <span style="color: #8a94a6; font-size: 13px">共 {{ apps.length }} 个应用</span>
      <a-button type="primary" @click="createOpen = true"><template #icon><PlusOutlined /></template>新建应用</a-button>
    </div>

    <a-card title="应用列表">
      <!-- 列宽合计 ~1160px，窄窗口下开横向滚动并固定操作列，避免按钮被卡片右缘裁掉 -->
      <a-table
        :data-source="apps"
        :loading="loading"
        row-key="id"
        :pagination="false"
        :scroll="{ x: 1160 }"
        table-layout="fixed"
      >
        <a-table-column title="应用名称" data-index="name" key="name" :width="140">
          <template #default="{ record }">
            <span style="font-weight: 600; color: #e6edf7">{{ record.name }}</span>
          </template>
        </a-table-column>
        <a-table-column title="API Key" key="key" :width="150">
          <template #default="{ record }">
            <code style="color: #7cc0f5">{{ record.key_prefix }}</code>
          </template>
        </a-table-column>
        <a-table-column title="备注" key="note" :width="180" ellipsis>
          <template #default="{ record }">
            <span v-if="record.note" style="color: #a5b8d8">{{ record.note }}</span>
            <span v-else style="color: #5b6476">-</span>
          </template>
        </a-table-column>
        <a-table-column title="请求数" key="requests" :width="80" align="right">
          <template #default="{ record }">{{ record.requests }}</template>
        </a-table-column>
        <a-table-column title="Tokens" key="tokens" :width="90" align="right">
          <template #default="{ record }">{{ fmtTokens(record.tokens) }}</template>
        </a-table-column>
        <a-table-column title="积分" key="credits" :width="80" align="right">
          <template #default="{ record }">{{ record.credits ? record.credits.toFixed(2) : '-' }}</template>
        </a-table-column>
        <a-table-column title="状态" key="enabled" :width="80">
          <template #default="{ record }">
            <a-tag :color="record.enabled ? 'green' : 'default'">
              {{ record.enabled ? '启用' : '停用' }}
            </a-tag>
          </template>
        </a-table-column>
        <a-table-column title="创建时间" key="created_at" :width="140">
          <template #default="{ record }">{{ fmtTime(record.created_at) }}</template>
        </a-table-column>
        <a-table-column title="操作" key="action" :width="220" fixed="right">
          <template #default="{ record }">
            <a-space>
              <a-button size="small" @click="onViewKey(record)"><template #icon><KeyOutlined /></template>查看 Key</a-button>
              <a-button size="small" :danger="record.enabled" @click="onToggle(record)">
                {{ record.enabled ? '停用' : '启用' }}
              </a-button>
              <a-popconfirm title="删除后该 Key 将立即失效，确定删除？" ok-text="删除" cancel-text="取消" @confirm="onDelete(record)">
                <a-button size="small" danger>删除</a-button>
              </a-popconfirm>
            </a-space>
          </template>
        </a-table-column>
      </a-table>
    </a-card>

    <!-- 新建应用弹窗 -->
    <a-modal v-model:open="createOpen" title="新建应用" :confirm-loading="creating" ok-text="创建" @ok="onCreate">
      <div style="margin-bottom: 12px">
        <div style="margin-bottom: 6px; color: #cdd6e8">应用名称</div>
        <a-input v-model:value="newName" placeholder="如：我的网站 / 脚本任务 / 测试环境" maxlength="40" @pressEnter="onCreate" />
      </div>
      <div>
        <div style="margin-bottom: 6px; color: #cdd6e8">备注（可选）</div>
        <a-textarea v-model:value="newNote" placeholder="说明这个应用/用途" :rows="2" maxlength="200" />
      </div>
    </a-modal>

    <!-- 新 Key 展示弹窗 -->
    <a-modal v-model:open="keyVisible" title="API Key 已生成" :footer="null" width="520px">
      <div style="margin-bottom: 8px; color: #cdd6e8">应用「{{ newKeyName }}」的 API Key（已加密保存，可随时在列表点「查看 Key」重新查看）：</div>
      <a-input v-model:value="newKey" read-only>
        <template #prefix><KeyOutlined style="color: #8b5cf6" /></template>
        <template #suffix>
          <a-button size="small" type="text" @click="copyText(newKey)"><CopyOutlined /> 复制</a-button>
        </template>
      </a-input>
      <div style="margin-top: 10px; color: #8a94a6; font-size: 12px">
        调用时在请求头加 <code>Authorization: Bearer {{ newKey }}</code> 或 <code>X-Api-Key: {{ newKey }}</code>
      </div>
    </a-modal>

    <!-- 查看 Key 弹窗 -->
    <a-modal v-model:open="viewOpen" title="查看 API Key" :footer="null" width="520px">
      <div v-if="viewUnavailable" style="color: #e6a23c">
        <a-alert type="warning" show-icon message="该应用创建于旧版本，无法还原明文 Key。如需使用，请删除后重新创建。" />
      </div>
      <template v-else>
        <div style="margin-bottom: 8px; color: #cdd6e8">应用「{{ viewKeyName }}」的 API Key：</div>
        <a-input :value="viewKey" read-only :loading="viewLoading">
          <template #prefix><KeyOutlined style="color: #8b5cf6" /></template>
          <template #suffix>
            <a-button size="small" type="text" @click="copyText(viewKey)"><CopyOutlined /> 复制</a-button>
          </template>
        </a-input>
        <div style="margin-top: 10px; color: #8a94a6; font-size: 12px">
          调用时在请求头加 <code>Authorization: Bearer {{ viewKey }}</code> 或 <code>X-Api-Key: {{ viewKey }}</code>
        </div>
      </template>
    </a-modal>
  </div>
</template>
