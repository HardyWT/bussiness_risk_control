<script setup>
/**
 * 页面五 /strategy/rules —— 策略规则配置（v3.0）
 *
 * SPEC 模块 3：规则字段 rule_id / rule_category（6 类风险场景）/ event_type（六类事件 + 通用）
 * / rule_condition（JSON 条件树，10 种运算符）/ risk_level（低/中/高/极高）/ risk_score（0~100）
 * / action（通过/标记/人工审核/拒绝）/ challengeable / priority（越大越先匹配）。
 * API-3.1 ~ 3.6、API-2.1（特征字典，供条件编辑器字段下拉，R-FE2-07 不硬编码）。
 */
import { computed, onMounted, reactive, ref } from 'vue'

import { ruleApi } from '@/api'
import ConfirmDialog from '@/components/ConfirmDialog.vue'
import RiskTag from '@/components/RiskTag.vue'
import StateBlock from '@/components/StateBlock.vue'
import { useUserStore } from '@/stores/user'

const store = useUserStore()
/**
 * 可写判定 —— **必须与后端角色依赖逐字对齐**（SPEC R-FE2-20 / PRD §2.1.1）。
 *
 * 后端规则写操作是 `require_role("strategist")`：**admin 对规则只读**。
 * v3.0 这里写成 ['strategist','admin']，导致管理员在页面上看到可点的
 * 「新建规则 / 编辑 / 启停 / 内联改分」，一点就吃到后端 403 ——
 * 「按钮能点但必然失败」是最伤信任的一类破窗（README §8.2 #7 修了后端没修前端）。
 */
const canEdit = computed(() => store.role === 'strategist')

/* ---------------------------------------------------------------- 枚举常量 */
const CATEGORIES = ['订单欺诈', '支付风险', '账户风险', '售后滥用', '地址风险', '物流风险']
const EVENT_TYPES = [
  { value: 'login', label: 'login 登录' },
  { value: 'coupon_receive', label: 'coupon_receive 领券' },
  { value: 'order_create', label: 'order_create 下单' },
  { value: 'order_pay', label: 'order_pay 支付' },
  { value: 'after_sale_apply', label: 'after_sale_apply 售后申请' },
  { value: 'logistics_complaint', label: 'logistics_complaint 物流投诉' },
  { value: '通用', label: '通用（全场景）' },
]
const LEVELS = ['低', '中', '高', '极高']
const LEVEL_KEY = { 低: 'low', 中: 'medium', 高: 'high', 极高: 'critical' }
const ACTIONS = ['通过', '标记', '人工审核', '拒绝']
const ACTION_TAG = { 通过: 'tag-green', 标记: 'tag-yellow', 人工审核: 'tag-orange', 拒绝: 'tag-red' }
/** 10 种运算符（SPEC §3.5） */
const OPERATORS = [
  { value: '>', label: '> 大于' }, { value: '>=', label: '>= 大于等于' },
  { value: '<', label: '< 小于' }, { value: '<=', label: '<= 小于等于' },
  { value: '==', label: '== 等于' }, { value: '!=', label: '!= 不等于' },
  { value: 'in', label: 'in 包含于' }, { value: 'not_in', label: 'not_in 不包含于' },
  { value: 'between', label: 'between 闭区间' },
]
const ARRAY_OPS = ['in', 'not_in', 'between']
const DIM_ORDER = ['用户', '订单', '地址', '团伙']

/* ---------------------------------------------------------------- 列表状态 */
const loading = ref(true)
const error = ref('')
const toast = ref('')
const rules = ref([])
const features = ref([])
const expanded = ref({}) // rule_id → 是否展开 JSON

const filters = reactive({ rule_category: '', event_type: '', status: '' })

const normEvent = (v) => (!v || v === 'all' || v === '通用' ? '通用' : v)

const filtered = computed(() =>
  rules.value.filter((r) =>
    (!filters.rule_category || r.rule_category === filters.rule_category)
    && (!filters.event_type || normEvent(r.event_type) === normEvent(filters.event_type))
    && (!filters.status || (r.is_enabled ? 'enabled' : 'disabled') === filters.status)),
)

const enabledCount = computed(() => filtered.value.filter((r) => r.is_enabled).length)

/** 特征按 4 维度分组，供 optgroup 展示（数据来自 API-2.1，不硬编码） */
const featureGroups = computed(() => {
  const map = {}
  for (const f of features.value) {
    const dim = f.dimension || '其他'
    ;(map[dim] || (map[dim] = [])).push(f)
  }
  const ordered = DIM_ORDER.filter((d) => map[d]).map((d) => ({ dimension: d, items: map[d] }))
  for (const d of Object.keys(map)) if (!DIM_ORDER.includes(d)) ordered.push({ dimension: d, items: map[d] })
  return ordered
})

const condText = (r) => {
  try {
    const c = typeof r.rule_condition === 'string' ? JSON.parse(r.rule_condition) : r.rule_condition
    return JSON.stringify(c)
  } catch {
    return String(r.rule_condition ?? '')
  }
}

/* ---------------------------------------------------------------- 数据加载 */
async function load() {
  loading.value = true
  try {
    const [r, f] = await Promise.all([ruleApi.list(), ruleApi.featureDict()])
    rules.value = r || []
    features.value = f || []
    error.value = ''
  } catch (err) {
    error.value = err.message
  } finally {
    loading.value = false
  }
}

/* ---------------------------------------------------------------- 启停 / 内联编辑 */
async function toggle(rule) {
  try {
    const res = await ruleApi.toggle(rule.rule_id)
    rule.is_enabled = res?.is_enabled ?? (rule.is_enabled ? 0 : 1)
    toast.value = `${rule.rule_id} 已${rule.is_enabled ? '启用' : '停用'}（缓存 TTL ≤ 5s，即时对线上生效）`
  } catch (err) {
    toast.value = `切换失败：${err.message}`
  }
}

/** FR-34 / R-FE2-08：分值内联编辑，失焦保存 */
async function onScoreBlur(rule, event) {
  const value = Number(event.target.value)
  if (!Number.isInteger(value) || value < 0 || value > 100) {
    toast.value = '命中分值必须为 0~100 的整数'
    event.target.value = rule.risk_score
    return
  }
  if (value === rule.risk_score) return
  const before = rule.risk_score
  try {
    await ruleApi.update(rule.rule_id, { risk_score: value })
    rule.risk_score = value
    toast.value = `${rule.rule_id} 分值 ${before} → ${value}`
  } catch (err) {
    event.target.value = before
    toast.value = `保存失败：${err.message}`
  }
}

/** 优先级内联编辑，失焦保存（越大越先匹配） */
async function onPriorityBlur(rule, event) {
  const value = Number(event.target.value)
  if (!Number.isInteger(value) || value < 0) {
    toast.value = '优先级必须为 ≥ 0 的整数'
    event.target.value = rule.priority
    return
  }
  if (value === rule.priority) return
  const before = rule.priority
  try {
    await ruleApi.update(rule.rule_id, { priority: value })
    rule.priority = value
    toast.value = `${rule.rule_id} 优先级 ${before} → ${value}`
  } catch (err) {
    event.target.value = before
    toast.value = `保存失败：${err.message}`
  }
}

/* ---------------------------------------------------------------- 条件树构建器 */
const editorVisible = ref(false)
const editorMode = ref('create')
const saving = ref(false)
const editorError = ref('')

const form = reactive({
  rule_id: '', rule_name: '', event_type: '通用', rule_category: '订单欺诈',
  risk_level: '中', risk_score: 20, priority: 100, action: '标记',
  challengeable: 1, description: '',
})
/** 条件组：[{ logic:'and'|'or', rows:[{ field, op, value }] }] */
const groups = ref([])

const isLeaf = (node) => node && typeof node === 'object' && 'field' in node && 'op' in node

function newRow() {
  return { field: features.value[0]?.key || '', op: '>', value: '' }
}

/** 解析已有 JSON 条件树 → 扁平条件组（支持 and/or 嵌套，深嵌套按语义展平） */
function parseTree(node) {
  if (isLeaf(node)) {
    return { groups: [{ logic: 'and', rows: [{ field: node.field, op: node.op ?? '>', value: node.value ?? '' }] }], top: 'and' }
  }
  const key = node && ('and' in node ? 'and' : ('or' in node ? 'or' : null))
  if (!key) return { groups: [{ logic: 'and', rows: [newRow()] }], top: 'and' }
  const items = Array.isArray(node[key]) ? node[key] : []
  const outGroups = []
  const leafRows = []
  for (const item of items) {
    if (isLeaf(item)) {
      leafRows.push({ field: item.field, op: item.op ?? '>', value: item.value ?? '' })
    } else {
      outGroups.push(...parseTree(item).groups)
    }
  }
  if (leafRows.length) outGroups.unshift({ logic: key, rows: leafRows })
  if (!outGroups.length) outGroups.push({ logic: key, rows: [newRow()] })
  return { groups: outGroups, top: key }
}

function coerceValue(op, raw) {
  if (ARRAY_OPS.includes(op)) {
    return String(raw).split(',').map((s) => s.trim()).filter((s) => s !== '')
      .map((s) => (Number.isNaN(Number(s)) ? s : Number(s)))
  }
  const t = String(raw).trim()
  if (t === '') return null
  const n = Number(t)
  return Number.isNaN(n) ? t : n
}

function buildTree() {
  const built = groups.value
    .map((g) => ({ logic: g.logic, rows: g.rows.filter((r) => r.field && r.value !== '' && r.value !== null) }))
    .filter((g) => g.rows.length)
    .map((g) => ({ [g.logic]: g.rows.map((r) => ({ field: r.field, op: r.op, value: coerceValue(r.op, r.value) })) }))
  if (built.length === 0) return { and: [] }
  if (built.length === 1) return built[0]
  return { and: built }
}

const jsonPreview = computed(() => JSON.stringify(buildTree(), null, 2))

const featureLabel = (key) => features.value.find((f) => f.key === key)?.name || ''

function onOpChange(row) {
  if (!ARRAY_OPS.includes(row.op)) {
    if (Array.isArray(row.value)) row.value = row.value.join(',')
  }
}

function addRow(group) { group.rows.push(newRow()) }
function removeRow(group, idx) { group.rows.splice(idx, 1) }
function addGroup() { groups.value.push({ logic: 'and', rows: [newRow()] }) }
function removeGroup(idx) { groups.value.splice(idx, 1); if (!groups.value.length) addGroup() }

/* ---------------------------------------------------------------- 弹窗 */
function openCreate() {
  editorMode.value = 'create'
  Object.assign(form, {
    rule_id: '', rule_name: '', event_type: '通用', rule_category: '订单欺诈',
    risk_level: '中', risk_score: 20, priority: 100, action: '标记',
    challengeable: 1, description: '',
  })
  groups.value = [{ logic: 'and', rows: [newRow()] }]
  editorError.value = ''
  editorVisible.value = true
}

function openEdit(rule) {
  editorMode.value = 'edit'
  const c = typeof rule.rule_condition === 'string' ? safeParse(rule.rule_condition) : rule.rule_condition
  const parsed = parseTree(c || { and: [] })
  Object.assign(form, {
    rule_id: rule.rule_id ?? rule.rule_code,
    rule_name: rule.rule_name,
    event_type: normEvent(rule.event_type) === '通用' ? '通用' : rule.event_type,
    rule_category: rule.rule_category || '订单欺诈',
    risk_level: rule.risk_level || '中',
    risk_score: rule.risk_score ?? 0,
    priority: rule.priority ?? 0,
    action: rule.action || '标记',
    challengeable: rule.challengeable ?? 1,
    description: rule.description || rule.remark || '',
  })
  groups.value = parsed.groups
  editorError.value = ''
  editorVisible.value = true
}

function safeParse(s) { try { return JSON.parse(s) } catch { return null } }

function validateForm() {
  if (!form.rule_name.trim()) return '请填写规则名称'
  const score = Number(form.risk_score)
  if (!Number.isInteger(score) || score < 0 || score > 100) return '命中分值必须为 0~100 的整数'
  const priority = Number(form.priority)
  if (!Number.isInteger(priority) || priority < 0) return '优先级必须为 ≥ 0 的整数'
  for (const g of groups.value) {
    if (!g.rows.length) return '每个条件组至少包含 1 个条件'
    for (const r of g.rows) {
      if (!r.field) return '存在未选择特征字段的条件'
      if (r.value === '' || r.value === null || r.value === undefined) return `特征 ${r.field} 的取值不能为空`
      if (r.op === 'between') {
        const arr = String(r.value).split(',').map((s) => s.trim()).filter(Boolean)
        if (arr.length !== 2) return 'between 运算符需要 [最小值, 最大值] 两个值'
      }
    }
  }
  return ''
}

async function save() {
  const err = validateForm()
  if (err) { editorError.value = err; return }
  saving.value = true
  editorError.value = ''
  try {
    const payload = {
      rule_name: form.rule_name.trim(),
      event_type: normEvent(form.event_type),
      rule_category: form.rule_category,
      rule_condition: buildTree(),
      risk_level: form.risk_level,
      risk_score: Number(form.risk_score),
      action: form.action,
      challengeable: form.challengeable,
      priority: Number(form.priority),
      description: form.description,
    }
    if (editorMode.value === 'create') {
      const res = await ruleApi.create(payload)
      toast.value = `规则 ${res?.rule_id || res?.rule_code || ''} 已创建并启用`
    } else {
      await ruleApi.update(form.rule_id, payload)
      toast.value = `规则 ${form.rule_id} 已更新`
    }
    editorVisible.value = false
    await load()
  } catch (e) {
    // code 3002 = 条件树格式错误（后端返回具体错误位置，SPEC §3.8）
    editorError.value = e.code === '3002' ? e.message : `保存失败：${e.message}`
  } finally {
    saving.value = false
  }
}

/* ---------------------------------------------------------------- 删除 */
const deleteTarget = ref(null)
const deleting = ref(false)

async function confirmDelete() {
  if (!deleteTarget.value) return
  deleting.value = true
  try {
    const id = deleteTarget.value.rule_id ?? deleteTarget.value.rule_code
    await ruleApi.remove(id)
    toast.value = `规则 ${id} 已删除`
    deleteTarget.value = null
    await load()
  } catch (err) {
    toast.value = `删除失败：${err.message}`
  } finally {
    deleting.value = false
  }
}

const rid = (r) => r.rule_id ?? r.rule_code

onMounted(load)
</script>

<template>
  <div>
    <div v-if="toast" class="alert alert-info mb-16">
      <div>{{ toast }}</div>
    </div>

    <!-- 工具栏 · FR-31 -->
    <div class="card">
      <div class="card-bd tight" style="padding-top:14px;padding-bottom:14px">
        <div class="row-gap-12">
          <div class="field" style="min-width:180px">
            <label>风险场景分类</label>
            <select v-model="filters.rule_category">
              <option value="">全部分类</option>
              <option v-for="c in CATEGORIES" :key="c" :value="c">{{ c }}</option>
            </select>
          </div>
          <div class="field" style="min-width:160px">
            <label>适用事件</label>
            <select v-model="filters.event_type">
              <option value="">全部事件</option>
              <option v-for="e in EVENT_TYPES" :key="e.value" :value="e.value">{{ e.label }}</option>
            </select>
          </div>
          <div class="field" style="min-width:130px">
            <label>状态</label>
            <select v-model="filters.status">
              <option value="">全部</option>
              <option value="enabled">enabled 启用</option>
              <option value="disabled">disabled 停用</option>
            </select>
          </div>
          <div style="flex:1" />
          <button v-if="canEdit" class="btn btn-primary" @click="openCreate">+ 新建规则</button>
        </div>
      </div>
    </div>

    <!-- 规则列表 -->
    <div class="card">
      <div class="card-hd">
        <span>规则列表</span>
        <span class="row-gap-8">
          <span class="tag tag-gray">共 {{ filtered.length }} 条</span>
          <span class="tag tag-green">启用 {{ enabledCount }}</span>
          <span class="tag tag-gray">停用 {{ filtered.length - enabledCount }}</span>
        </span>
      </div>

      <StateBlock :loading="loading" :error="error" :empty="filtered.length === 0" empty-text="暂无规则">
        <table class="tb">
          <thead>
            <tr>
              <th style="width:74px">规则编码</th>
              <th style="width:132px">规则名称</th>
              <th>触发条件（JSON 条件树）</th>
              <th style="width:96px">分类</th>
              <th style="width:60px">等级</th>
              <th class="num" style="width:66px">分值</th>
              <th class="num" style="width:66px">优先级</th>
              <th style="width:78px">可挑战</th>
              <th style="width:60px">启停</th>
              <th style="width:96px">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="r in filtered" :key="rid(r)" :style="!r.is_enabled ? { opacity: .62 } : {}">
              <td class="mono"><b>{{ rid(r) }}</b></td>
              <td>{{ r.rule_name }}</td>
              <td>
                <template v-if="expanded[rid(r)]">
                  <pre class="code" style="margin:0;font-size:11.5px;max-height:160px;overflow:auto">{{ condText(r) }}</pre>
                </template>
                <template v-else>
                  <span class="expr">{{ condText(r).slice(0, 90) }}{{ condText(r).length > 90 ? '…' : '' }}</span>
                </template>
                <div style="margin-top:3px">
                  <span class="link" style="font-size:12px" @click="expanded[rid(r)] = !expanded[rid(r)]">
                    {{ expanded[rid(r)] ? '收起' : '展开 JSON' }}
                  </span>
                </div>
              </td>
              <td>
                <span class="tag tag-orange">{{ r.rule_category }}</span>
                <div class="cell-sub">{{ normEvent(r.event_type) }}</div>
              </td>
              <td><RiskTag kind="level" :value="LEVEL_KEY[r.risk_level] || r.risk_level" /></td>
              <td class="num">
                <input
                  type="number" min="0" max="100"
                  :value="r.risk_score"
                  :disabled="!canEdit"
                  style="width:58px;text-align:right;padding:3px 6px;font-size:12.5px"
                  @blur="onScoreBlur(r, $event)"
                >
              </td>
              <td class="num">
                <input
                  type="number" min="0"
                  :value="r.priority"
                  :disabled="!canEdit"
                  style="width:58px;text-align:right;padding:3px 6px;font-size:12.5px"
                  @blur="onPriorityBlur(r, $event)"
                >
              </td>
              <td>
                <span class="tag" :class="r.challengeable ? 'tag-yellow' : 'tag-red'">
                  {{ r.challengeable ? '可挑战' : '不可挑战' }}
                </span>
              </td>
              <td>
                <span class="switch" :class="{ on: !!r.is_enabled }" @click="canEdit && toggle(r)" />
              </td>
              <td>
                <template v-if="canEdit">
                  <span class="link" @click="openEdit(r)">编辑</span>
                  <span class="link" style="color:var(--danger-text);margin-left:8px" @click="deleteTarget = r">删除</span>
                </template>
                <span v-else class="muted">只读</span>
              </td>
            </tr>
          </tbody>
        </table>
      </StateBlock>

      <div class="card-bd tight" style="border-top:1px solid var(--border2);font-size:12px;color:var(--text3);line-height:1.85">
        触发条件以 <b>JSON 条件树</b>存储，支持
        <span class="mono">&gt; &gt;= &lt; &lt;= == != in not_in between</span> 十种运算符与 <span class="mono">and / or</span> 任意嵌套。
        最终分 <span class="mono">final = min(max(命中分值) + multi_rule_bonus × (命中数 − 1), 100)</span>；命中任一「极高」等级规则触发
        <b>一票否决</b>（最终分 ≥ veto_min_score，默认 90）。分值与优先级为列表内联编辑，失焦即调
        <span class="mono">PUT /rules/{rule_id}</span> 保存（R-FE2-08）。
      </div>
    </div>

    <!-- 规则编辑弹窗 + JSON 条件树构建器 -->
    <div v-if="editorVisible" class="modal-mask" @click.self="editorVisible = false">
      <div class="modal-box" style="width:900px;max-width:94vw">
        <div class="modal-hd">
          <span>{{ editorMode === 'create' ? '新建规则' : `编辑规则 ${form.rule_id}` }}</span>
          <span class="muted" style="font-size:16px;cursor:pointer" @click="editorVisible = false">×</span>
        </div>

        <div class="modal-bd" style="display:grid;grid-template-columns:1fr 1.15fr;gap:20px;max-height:70vh;overflow:auto">
          <!-- 左：规则字段 -->
          <div class="stack-12">
            <div class="field">
              <label>规则名称<span class="req">*</span></label>
              <input v-model.trim="form.rule_name" type="text" placeholder="如：地址团伙退款">
            </div>
            <div class="field">
              <label>适用事件 event_type<span class="req">*</span></label>
              <select v-model="form.event_type">
                <option v-for="e in EVENT_TYPES" :key="e.value" :value="e.value">{{ e.label }}</option>
              </select>
            </div>
            <div class="field">
              <label>风险场景分类 rule_category<span class="req">*</span></label>
              <select v-model="form.rule_category">
                <option v-for="c in CATEGORIES" :key="c" :value="c">{{ c }}</option>
              </select>
            </div>
            <div class="row-gap-12" style="gap:12px">
              <div class="field" style="flex:1">
                <label>风险等级 risk_level<span class="req">*</span></label>
                <select v-model="form.risk_level">
                  <option v-for="l in LEVELS" :key="l" :value="l">{{ l }}</option>
                </select>
              </div>
              <div class="field" style="flex:1">
                <label>命中分值 risk_score<span class="req">*</span></label>
                <input v-model="form.risk_score" type="number" min="0" max="100">
              </div>
            </div>
            <div class="row-gap-12" style="gap:12px">
              <div class="field" style="flex:1">
                <label>优先级 priority<span class="req">*</span></label>
                <input v-model="form.priority" type="number" min="0" placeholder="越大越先匹配">
              </div>
              <div class="field" style="flex:1">
                <label>命中动作 action<span class="req">*</span></label>
                <select v-model="form.action">
                  <option v-for="a in ACTIONS" :key="a" :value="a">{{ a }}</option>
                </select>
              </div>
            </div>
            <div class="field">
              <label>备注 description</label>
              <input v-model.trim="form.description" type="text" placeholder="选填">
            </div>
            <label class="row-gap-8" style="font-size:13px;cursor:pointer">
              <input type="checkbox" :checked="form.challengeable === 1" style="width:15px;height:15px"
                     @change="form.challengeable = $event.target.checked ? 1 : 0">
              可挑战（challengeable = 1）
            </label>
            <div class="alert alert-warn">
              <div>取消勾选即 <span class="mono">challengeable = 0</span>：命中后中风险档（60~79）<b>不进行二次验证</b>，
                直接判 <b>Review</b> 人审。团伙化、高确定性规则（同设备多账号 / 地址团伙退款 / 设备高频下单）建议取消勾选。</div>
            </div>
          </div>

          <!-- 右：JSON 条件树构建器 -->
          <div>
            <div class="sec-title" style="margin-top:0">
              <span>条件编辑器 · JSON 条件树构建器</span>
              <span class="muted">特征字段来自 GET /features/dict</span>
            </div>

            <div v-for="(g, gi) in groups" :key="gi" class="grp">
              <div class="grp-hd">
                <span class="chip">条件组 {{ gi + 1 }}</span>
                <span>组内逻辑</span>
                <select v-model="g.logic" style="width:82px">
                  <option value="and">AND</option>
                  <option value="or">OR</option>
                </select>
                <span style="flex:1" />
                <button class="btn btn-sm" @click="addRow(g)">+ 添加条件</button>
                <button class="btn btn-sm" @click="removeGroup(gi)">删除组</button>
              </div>

              <div v-for="(row, ri) in g.rows" :key="ri" class="cond-row mb-8">
                <select v-model="row.field" style="min-width:220px">
                  <optgroup v-for="grpDim in featureGroups" :key="grpDim.dimension"
                            :label="`${grpDim.dimension}维度（${grpDim.items.length}）`">
                    <option v-for="f in grpDim.items" :key="f.key" :value="f.key">
                      {{ f.key }} {{ f.name }}
                    </option>
                  </optgroup>
                </select>
                <select v-model="row.op" style="min-width:112px" @change="onOpChange(row)">
                  <option v-for="op in OPERATORS" :key="op.value" :value="op.value">{{ op.label }}</option>
                </select>
                <input
                  v-model="row.value" type="text" style="width:150px"
                  :placeholder="ARRAY_OPS.includes(row.op) ? (row.op === 'between' ? '1000,5000' : '1,2,3') : '值'"
                >
                <button class="btn btn-sm" @click="removeRow(g, ri)">删除</button>
              </div>
            </div>

            <button class="btn btn-sm" style="margin-top:9px" @click="addGroup">+ 添加条件组</button>

            <div class="sec-title" style="margin:16px 0 8px">
              <span>JSON 预览</span>
              <span class="muted">提交后端 rule_condition 字段</span>
            </div>
            <pre class="json-preview">{{ jsonPreview }}</pre>

            <div class="muted" style="font-size:11.5px;line-height:1.8;margin-top:8px">
              条件树由后端按白名单运算符安全求值，<b>不使用字符串 DSL、不做 eval</b>；未知字段或越界运算符直接判规则非法（SPEC §3.5）。
              <span class="mono">in / not_in</span> 与 <span class="mono">between</span> 支持数组输入（逗号分隔）。
              当前特征字段共 <b>{{ features.length }}</b> 个（用户 14 / 订单 8 / 地址 3 / 团伙 8）。
            </div>
          </div>
        </div>

        <div class="modal-ft">
          <div v-if="editorError" class="alert alert-danger" style="margin-right:auto;flex:1">
            <div>{{ editorError }}</div>
          </div>
          <button class="btn" @click="editorVisible = false">取消</button>
          <button class="btn btn-primary" :disabled="saving" @click="save">{{ saving ? '保存中…' : '保存' }}</button>
        </div>
      </div>
    </div>

    <!-- 删除二次确认 · FR-35 -->
    <ConfirmDialog
      :visible="!!deleteTarget"
      title="删除规则"
      :actions="deleteTarget ? [
        `删除规则 ${rid(deleteTarget)} ${deleteTarget.rule_name}`,
        '该规则将立即停止参与线上决策（缓存 TTL ≤ 5s）',
        '删除操作将写入审计日志（含变更前快照）',
      ] : []"
      warning="删除后该规则的历史命中记录仍保留，但规则本身不可恢复。"
      confirm-text="确认删除"
      :loading="deleting"
      @cancel="deleteTarget = null"
      @confirm="confirmDelete"
    />
  </div>
</template>

<style scoped>
.cond-row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.cond-row select, .cond-row input { width: auto; }
.expr {
  font-family: var(--mono); font-size: 11.5px; color: var(--text2);
  background: #fafbfc; border: 1px solid var(--border); border-radius: 4px; padding: 3px 7px;
  display: inline-block; line-height: 1.55; word-break: break-all;
}
.grp { border: 1px solid var(--border); border-radius: 6px; padding: 12px; background: #fafbfc; }
.grp + .grp { margin-top: 9px; }
.grp-hd { display: flex; align-items: center; gap: 9px; margin-bottom: 10px; font-size: 12.5px; color: var(--text2); }
.json-preview {
  font-family: var(--mono); font-size: 12px; line-height: 1.8; background: #fafbfc;
  border: 1px solid var(--border); border-radius: 6px; padding: 12px 14px;
  color: var(--text2); white-space: pre-wrap; word-break: break-all; margin: 0;
}
.chip {
  display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 11.5px;
  background: var(--primary-bg); color: var(--primary); border: 1px solid var(--primary-border);
}
</style>
