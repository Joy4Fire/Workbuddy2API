<script setup lang="ts">
// 应用外壳：侧边栏 + 顶栏 + 内容区。业务逻辑分别在 components/ 与 views/；
// 全局样式在 styles/base.css（布局）与 styles/dark-theme.css（antd 深色覆盖）。
import { ThunderboltOutlined } from '@ant-design/icons-vue'
import AppSidebar from '@/components/AppSidebar.vue'
import TokenManager from '@/components/TokenManager.vue'
</script>

<template>
  <a-layout class="app-shell">
    <AppSidebar />

    <a-layout class="main-col">
      <!-- 顶栏 -->
      <a-layout-header class="app-header">
        <div class="header-left">
          <ThunderboltOutlined class="header-bolt" />
          <span>模型网关控制台</span>
        </div>
        <div class="header-right">
          <span class="status-tag"><i class="status-dot"></i>运行中</span>
          <TokenManager />
        </div>
      </a-layout-header>

      <a-layout-content class="page-content">
        <router-view />
      </a-layout-content>
    </a-layout>
  </a-layout>
</template>
