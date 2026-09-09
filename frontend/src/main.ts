import { createApp } from 'vue'
import { createRouter, createWebHashHistory } from 'vue-router'
import Antd from 'ant-design-vue'
import 'ant-design-vue/dist/reset.css'
import App from './App.vue'

// 路由懒加载：每个页面独立 chunk，首屏只加载当前页
const router = createRouter({
  // 使用 hash 模式，避免后端需要 history 回退（静态托管更简单）
  history: createWebHashHistory(),
  routes: [
    { path: '/', redirect: '/overview' },
    { path: '/overview', component: () => import('./views/Overview.vue'), meta: { title: '概览' } },
    { path: '/accounts', component: () => import('./views/Accounts.vue'), meta: { title: '账号' } },
    { path: '/models', component: () => import('./views/Models.vue'), meta: { title: '模型' } },
    { path: '/usage', component: () => import('./views/Usage.vue'), meta: { title: '用量' } },
    { path: '/records', component: () => import('./views/Records.vue'), meta: { title: '使用记录' } },
    { path: '/apps', component: () => import('./views/Apps.vue'), meta: { title: '应用' } },
  ],
})

const app = createApp(App)
app.use(router)
app.use(Antd)
app.mount('#app')
