<script setup lang="ts">
import { ref, h } from 'vue'
import { useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import { KeyOutlined } from '@ant-design/icons-vue'
import { setAdminToken } from '@/api/client'

const router = useRouter()

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
</script>

<template>
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
</template>
