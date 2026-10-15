<script setup>
/**
 * 页面三 /workbench —— 审核工作台（案件列表）
 *
 * PRD §7.3 / FR-23 / R-FE2-05 / SPEC §4.6（API-4.1、4.2）+ §8.1。
 * v3.0：案件统计 6 卡（总数 + 五态）、风险等级四档、案件分类 6 类、案件五态。
 * admin 只读（PRD §2.1.1）。
 */
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'

import { caseApi } from '@/api'
import RiskTag from '@/components/RiskTag.vue'
import StateBlock from '@/components/StateBlock.vue'
import { useUserStore } from '@/stores/user'

const router = useRouter()
const store = useUserStore()

const loading = ref(true)
const error = ref('')
const rows = ref([])
const total = ref(0)
const rawStats = ref({})
const categoryStats = ref({})        // 后端返回的全量案件分类分布

const filters = reactive({
  risk_level: '',
  case_category: '',
  case_status: 'pending',
  start_time: '',
  page: 1,
  page_size: 20,
})

/** 四档风险等级（SPEC §8.2 R-FE2-19：深红=极高 / 橙=高 / 黄=中 / 绿=低） */
const RISK_LEVELS = [
  { value: 'critical', label: '极高 critical', color: '#cf1322' },
  { value: 'high', label: '高 high', color: 'var(--warning)' },
  { value: 'medium', label: '中 medium', color: 'var(--challenge)' },
  { value: 'low', label: '低 low', color: 'var(--success)' },
]

/** 案件六分类（SPEC §8.2 固定口径）——始终展示全部 6 类，不随数据分布增减 */
const CATEGORY_FALLBACK = ['订单欺诈', '支付风险', '账户风险', '售后滥用', '地址风险', '物流风险']
const CATEGORIES = computed(() => {
  // 后端若出现清单外的新分类，追加到末尾，避免遗漏
  const extra = Object.keys(categoryStats.value || {}).filter((c) => !CATEGORY_FALLBACK.includes(c))
  return [...CATEGORY_FALLBACK, ...extra]
})

/** 案件五态 */
const STATUSES = [
  { value: 'pending', label: '待审核 pending' },
  { value: 'reviewing', label: '审核中 reviewing' },
  { value: 'disposed', label: '已处置 disposed' },
  { value: 'closed', label: '已关闭 closed' },
  { value: 'archived', label: '已归档 archived' },
]

const LEVEL_META = {
  critical: { label: '极高', color: '#cf1322' },
  high: { label: '高', color: 'var(--warning)' },
  medium: { label: '中', color: 'var(--challenge)' },
  low: { label: '低', color: 'var(--success)' },
}

const isAdmin = computed(() => store.role === 'admin')
const pageCount = computed(() => Math.max(Math.ceil(total.value / filters.page_size), 1))

/** 顶部 6 张统计卡 */
const statCards = computed(() => {
  const s = rawStats.value || {}
  const pick = (...keys) => {
    for (const k of keys) {
      const v = s[k]
      if (v !== undefined && v !== null) return Number(v) || 0
    }
    return 0
  }
  const st = {
    pending: pick('待审核', 'pending'),
    reviewing: pick('审核中', 'reviewing'),
    disposed: pick('已处置', 'disposed'),
    closed: pick('已关闭', 'closed'),
    archived: pick('已归档', 'archived'),
  }
  return [
    // 案件总数取**全量**口径，与其余五张状态卡一致（列表 total 为筛选后数量）
    { lb: '案件总数', val: pick('total', '总数') || total.value, sub: '全部状态', color: '' },
    { lb: '待审核', val: st.pending, sub: 'pending', color: 'var(--warning-text)' },
    { lb: '审核中', val: st.reviewing, sub: 'reviewing', color: '#0958d9' },
    { lb: '已处置', val: st.disposed, sub: 'disposed', color: 'var(--success-text)' },
    { lb: '已关闭', val: st.closed, sub: 'closed', color: 'var(--text3)' },
    { lb: '已归档', val: st.archived, sub: 'archived', color: 'var(--text3)' },
  ]
})

/** 归一化：接口可能返回中文或英文状态/等级 */
function normStatus(v) {
  const map = {
    pending: 'pending', 待审核: 'pending',
    reviewing: 'reviewing', 审核中: 'reviewing',
    disposed: 'disposed', 已处置: 'disposed',
    closed: 'closed', 已关闭: 'closed',
    archived: 'archived', 已归档: 'archived',
  }
  return map[v] || 'pending'
}

function normLevel(v) {
  const map = { critical: 'critical', 极高: 'critical', high: 'high', 高: 'high', medium: 'medium', 中: 'medium', low: 'low', 低: 'low' }
  return map[v] || 'low'
}

function levelMeta(v) {
  return LEVEL_META[normLevel(v)] || LEVEL_META.low
}

function fmt(iso) {
  if (!iso) return '-'
  return String(iso).replace('T', ' ').slice(0, 16)
}

async function load() {
  loading.value = true
  try {
    const params = Object.fromEntries(
      Object.entries(filters).filter(([, v]) => v !== '' && v !== null),
    )
    const data = await caseApi.list(params)
    rows.value = data.items || []
    total.value = data.total || 0
    rawStats.value = data.statistics || {}
    categoryStats.value = data.by_category || {}
    error.value = ''
  } catch (err) {
    error.value = err.message
  } finally {
    loading.value = false
  }
}

function search() {
  filters.page = 1 // R-FE2-05：筛选条件变化后重置到第 1 页
  load()
}

function reset() {
  Object.assign(filters, {
    risk_level: '', case_category: '', case_status: 'pending',
    start_time: '', page: 1,
  })
  load()
}

function goPage(p) {
  if (p < 1 || p > pageCount.value) return
  filters.page = p
  load()
}

function openCase(row) {
  router.push(`/workbench/case/${row.case_id}`)
}

onMounted(load)
</script>

<template>
  <div>
    <!-- admin 只读提示（PRD §2.1.1 / R-FE2-14） -->
    <div v-if="isAdmin" class="alert alert-info mb-16">
      <div>当前身份为<b>系统管理员</b>：可查看全部案件，但按 PRD §2.1.1 <b>不可执行处置</b>，
        案件详情页的处置按钮将置灰禁用（而非隐藏）。</div>
    </div>

    <!-- ① 案件统计卡片行（总数 + 五态）· FR-23 -->
    <div class="grid" style="grid-template-columns:repeat(6,1fr)">
      <div v-for="c in statCards" :key="c.lb" class="metric">
        <div class="lb">{{ c.lb }}</div>
        <div class="val" :style="c.color ? { color: c.color } : {}">{{ c.val }}</div>
        <div class="sub">{{ c.sub }}</div>
      </div>
    </div>

    <div class="grid" style="grid-template-columns:200px 1fr; align-items:start">

      <!-- ② 筛选区 -->
      <div class="card" style="margin-bottom:0">
        <div class="card-hd" style="padding:12px 16px"><span>筛选</span></div>
        <div class="card-bd" style="padding:16px;display:flex;flex-direction:column;gap:14px">
          <div class="field">
            <label>风险等级</label>
            <select v-model="filters.risk_level">
              <option value="">全部</option>
              <option v-for="l in RISK_LEVELS" :key="l.value" :value="l.value">{{ l.label }}</option>
            </select>
          </div>
          <div class="field">
            <label>案件分类</label>
            <select v-model="filters.case_category">
              <option value="">全部</option>
              <option v-for="c in CATEGORIES" :key="c" :value="c">
                {{ c }}{{ categoryStats[c] ? `（${categoryStats[c]}）` : '' }}
              </option>
            </select>
          </div>
          <div class="field">
            <label>审核状态</label>
            <select v-model="filters.case_status">
              <option value="">全部</option>
              <option v-for="s in STATUSES" :key="s.value" :value="s.value">{{ s.label }}</option>
            </select>
          </div>
          <div class="field">
            <label>触发时间（起）</label>
            <input v-model="filters.start_time" type="datetime-local">
          </div>
          <div class="row" style="gap:8px;margin-top:2px">
            <button class="btn btn-primary" style="flex:1;justify-content:center" @click="search">查询</button>
            <button class="btn" style="flex:1;justify-content:center" @click="reset">重置</button>
          </div>
        </div>
      </div>

      <!-- ③ 案件列表 -->
      <div class="card" style="margin-bottom:0">
        <div class="card-hd">
          <span>案件列表</span>
          <span class="row" style="gap:8px">
            <span class="tag tag-orange">待审核 {{ statCards[1].val }}</span>
            <span class="tag tag-gray">共 {{ total }} 案件</span>
          </span>
        </div>

        <StateBlock
          :loading="loading" :error="error" :empty="rows.length === 0"
          empty-text="暂无符合条件的案件"
        >
          <table class="tb">
            <thead>
              <tr>
                <th style="width:110px">案件号</th>
                <th style="width:92px">风险等级</th>
                <th style="width:150px">案件分类</th>
                <th style="width:80px">风险分</th>
                <th style="width:150px">触发时间</th>
                <th style="width:110px">状态</th>
                <th style="width:80px">操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in rows" :key="row.case_id">
                <td>
                  <div class="cell-main">{{ row.case_id }}</div>
                  <div class="cell-sub">{{ row.user_id }}</div>
                </td>
                <td>
                  <span style="display:inline-flex;align-items:center;gap:4px">
                    <span class="dot" :style="{ marginRight: 0, background: levelMeta(row.risk_level).color }" />
                    {{ levelMeta(row.risk_level).label }}
                  </span>
                </td>
                <td>
                  {{ row.case_category || '-' }}
                  <span
                    v-if="row.top_rule_id || row.rule_id"
                    style="color:var(--text3);font-size:12px"
                  >{{ row.top_rule_id || row.rule_id }}</span>
                </td>
                <td class="mono">{{ row.final_score }}</td>
                <td class="mono" style="font-size:12px">{{ fmt(row.create_time) }}</td>
                <td><RiskTag kind="status" :value="normStatus(row.case_status)" /></td>
                <td>
                  <span class="link" @click="openCase(row)">
                    {{ normStatus(row.case_status) === 'pending' ? '处理' : '查看' }}
                  </span>
                </td>
              </tr>
            </tbody>
          </table>
        </StateBlock>

        <div v-if="!loading && !error && rows.length" class="pagination">
          <span class="pg" @click="goPage(filters.page - 1)">‹</span>
          <span
            v-for="p in Math.min(pageCount, 7)" :key="p"
            class="pg" :class="{ on: p === filters.page }" @click="goPage(p)"
          >{{ p }}</span>
          <span class="pg" @click="goPage(filters.page + 1)">›</span>
          <span class="total">共 {{ total }} 条 · 每页 {{ filters.page_size }} 条</span>
        </div>
      </div>
    </div>
  </div>
</template>
