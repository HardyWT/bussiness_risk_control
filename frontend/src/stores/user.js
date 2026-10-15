/**
 * 用户状态（SPEC §0.4.3 src/stores/user.js、模块 11）。
 *
 * 持有 token / role / 菜单权限。⚠ 前端菜单与按钮可见性只是**体验优化**，
 * 真实鉴权由后端模块 11 的依赖注入强制校验（R-FE2-04 / R-AU-05）。
 */
import { defineStore } from 'pinia'

import { authApi } from '@/api'

/** 五个角色（SPEC 模块 11 / R-AU-11） */
export const ROLES = ['admin', 'reviewer', 'strategist', 'analyst', 'ops']

/** 角色 → 默认落地页（R-FE2-12） */
export const ROLE_HOME = {
  reviewer: '/workbench',
  strategist: '/strategy/rules',
  analyst: '/chat',
  admin: '/dashboard',
  ops: '/dashboard',
}

/** 角色 → 中文标签 */
export const ROLE_LABEL = {
  admin: '管理员',
  reviewer: '风控审核员',
  strategist: '风控策略师',
  analyst: '数据分析师',
  ops: '系统运维',
}

/**
 * 角色 → 可见菜单键（对应路由 name，PRD §2.1.1 / R-FE2-12）。
 *
 * ⚠ PRD §2.1.1 的图例：`✗` = 不可见（菜单不渲染 + 直访 403）、`👁` = 可见但只读。
 *   规则 / 名单 / 特征三页对 reviewer 与 analyst 是 **👁 只读**，因此**要出现在菜单里**
 *   —— 只读是「进去后按钮置灰」，不是「进不去」。v3.0 把这三页只发给 strategist / admin，
 *   与 PRD 不符（后端其实已对 4 个角色放开读）。
 *
 * reviewer   : dashboard, workbench, rules(只读), watchlist(只读), features(只读), analysis, audit, profile
 * strategist : dashboard, rules, watchlist, features, simulate, chat, analysis, audit, profile
 * analyst    : dashboard, rules(只读), watchlist(只读), features(只读), chat, analysis, profile
 * admin      : 全部业务页（含 case-detail，无独立菜单项）
 * ops        : dashboard, analysis, health, profile
 */
export const ROLE_MENUS = {
  reviewer: ['dashboard', 'workbench', 'rules', 'watchlist', 'features',
             'analysis', 'audit', 'profile'],
  strategist: ['dashboard', 'rules', 'watchlist', 'features', 'simulate', 'chat',
               'analysis', 'audit', 'profile'],
  analyst: ['dashboard', 'rules', 'watchlist', 'features', 'chat', 'analysis', 'profile'],
  admin: ['dashboard', 'workbench', 'rules', 'watchlist', 'features', 'simulate', 'chat',
          'analysis', 'audit', 'health', 'profile'],
  ops: ['dashboard', 'analysis', 'health', 'profile'],
}

export const useUserStore = defineStore('user', {
  state: () => ({
    token: sessionStorage.getItem('ercs_token') || '',
    user: JSON.parse(sessionStorage.getItem('ercs_user') || 'null'),
  }),

  getters: {
    isLoggedIn: (s) => Boolean(s.token),
    role: (s) => s.user?.role || '',
    displayName: (s) => s.user?.display_name || '',
    username: (s) => s.user?.username || '',
    roleLabel: (s) => ROLE_LABEL[s.user?.role] || '',
    /** 当前角色可见的菜单键 */
    menus: (s) => ROLE_MENUS[s.user?.role] || [],
    home: (s) => ROLE_HOME[s.user?.role] || '/dashboard',
  },

  actions: {
    async login(username, password) {
      const data = await authApi.login({ username, password })
      this.token = data.access_token
      this.user = data.user
      sessionStorage.setItem('ercs_token', data.access_token)
      sessionStorage.setItem('ercs_user', JSON.stringify(data.user))
      return data
    },

    async logout() {
      try {
        await authApi.logout()
      } catch {
        // 登出接口失败不阻断本地清理（JWT 无状态，R-FE2-13）
      }
      this.clear()
    },

    clear() {
      this.token = ''
      this.user = null
      sessionStorage.removeItem('ercs_token')
      sessionStorage.removeItem('ercs_user')
    },

    hasRole(...roles) {
      return roles.includes(this.user?.role)
    },
  },
})
