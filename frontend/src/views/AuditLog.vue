<script setup>
/**
 * 页面十一 /audit —— 审计日志（SPEC 模块 6 · API-6.9 / FR-53）。
 * 表格无删除列、无编辑入口（审计不可篡改）；非管理员操作人输入框禁用（后端强制按本人过滤）。
 */
import { computed, onMounted, reactive, ref } from 'vue'

import { auditApi } from '@/api'
import DiffViewer from '@/components/DiffViewer.vue'
import StateBlock from '@/components/StateBlock.vue'
import { useUserStore } from '@/stores/user'

const store = useUserStore()

/** 业务类型六枚举（v3.0 新增 agent_call 为 AI 工具调用审计） */
const BIZ_TYPES = [
  { value: '', label: '全部类型' },
  { value: 'case_dispose', label: 'case_dispose 案件处置' },
  { value: 'rule_change', label: 'rule_change 规则变更' },
  { value: 'blacklist_change', label: 'blacklist_change 名单变更' },
  { value: 'system_config', label: 'system_config 系统配置' },
  { value: 'auth', label: 'auth 认证' },
  { value: 'agent_call', label: 'agent_call AI 工具调用' },
]

const BIZ_CLS = {
  case_dispose: 'tag-orange',
  rule_change: 'tag-purple',
  blacklist_change: 'tag-green',
  system_config: 'tag-gray',
  auth: 'tag-blue',
  agent_call: 'tag-yellow',
}

const loading = ref(true)
const error = ref('')
const rows = ref([])
const total = ref(0)
const scope = ref('self')
const expanded = ref('')

const filters = reactive({
  biz_type: '', operator: '', start_time: '', end_time: '', page: 1, page_size: 20,
})

const isAdmin = computed(() => store.role === 'admin')
const totalPages = computed(() => Math.max(Math.ceil(total.value / filters.page_size), 1))

/** 分页页码窗口（最多 7 个） */
const pageNumbers = computed(() => {
  const tp = totalPages.value
  const cur = filters.page
  const span = 2
  let start = Math.max(cur - span, 1)
  let end = Math.min(start + 6, tp)
  start = Math.max(end - 6, 1)
  const arr = []
  for (let i = start; i <= end; i += 1) arr.push(i)
  return arr
})

function fmt(iso) {
  return iso ? String(iso).replace('T', ' ').slice(0, 19) : '-'
}

/** 变更摘要：把 after_value 压成一行可读文本 */
function summarize(row) {
  const a = row.after_value
  if (!a || typeof a !== 'object') return row.action
  const parts = []
  if (a.status) parts.push(`状态 ${a.status}`)
  if (a.conclusion) parts.push(`结论 ${a.conclusion}`)
  if (a.score !== undefined) parts.push(`分值 ${a.score}`)
  if (a.pass_threshold !== undefined) parts.push(`pass ${a.pass_threshold}`)
  if (a.reject_threshold !== undefined) parts.push(`reject ${a.reject_threshold}`)
  if (a.entity_value) parts.push(`${a.entity_type || ''} ${a.entity_value}`)
  if (a.success !== undefined) parts.push(`成功 ${a.success} / 失败 ${a.failed}`)
  if (a.result) parts.push(`验证 ${a.result} → ${a.decision || ''}`)
  if (a.tool) parts.push(`调用 ${a.tool} 工具${a.duration_ms ? `（耗时 ${a.duration_ms}ms）` : ''}`)
  if (a.reason) parts.push(a.reason)
  return parts.length ? parts.join(' · ') : row.action
}

function beforeSummary(row) {
  const b = row.before_value
  if (!b || typeof b !== 'object') return '—'
  const parts = []
  if (b.status) parts.push(`状态 ${b.status}`)
  if (b.score !== undefined) parts.push(`分值 ${b.score}`)
  if (b.pass_threshold !== undefined) parts.push(`pass ${b.pass_threshold}`)
  if (b.reject_threshold !== undefined) parts.push(`reject ${b.reject_threshold}`)
  return parts.length ? parts.join(' · ') : '—'
}

async function load() {
  loading.value = true
  try {
    const params = { page: filters.page, page_size: filters.page_size }
    if (filters.biz_type) params.biz_type = filters.biz_type
    if (filters.operator && isAdmin.value) params.operator = filters.operator
    if (filters.start_time) params.start_time = `${filters.start_time}T00:00:00`
    if (filters.end_time) params.end_time = `${filters.end_time}T23:59:59`
    const data = await auditApi.logs(params)
    rows.value = data.items || []
    total.value = data.total || 0
    scope.value = data.scope || 'self'
    error.value = ''
  } catch (err) {
    error.value = err.message || '加载失败'
  } finally {
    loading.value = false
  }
}

function search() {
  filters.page = 1
  load()
}

function reset() {
  Object.assign(filters, { biz_type: '', operator: '', start_time: '', end_time: '', page: 1 })
  load()
}

function goPage(p) {
  if (p < 1 || p > totalPages.value || p === filters.page) return
  filters.page = p
  expanded.value = ''
  load()
}

/** 导出 CSV（UTF-8 BOM，SPEC §0.3 数据输出约定） */
function exportCsv() {
  const header = ['时间', '业务类型', '关联编号', '操作人', '动作', '变更前', '变更后']
  const lines = rows.value.map((r) => [
    fmt(r.created_at), r.biz_type, r.biz_id, r.operator, r.action,
    JSON.stringify(r.before_value ?? ''), JSON.stringify(r.after_value ?? ''),
  ].map((v) => `"${String(v).replace(/"/g, '""')}"`).join(','))
  const csv = `﻿${header.join(',')}\n${lines.join('\n')}`
  const url = URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8' }))
  const a = document.createElement('a')
  a.href = url
  a.download = `ercs-audit-${new Date().toISOString().slice(0, 10)}.csv`
  a.click()
  URL.revokeObjectURL(url)
}

onMounted(load)
</script>

<template>
  <div>
    <div class="alert alert-warn mb-16">
      <div>
        本日志<b>写入后不可修改或删除</b>（数据库触发器禁止 UPDATE / DELETE，应用层不提供改删接口）。
        当前身份为 <b>{{ store.roleLabel }}</b>，可见范围：
        <b>{{ scope === 'all' ? '全量流水' : '仅本人操作记录' }}</b>。
      </div>
    </div>

    <!-- 筛选区 -->
    <div class="card">
      <div class="card-bd tight" style="padding-top:14px;padding-bottom:14px">
        <div class="row-gap-12">
          <div class="field" style="min-width:200px">
            <label>业务类型</label>
            <select v-model="filters.biz_type">
              <option v-for="b in BIZ_TYPES" :key="b.value" :value="b.value">{{ b.label }}</option>
            </select>
          </div>
          <div class="field" style="min-width:150px">
            <label>操作人</label>
            <input
              v-model.trim="filters.operator" type="text"
              :placeholder="isAdmin ? '输入用户名' : '仅本人，忽略此条件'"
              :disabled="!isAdmin"
              @keyup.enter="search"
            >
          </div>
          <div class="field" style="min-width:150px">
            <label>开始时间</label>
            <input v-model="filters.start_time" type="date">
          </div>
          <div class="field" style="min-width:150px">
            <label>结束时间</label>
            <input v-model="filters.end_time" type="date">
          </div>
          <div style="flex:1" />
          <div class="row-gap-8" style="margin-bottom:1px">
            <button class="btn btn-primary" @click="search">查询</button>
            <button class="btn" @click="reset">重置</button>
            <button class="btn" @click="exportCsv">导出 CSV</button>
          </div>
        </div>
      </div>
    </div>

    <!-- 流水表 -->
    <div class="card">
      <div class="card-hd">
        <span>审计流水</span>
        <span class="row-gap-8">
          <span class="tag tag-gray">共 {{ total }} 条</span>
          <span class="muted">仅追加 · append-only</span>
        </span>
      </div>

      <StateBlock :loading="loading" :error="error" :empty="rows.length === 0" empty-text="暂无审计记录">
        <table class="tb">
          <thead>
            <tr>
              <th style="width:110px">时间</th>
              <th style="width:160px">业务类型</th>
              <th style="width:110px">操作人</th>
              <th style="width:92px">动作</th>
              <th style="width:190px">变更前</th>
              <th>变更后</th>
              <th style="width:60px" />
            </tr>
          </thead>
          <tbody>
            <template v-for="r in rows" :key="r.log_id">
              <tr :class="{ sel: expanded === r.log_id }">
                <td class="mono" style="font-size:12px">{{ fmt(r.created_at) }}</td>
                <td><span class="tag" :class="BIZ_CLS[r.biz_type] || 'tag-gray'">{{ r.biz_type }}</span></td>
                <td>{{ r.operator }}</td>
                <td class="mono" style="font-size:12px">{{ r.action }}</td>
                <td class="muted" style="font-size:12.5px">{{ beforeSummary(r) }}</td>
                <td style="font-size:12.5px">{{ summarize(r) }}</td>
                <td>
                  <span class="link" @click="expanded = expanded === r.log_id ? '' : r.log_id">
                    {{ expanded === r.log_id ? '收起' : '展开' }}
                  </span>
                </td>
              </tr>

              <tr v-if="expanded === r.log_id">
                <td colspan="7" style="background:#fbfcfe;padding:16px 18px">
                  <div class="mono" style="font-size:12px;color:var(--text3);margin-bottom:9px">
                    log_id = {{ r.log_id }} · biz_id = {{ r.biz_id }} · created_at = {{ fmt(r.created_at) }}
                  </div>
                  <DiffViewer :before="r.before_value" :after="r.after_value" />
                </td>
              </tr>
            </template>
          </tbody>
        </table>

        <div class="pagination">
          <span class="pg" :class="{ disabled: filters.page <= 1 }" @click="goPage(filters.page - 1)">‹</span>
          <span
            v-for="p in pageNumbers" :key="p" class="pg"
            :class="{ on: p === filters.page }" @click="goPage(p)"
          >{{ p }}</span>
          <span class="pg" :class="{ disabled: filters.page >= totalPages }" @click="goPage(filters.page + 1)">›</span>
          <span class="total">共 {{ total }} 条 · 每页 {{ filters.page_size }} 条</span>
        </div>
      </StateBlock>
    </div>

    <div class="alert alert-info mt-16">
      <div>表格<b>无删除列、无编辑入口</b> —— 审计不可篡改是产品承诺（NFR）。
        业务类型六枚举 <span class="mono">case_dispose / rule_change / blacklist_change / system_config / auth / agent_call</span>，
        其中 <span class="mono">agent_call</span> 为 v3.0 新增的 AI 工具调用审计。
        登录成功 / 登录失败 / 登出 / 越权拒绝四类认证事件全部入账（R-AU-09）；
        <span class="mono">operator</span> 一律取后端登录用户，禁止前端传入（R-AU-08）。</div>
    </div>
  </div>
</template>

<style scoped>
.pagination .pg.disabled { color: var(--text4); cursor: not-allowed; border-color: #eee; }
.pagination .pg.disabled:hover { border-color: #eee; color: var(--text4); }
</style>
