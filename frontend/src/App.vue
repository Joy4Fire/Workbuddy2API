<script setup lang="ts">
// 应用外壳：侧边栏 + 顶栏 + 内容区。业务逻辑分别在 components/ 与 views/；
// 全局样式在 styles/base.css（布局）与 styles/dark-theme.css（antd 深色覆盖）。
import { ThunderboltOutlined } from '@ant-design/icons-vue'
// antd 语言包：不配的话**组件内置文案全是英文**——弹窗按钮是 Cancel/OK、
// 分页是「10 / page」、空表是 No Data，混在满屏中文里很突兀。
// 注意：locale 只覆盖组件自带文案；项目自己的文案本来就写的中文，不受影响。
// 目前没有用到日期类组件，所以不需要额外引 dayjs 的 zh-cn（以后加 a-date-picker
// 才需要补 `import 'dayjs/locale/zh-cn'` + `dayjs.locale('zh-cn')`）。
import zhCN from 'ant-design-vue/es/locale/zh_CN'
import AppSidebar from '@/components/AppSidebar.vue'
import TokenManager from '@/components/TokenManager.vue'
</script>

<template>
  <a-config-provider :locale="zhCN">
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
  </a-config-provider>
</template>
