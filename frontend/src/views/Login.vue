<script setup>
/** 页面一 /login —— 登录页（SPEC 模块 11 / FR-66~70 / R-FE2-12） */
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { useUserStore } from '@/stores/user'

const route = useRoute()
const router = useRouter()
const store = useUserStore()

const form = reactive({ username: 'reviewer', password: 'reviewer@123' })
const loading = ref(false)
const errorMsg = ref('')

/** 5 个演示账号（SPEC §10.1）：点击自动填入，登录后按角色跳转不同落地页 */
const DEMOS = [
  { username: 'reviewer', password: 'reviewer@123', label: '审核员', cls: 'tag-red' },
  { username: 'strategist', password: 'strategist@123', label: '策略师', cls: 'tag-purple' },
  { username: 'analyst', password: 'analyst@123', label: '分析师', cls: 'tag-blue' },
  { username: 'admin', password: 'admin@123', label: '管理员', cls: 'tag-green' },
  { username: 'ops', password: 'ops@123', label: '运维', cls: 'tag-gray' },
]

function fill(demo) {
  form.username = demo.username
  form.password = demo.password
  errorMsg.value = ''
}

async function submit() {
  if (!form.username || !form.password) {
    errorMsg.value = '请输入账号与密码'
    return
  }
  loading.value = true
  errorMsg.value = ''
  try {
    await store.login(form.username, form.password)
    // 登录成功后按角色跳转（FR-54）：优先回跳原目标路径，否则走角色落地页
    const redirect = route.query.redirect
    router.replace(typeof redirect === 'string' && redirect ? redirect : store.home)
  } catch (err) {
    errorMsg.value = err.message || '登录失败，请重试'
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="login-wrap">
    <div class="login-card">
      <div class="login-brand">
        <span class="mark">
          <svg width="24" height="24" viewBox="0 0 16 16" fill="none">
            <path d="M8 1.5l5.5 3v6L8 13.5 2.5 10.5v-6z" stroke="#fff" stroke-width="1.5" stroke-linejoin="round"/>
            <path d="M8 6.2v3.4" stroke="#fff" stroke-width="1.5" stroke-linecap="round"/>
          </svg>
        </span>
        <h1>电商风险控制系统</h1>
        <p>ERCS · 风控管理平台</p>
      </div>

      <form class="login-form" @submit.prevent="submit">
        <div class="field">
          <label>账号</label>
          <input v-model.trim="form.username" type="text" placeholder="请输入账号" autocomplete="username">
        </div>

        <div class="field">
          <label>密码</label>
          <input v-model="form.password" type="password" placeholder="请输入密码" autocomplete="current-password">
        </div>

        <div v-if="errorMsg" class="alert alert-danger" style="padding:7px 12px">
          <svg width="14" height="14" viewBox="0 0 16 16" fill="none" style="flex:0 0 14px;margin-top:3px">
            <circle cx="8" cy="8" r="6.3" stroke="currentColor" stroke-width="1.5"/>
            <path d="M8 4.8v3.8M8 11v.6" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>
          </svg>
          <span>{{ errorMsg }}</span>
        </div>

        <button type="submit" class="btn btn-primary w-full" style="padding:9px;justify-content:center;font-size:14px" :disabled="loading">
          {{ loading ? '登录中…' : '登 录' }}
        </button>
      </form>

      <div class="divider" style="margin:22px 0 14px"></div>

      <div style="font-size:12px;color:var(--text3)">
        <div style="margin-bottom:6px">演示账号（点击后按角色跳转不同落地页）</div>
        <table style="width:100%;border-collapse:collapse">
          <tr v-for="d in DEMOS" :key="d.username" style="cursor:pointer" @click="fill(d)">
            <td class="mono" style="padding:3px 0">{{ d.username }}</td>
            <td class="mono" style="color:var(--text4)">{{ d.password }}</td>
            <td style="text-align:right">
              <span class="tag" :class="d.cls">{{ d.label }}</span>
            </td>
          </tr>
        </table>
      </div>

      <div style="margin-top:14px;font-size:11.5px;color:var(--text4);line-height:1.7;text-align:center">
        密码以 bcrypt 加盐哈希存储 · 登录成功签发 JWT（有效期 8 小时）<br>
        连续 5 次密码错误将锁定账号
      </div>
    </div>
  </div>
</template>
