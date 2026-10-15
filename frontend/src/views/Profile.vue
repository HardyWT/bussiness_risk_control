<script setup>
/** 页面十三 /profile —— 个人中心（SPEC 模块 11 / FR-67 / FR-69 / R-FE2-12） */
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'

import { authApi } from '@/api'
import StateBlock from '@/components/StateBlock.vue'
import { ROLES, ROLE_LABEL, ROLE_MENUS, useUserStore } from '@/stores/user'

const router = useRouter()
const store = useUserStore()

const loading = ref(true)
const error = ref('')
const toast = ref('')
const me = ref({})

const form = reactive({ old_password: '', new_password: '', confirm_password: '' })
const saving = ref(false)
const formError = ref('')

/** 角色列顺序与标签 */
const ROLE_COLS = ['reviewer', 'strategist', 'analyst', 'admin', 'ops']

/**
 * 五角色权限矩阵（与 SPEC §11.4 接口允许角色映射表 / PRD §2.1.1 严格一致）。
 * 每个单元格取值：yes(可操作/全量) / ro(只读) / own(仅本人) / all(全量) / no(无权限)
 */
const PERM_MATRIX = [
  { label: '态势大盘', cells: ['yes', 'yes', 'yes', 'yes', 'yes'] },
  { label: '案件列表 / 详情', cells: ['yes', 'no', 'no', 'ro', 'no'] },
  { label: '案件处置', cells: ['yes', 'no', 'no', 'no', 'no'] },
  { label: '规则 / 名单 / 仿真', cells: ['no', 'yes', 'no', 'ro', 'no'] },
  { label: '特征字典编辑', cells: ['ro', 'yes', 'ro', 'ro', 'no'] },
  { label: '决策阈值配置', cells: ['no', 'no', 'no', 'yes', 'no'] },
  { label: 'AI 智能助手', cells: ['no', 'yes', 'yes', 'yes', 'no'] },
  { label: '趋势与规则效果分析', cells: ['yes', 'yes', 'yes', 'yes', 'ro'] },
  { label: '审计流水', cells: ['own', 'own', 'no', 'all', 'own'] },
  { label: '系统健康度', cells: ['no', 'no', 'no', 'yes', 'yes'] },
]

/** 单元格码 → 展示文案与样式类 */
const CELL_META = {
  yes: { text: '✓', cls: 'tag-green' },
  ro: { text: '👁 只读', cls: 'tag-blue' },
  own: { text: '仅本人', cls: 'tag-orange' },
  all: { text: '全量', cls: 'tag-green' },
  no: { text: '✗', cls: 'tag-gray' },
}

const myRoleIndex = computed(() => ROLE_COLS.indexOf(store.role))
const menuCount = computed(() => (ROLE_MENUS[store.role] || []).length)
const roleOptions = ROLES

async function load() {
  loading.value = true
  try {
    me.value = await authApi.me()
    error.value = ''
  } catch (err) {
    error.value = err.message
  } finally {
    loading.value = false
  }
}

function resetForm() {
  Object.assign(form, { old_password: '', new_password: '', confirm_password: '' })
  formError.value = ''
}

function validate() {
  if (!form.old_password) return '请输入原密码'
  if (form.new_password.length < 8) return '新密码至少 8 位'
  if (form.new_password !== form.confirm_password) return '两次输入的新密码不一致'
  return ''
}

async function submit() {
  formError.value = validate()
  if (formError.value) return
  saving.value = true
  try {
    await authApi.changePassword({
      old_password: form.old_password,
      new_password: form.new_password,
    })
    toast.value = '密码已更新，旧密码立即失效（已写入审计日志）'
    resetForm()
  } catch (err) {
    formError.value = err.message
  } finally {
    saving.value = false
  }
}

async function logout() {
  await store.logout()
  router.replace('/login')
}

onMounted(load)
</script>

<template>
  <StateBlock :loading="loading" :error="error">
    <div v-if="toast" class="alert alert-info mb-16"><div>{{ toast }}</div></div>

    <div class="grid g-1-2">
      <div class="stack-16">
        <!-- 账号信息（只读） -->
        <div class="card" style="margin-bottom:0">
          <div class="card-hd"><span>账号信息</span><span class="muted">只读</span></div>
          <div class="card-bd">
            <div class="profile-hd" style="margin-bottom:14px">
              <div class="big-avatar">{{ (me.display_name || store.displayName).slice(0, 1) }}</div>
              <div>
                <div style="font-weight:600;font-size:15px">{{ me.display_name || store.displayName }}</div>
                <div class="muted" style="margin-top:3px">{{ me.username || store.username }}</div>
                <div style="margin-top:8px">
                  <span class="tag tag-green">{{ ROLE_LABEL[me.role] || store.roleLabel }} {{ me.role || store.role }}</span>
                </div>
              </div>
            </div>

            <div class="info-row"><span class="k">用户名</span><span class="mono">{{ me.username || store.username }}</span></div>
            <div class="info-row"><span class="k">显示名</span><span>{{ me.display_name || '-' }}</span></div>
            <div class="info-row">
              <span class="k">角色</span>
              <span>{{ ROLE_LABEL[me.role] || '-' }}（{{ me.role || '-' }}）</span>
            </div>
            <div class="info-row">
              <span class="k">账号状态</span>
              <span><span class="tag" :class="me.status === 'active' ? 'tag-green' : 'tag-red'">{{ me.status || 'active' }}</span></span>
            </div>
            <div class="info-row">
              <span class="k">上次登录</span>
              <span class="mono" style="font-size:12.5px">{{ me.last_login_at ? me.last_login_at.replace('T', ' ').slice(0, 19) : '-' }}</span>
            </div>
            <div class="info-row"><span class="k">可见菜单</span><span>{{ menuCount }} 项</span></div>
          </div>
        </div>
      </div>

      <div class="stack-16">
        <!-- 修改密码 -->
        <div class="card" style="margin-bottom:0">
          <div class="card-hd"><span>修改密码</span><span class="muted">FR-69</span></div>
          <div class="card-bd" style="display:flex;flex-direction:column;gap:15px;max-width:460px">
            <div class="field">
              <label>原密码<span class="req">*</span></label>
              <input v-model="form.old_password" type="password" placeholder="请输入当前密码" autocomplete="current-password">
            </div>
            <div class="field">
              <label>新密码<span class="req">*</span> <span class="muted">至少 8 位</span></label>
              <input v-model="form.new_password" type="password" placeholder="请输入新密码" autocomplete="new-password">
            </div>
            <div class="field">
              <label>确认新密码<span class="req">*</span></label>
              <input v-model="form.confirm_password" type="password" placeholder="请再次输入新密码" autocomplete="new-password">
              <div v-if="formError" class="form-error">{{ formError }}</div>
            </div>

            <div class="alert alert-warn">
              <div>密码以 <b>bcrypt 加盐哈希</b>存储，绝不存明文；
                <span class="mono">password_hash</span> 字段对外接口永不返回（R-AU-01）。
                修改成功后旧密码立即失效，并写入一条 <span class="mono">biz_type = auth</span> 的审计记录。</div>
            </div>

            <div class="row-gap-8">
              <button class="btn" @click="resetForm">取消</button>
              <button class="btn btn-primary" :disabled="saving" @click="submit">{{ saving ? '保存中…' : '保存' }}</button>
            </div>
          </div>
        </div>

        <!-- 登出 -->
        <div class="card" style="margin-bottom:0">
          <div class="card-hd"><span>登出</span><span class="muted">FR-69</span></div>
          <div class="card-bd">
            <div class="logout-box">
              <div style="font-size:13px;margin-bottom:12px">
                登出将清除本地 Token 与用户状态，并跳转至登录页。审计日志会新增一条登出记录。
              </div>
              <button class="btn btn-danger" @click="logout">退出登录</button>
            </div>
            <div class="muted" style="margin-top:12px;font-size:11.5px;line-height:1.7">
              JWT 为无状态令牌，服务端不维护会话表；登出 = 后端记审计 + 前端丢弃 Token（R-AU-09 / R-FE2-13）。
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- 我的权限范围 · 五角色对照矩阵 -->
    <div class="card">
      <div class="card-hd">
        <span>我的权限范围 · 五角色对照</span>
        <span class="legend">
          <span><i style="background:var(--success-text)"></i>✓ 可操作 / 全量</span>
          <span><i style="background:#0958d9"></i>👁 只读</span>
          <span><i style="background:var(--warning-text)"></i>仅本人</span>
          <span><i style="background:var(--text4)"></i>✗ 无权限</span>
        </span>
      </div>

      <table class="tb perm">
        <thead>
          <tr>
            <th style="width:210px">权限项</th>
            <th
              v-for="(r, i) in ROLE_COLS"
              :key="r"
              :class="{ 'cur-col': i === myRoleIndex }"
            >
              {{ ROLE_LABEL[r] }} {{ r }}
            </th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in PERM_MATRIX" :key="row.label">
            <td>{{ row.label }}</td>
            <td
              v-for="(code, i) in row.cells"
              :key="i"
              :class="{ 'cur-col': i === myRoleIndex }"
            >
              <span class="cell-badge" :class="CELL_META[code].cls">{{ CELL_META[code].text }}</span>
            </td>
          </tr>
        </tbody>
      </table>

      <div class="card-bd tight" style="border-top:1px solid var(--border2);font-size:12px;color:var(--text3);line-height:1.8">
        五个角色：<span class="mono">admin 管理员 / reviewer 风控审核员 / strategist 风控策略师 / analyst 数据分析师 / ops 系统运维</span>。
        前端菜单隐藏仅为体验优化，<b>真实鉴权由后端依赖注入强制校验</b>（R-AU-05）——隐藏不等同于授权，越权请求一律 403 并写审计。
      </div>
    </div>
  </StateBlock>
</template>

<style scoped>
.info-row {
  display: grid;
  grid-template-columns: 110px 1fr;
  gap: 14px;
  padding: 11px 0;
  border-bottom: 1px solid var(--border2);
  font-size: 13px;
}
.info-row:last-of-type { border-bottom: none; }
.info-row .k { color: var(--text3); }

.logout-box {
  border: 1px solid var(--danger-border);
  background: var(--danger-bg);
  border-radius: var(--radius);
  padding: 14px 16px;
}

.perm th, .perm td { text-align: center; }
.perm th:first-child, .perm td:first-child { text-align: left; }
.perm .cur-col { background: var(--primary-bg); }
.perm .cell-badge {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 30px;
  padding: 0 8px;
  border-radius: 4px;
  font-size: 12px;
  line-height: 19px;
  border: 1px solid transparent;
}
.perm .cell-badge.tag-green { background: var(--success-bg); color: var(--success-text); border-color: var(--success-border); }
.perm .cell-badge.tag-blue { background: var(--primary-bg); color: #0958d9; border-color: var(--primary-border); }
.perm .cell-badge.tag-orange { background: var(--warning-bg); color: var(--warning-text); border-color: var(--warning-border); }
.perm .cell-badge.tag-gray { background: #fafafa; color: var(--text4); border-color: #e5e7eb; }
</style>
