<script setup>
/**
 * 403 无权限页（R-FE2-15 / SPEC §8.1）。
 *
 * 直接访问无权路由时由守卫路由到此页，**不跳首页也不留白**；
 * 越权请求后端同样返回 403 + code 4003 并写审计（R-AU-05 / R-AU-09）。
 */
import { computed } from 'vue'
import { useRoute } from 'vue-router'

import { useUserStore } from '@/stores/user'

const route = useRoute()
const store = useUserStore()

/** 被拦截的目标路径（守卫在 query.from 中透传） */
const target = computed(() => route.query.from || '该页面')
</script>

<template>
  <div class="forbidden-wrap">
    <div class="card forbidden-card">
      <div class="card-bd" style="text-align:center;padding:48px 40px">
        <svg width="70" height="70" viewBox="0 0 64 64" fill="none" style="margin-bottom:20px">
          <circle cx="32" cy="32" r="25" stroke="var(--danger-border)" stroke-width="3"/>
          <path d="M32 19v18" stroke="var(--danger)" stroke-width="3.4" stroke-linecap="round"/>
          <circle cx="32" cy="44" r="2.4" fill="var(--danger)"/>
        </svg>

        <div style="font-size:40px;font-weight:600;line-height:1.1;letter-spacing:-1px">403</div>
        <div style="font-size:16px;font-weight:600;margin:10px 0 8px">当前角色无访问权限</div>
        <div class="muted" style="font-size:13px;line-height:1.9;max-width:430px;margin:0 auto">
          你的角色为 <b>{{ store.roleLabel }}（{{ store.role }}）</b>，无权访问
          <span class="mono">{{ target }}</span>。<br>
          如需访问，请联系系统管理员调整角色。
        </div>

        <div class="alert alert-danger" style="text-align:left;margin:22px 0 0">
          <div>
            越权访问已被记录：<span class="mono">biz_type = auth</span> ·
            <span class="mono">action = 403</span> · <span class="mono">code = 4003</span><br>
            后端返回 403 并同步写入审计日志（R-AU-05 / R-AU-09）
          </div>
        </div>

        <div class="row-gap-12" style="justify-content:center;margin-top:26px">
          <router-link :to="store.home" class="btn btn-primary">回到我的首页</router-link>
          <router-link to="/profile" class="btn">查看我的权限</router-link>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.forbidden-wrap {
  min-height: 100vh;
  background: var(--bg);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
}
.forbidden-card {
  max-width: 620px;
  width: 100%;
  margin: 0;
}
</style>
