/**
 * 路由表与全局前置守卫。
 *
 * 对应 SPEC §8.1 页面 → 路由 → 组件映射（13 个页面）与 R-FE2-11 / R-FE2-15。
 * meta.roles 与 PRD §2.1.1 三级权限矩阵严格一致。
 */
import { createRouter, createWebHistory } from 'vue-router'

import { ROLE_HOME, useUserStore } from '@/stores/user'

/** 五个角色（与 stores/user.js 的 ROLES 一致） */
const ALL_ROLES = ['reviewer', 'strategist', 'analyst', 'admin', 'ops']

const routes = [
  { path: '/', redirect: '/dashboard' },

  // ---------- 01 登录页（匿名） ----------
  {
    path: '/login',
    name: 'login',
    component: () => import('@/views/Login.vue'),
    meta: { public: true, title: '登录' },
  },

  // ---------- 02 态势大盘 ----------
  {
    path: '/dashboard',
    name: 'dashboard',
    component: () => import('@/views/Dashboard.vue'),
    meta: { roles: ALL_ROLES, title: '态势大盘', menu: 'dashboard' },
  },

  // ---------- 03 审核工作台（案件列表） ----------
  {
    path: '/workbench',
    name: 'workbench',
    component: () => import('@/views/Workbench.vue'),
    meta: { roles: ['reviewer', 'admin'], title: '审核工作台', menu: 'workbench' },
  },

  // ---------- 04 案件详情 ----------
  {
    path: '/workbench/case/:id',
    name: 'case-detail',
    component: () => import('@/views/CaseDetail.vue'),
    meta: { roles: ['reviewer', 'admin'], title: '案件详情', menu: 'workbench' },
  },

  // ---------- 05 策略规则配置（审核员 / 分析师 / 管理员只读可见） ----------
  {
    path: '/strategy/rules',
    name: 'rules',
    component: () => import('@/views/StrategyRules.vue'),
    meta: { roles: ['reviewer', 'strategist', 'analyst', 'admin'], title: '规则配置', menu: 'rules' },
  },

  // ---------- 06 名单库管理（审核员 / 分析师 / 管理员只读可见） ----------
  {
    path: '/strategy/watchlist',
    name: 'watchlist',
    component: () => import('@/views/Watchlist.vue'),
    meta: { roles: ['reviewer', 'strategist', 'analyst', 'admin'], title: '名单库管理', menu: 'watchlist' },
  },

  // ---------- 07 特征字典与阈值配置（运维不可见） ----------
  {
    path: '/strategy/features',
    name: 'features',
    component: () => import('@/views/FeatureConfig.vue'),
    meta: { roles: ['reviewer', 'strategist', 'analyst', 'admin'], title: '特征与阈值', menu: 'features' },
  },

  // ---------- 08 事件仿真测试 ----------
  {
    path: '/simulate',
    name: 'simulate',
    component: () => import('@/views/Simulate.vue'),
    meta: { roles: ['strategist', 'admin'], title: '事件仿真', menu: 'simulate' },
  },

  // ---------- 09 AI 智能助手 ----------
  {
    path: '/chat',
    name: 'chat',
    component: () => import('@/views/Chat.vue'),
    meta: { roles: ['strategist', 'analyst', 'admin'], title: 'AI 智能助手', menu: 'chat' },
  },

  // ---------- 10 趋势与规则效果分析 ----------
  {
    path: '/analysis',
    name: 'analysis',
    component: () => import('@/views/Analysis.vue'),
    meta: { roles: ALL_ROLES, title: '趋势分析', menu: 'analysis' },
  },

  // ---------- 11 审计日志（全角色，数据范围不同，analyst 除外） ----------
  {
    path: '/audit',
    name: 'audit',
    component: () => import('@/views/AuditLog.vue'),
    meta: { roles: ['reviewer', 'strategist', 'admin', 'ops'], title: '审计日志', menu: 'audit' },
  },

  // ---------- 12 系统健康度 ----------
  {
    path: '/system/health',
    name: 'health',
    component: () => import('@/views/SystemHealth.vue'),
    meta: { roles: ['admin', 'ops'], title: '系统健康度', menu: 'health' },
  },

  // ---------- 13 个人中心 ----------
  {
    path: '/profile',
    name: 'profile',
    component: () => import('@/views/Profile.vue'),
    meta: { roles: ALL_ROLES, title: '个人中心', menu: 'profile' },
  },

  // ---------- 403 无权限 ----------
  {
    path: '/403',
    name: 'forbidden',
    component: () => import('@/views/Forbidden.vue'),
    meta: { roles: ALL_ROLES, title: '无访问权限' },
  },

  { path: '/:pathMatch(.*)*', redirect: '/403' },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
  scrollBehavior: () => ({ top: 0 }),
})

// 全局前置守卫（R-FE2-11 / R-FE2-15）
router.beforeEach((to) => {
  const store = useUserStore()

  // 未登录：一律重定向登录页，并在 URL 保留原目标路径（FR-54）
  if (!store.isLoggedIn) {
    if (to.meta.public) return true
    return { path: '/login', query: { redirect: to.fullPath } }
  }

  // 已登录再访问登录页 → 回各自角色落地页
  if (to.meta.public) {
    return { path: store.home }
  }

  // 角色不匹配：渲染 403 页，不跳首页也不留白（R-FE2-15）
  const allowed = to.meta.roles
  if (allowed && !allowed.includes(store.role)) {
    return { path: '/403', query: { from: to.fullPath } }
  }

  return true
})

router.afterEach((to) => {
  document.title = to.meta.title ? `${to.meta.title} · ERCS` : 'ERCS 电商风险控制系统'
})

export { ROLE_HOME }
export default router
