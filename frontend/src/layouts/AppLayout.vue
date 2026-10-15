<script setup>
/**
 * 后台框架：侧栏导航 + 顶栏。
 *
 * 菜单按角色渲染（R-FE2-12 / PRD §2.1.1）：
 *   reviewer   → 大盘 / 工作台 / 趋势分析 / 审计(仅本人) / 个人中心
 *   strategist → 大盘 / 规则 / 名单 / 特征阈值 / 仿真 / AI 助手 / 趋势分析 / 审计 / 个人中心
 *   analyst    → 大盘 / AI 助手 / 趋势分析 / 个人中心
 *   admin      → 全部 12 个业务页面
 *   ops        → 大盘 / 趋势分析(只读) / 健康度 / 个人中心
 *
 * 可见性由 stores/user.js 的 ROLE_MENUS 决定（key 对应路由 name）。
 */
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { useUserStore } from '@/stores/user'

const route = useRoute()
const router = useRouter()
const store = useUserStore()

/** 菜单定义：key 对应 stores/user.js 的 ROLE_MENUS 与路由 meta.menu */
const MENUS = [
  { group: '概览', items: [{ key: 'dashboard', label: '态势大盘', to: '/dashboard' }] },
  {
    group: '风控审核',
    items: [{ key: 'workbench', label: '审核工作台', to: '/workbench' }],
  },
  {
    group: '策略配置',
    items: [
      { key: 'rules', label: '规则配置', to: '/strategy/rules' },
      { key: 'watchlist', label: '名单库管理', to: '/strategy/watchlist' },
      { key: 'features', label: '特征与阈值', to: '/strategy/features' },
    ],
  },
  {
    group: '仿真与智能',
    items: [
      { key: 'simulate', label: '事件仿真', to: '/simulate' },
      { key: 'chat', label: 'AI 智能助手', to: '/chat' },
    ],
  },
  {
    group: '分析监控',
    items: [
      { key: 'analysis', label: '趋势分析', to: '/analysis' },
      { key: 'audit', label: '审计日志', to: '/audit' },
      { key: 'health', label: '系统健康度', to: '/system/health' },
    ],
  },
  {
    group: '个人',
    items: [{ key: 'profile', label: '个人中心', to: '/profile' }],
  },
]

/** 按当前角色过滤出可见菜单分组 */
const visibleGroups = computed(() => {
  const allowed = new Set(store.menus)
  return MENUS
    .map((g) => ({ ...g, items: g.items.filter((i) => allowed.has(i.key)) }))
    .filter((g) => g.items.length > 0)
})

const activeKey = computed(() => route.meta.menu)

async function handleLogout() {
  await store.logout()
  router.replace('/login')
}
</script>

<template>
  <div class="layout">
    <aside class="sidebar">
      <div class="logo">
        <span class="mark">
          <svg width="13" height="13" viewBox="0 0 16 16" fill="none">
            <path d="M8 1.5l5.5 3v6L8 13.5 2.5 10.5v-6z" stroke="#fff" stroke-width="1.6" stroke-linejoin="round"/>
          </svg>
        </span>
        风控中心
      </div>

      <nav class="nav">
        <template v-for="group in visibleGroups" :key="group.group">
          <div class="nav-group">{{ group.group }}</div>
          <router-link
            v-for="item in group.items"
            :key="item.key"
            :to="item.to"
            :class="{ active: activeKey === item.key }"
          >
            {{ item.label }}
          </router-link>
        </template>
      </nav>

      <div class="sidebar-foot">ERCS v3.0 · {{ store.roleLabel }}</div>
    </aside>

    <div class="main">
      <header class="header">
        <div class="crumb">
          风控中心 / <b>{{ route.meta.title || '' }}</b>
        </div>
        <div class="header-right">
          <div class="user-chip">
            {{ store.displayName }}
            <span class="role-tag">{{ store.roleLabel }}</span>
          </div>
          <button class="btn btn-sm" @click="handleLogout">登出</button>
          <router-link to="/profile" class="avatar" :title="store.displayName">
            {{ store.displayName.slice(0, 1) || 'U' }}
          </router-link>
        </div>
      </header>

      <div class="content">
        <router-view />
      </div>
    </div>
  </div>
</template>
