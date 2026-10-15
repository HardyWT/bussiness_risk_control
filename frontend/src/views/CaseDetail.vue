<script setup>
/**
 * 页面四 /workbench/case/:id —— 案件详情
 *
 * PRD §7.4 / FR-22~30 / SPEC §4.4~4.7（API-4.3 ~ 4.6）+ §5 + §8.1。
 * v3.0：五档决策 / 四档等级 / 一票否决 / 33 特征 4 维度 / 6 分类 / 案件五态。
 * 装配 ProfileCard + FeatureTable + EntityGraph + DecisionCard + DisposePanel + ConfirmDialog。
 */
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { caseApi } from '@/api'
import ConfirmDialog from '@/components/ConfirmDialog.vue'
import EntityGraph from '@/components/EntityGraph.vue'
import FeatureTable from '@/components/FeatureTable.vue'
import RiskTag from '@/components/RiskTag.vue'
import StateBlock from '@/components/StateBlock.vue'
import { useUserStore } from '@/stores/user'

const route = useRoute()
const router = useRouter()
const store = useUserStore()

const loading = ref(true)
const error = ref('')
const detail = ref(null)
const submitting = ref(false)
const toast = ref('')

const conclusion = ref('reject')
const remark = ref('')
const confirmVisible = ref(false)
/** dispose | hold | close */
const confirmMode = ref('dispose')

/* ------------------------------------------------------------------ 常量 */

const LEVEL_META = {
  critical: { label: '极高', color: 'var(--danger-text)' },
  high: { label: '高', color: 'var(--warning-text)' },
  medium: { label: '中', color: 'var(--challenge-text)' },
  low: { label: '低', color: 'var(--success-text)' },
}

const STATUS_LABEL = {
  pending: '待审核', reviewing: '审核中', disposed: '已处置', closed: '已关闭', archived: '已归档',
}

const DECISION_META = {
  Pass: { label: 'Pass 放行', color: 'var(--success-text)' },
  Mark: { label: 'Mark 标记观察', color: 'var(--challenge-text)' },
  Challenge: { label: 'Challenge 挑战', color: 'var(--challenge-text)' },
  Review: { label: 'Review 人审', color: 'var(--warning-text)' },
  Reject: { label: 'Reject 拦截', color: 'var(--danger-text)' },
}

/** 处置结论 → 将执行的联动动作（SPEC §4.5） */
const ACTION_MAP = {
  pass: [],
  reject: ['block_order'],
  blacklist: ['block_order', 'blacklist_user', 'ban_device'],
}

const ACTION_NOTE = {
  pass: 'pass：案件标记误报，不执行联动动作，仅记录审计',
  reject: 'reject：执行 block_order，联动订单置为「已取消」',
  blacklist: 'blacklist：执行 block_order + blacklist_user + ban_device（SPEC §4.5）',
}

const CONCLUSIONS = [
  { v: 'pass', t: 'release — 证据不足，放行' },
  { v: 'reject', t: 'reject — 拦截订单' },
  { v: 'blacklist', t: 'blacklist — 违规拦截并拉黑用户' },
]

/* ------------------------------------------------------------------ 归一化 */

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

/** 五档决策：<30 Pass / 30~59 Mark / 60~79 Review / >=80 Reject（一票否决必然落到 Reject） */
function decide(score, hasCritical) {
  if (hasCritical || score >= 80) return 'Reject'
  if (score < 30) return 'Pass'
  if (score < 60) return 'Mark'
  return 'Review'
}

/* ------------------------------------------------------------------ 数据 */

const caseId = computed(() => detail.value?.case_id || route.params.id)
const status = computed(() => normStatus(detail.value?.case_status))
const statusLabel = computed(() => STATUS_LABEL[status.value] || status.value)
const profile = computed(() => detail.value?.profile || {})
const order = computed(() => detail.value?.order || null)
const graph = computed(() => detail.value?.graph || {})
const rules = computed(() => detail.value?.triggered_rules || detail.value?.rule_hits || [])

const score = computed(() => Number(detail.value?.final_score ?? 0))
const level = computed(() => normLevel(detail.value?.risk_level))
const levelMeta = computed(() => LEVEL_META[level.value] || LEVEL_META.low)

function ruleLevel(r) {
  return normLevel(r?.risk_level || r?.level)
}

const isVeto = computed(() => {
  if (detail.value?.is_veto !== undefined && detail.value?.is_veto !== null) return Boolean(detail.value.is_veto)
  return rules.value.some((r) => ruleLevel(r) === 'critical')
})

const decision = computed(() =>
  detail.value?.decision || decide(score.value, isVeto.value),
)
const decisionMeta = computed(() => DECISION_META[decision.value] || { label: decision.value, color: 'var(--text2)' })

const userId = computed(() => profile.value.user_id || detail.value?.user_id || '-')
const deviceId = computed(() => {
  const n = (graph.value.nodes || []).find((x) => x.type === 'device')
  return n?.label || detail.value?.device_id || ''
})

/* ------------------------------------------------------------------ 评分计算过程 */

const scoreValues = computed(() => rules.value.map((r) => Number(r.risk_score ?? r.score ?? 0)))

const maxCalc = computed(() => {
  const s = scoreValues.value
  if (!s.length) return ''
  return `max(${s.join(', ')}) + 3×(${s.length} − 1)`
})

const scoreCalc = computed(() => {
  const s = scoreValues.value
  const n = s.length
  if (!n) {
    return '本次案件无规则命中（可能由名单短路或挑战升级建案），最终风险分 0，决策 Pass 放行。'
  }
  const maxScore = Math.max(...s)
  const bonus = 3 * (n - 1)
  const final = Math.min(maxScore + bonus, 100)
  const lines = []
  lines.push('评分算法：final = min( max(命中分值) + 3 × (命中数 − 1), 100 )')
  lines.push(`= min( max(${s.join(', ')}) + 3 × (${n} − 1), 100 ) = min( ${maxScore} + ${bonus}, 100 ) = ${final}`)
  if (isVeto.value) {
    const top = rules.value.find((r) => ruleLevel(r) === 'critical') || {}
    lines.push(`一票否决：${top.rule_id || top.rule_code || '—'} 风险等级为「极高」→ 触发 veto，最终分不低于 veto_min_score = 90`)
  }
  lines.push(`五档决策：final=${score.value} ${arbitrateNote(score.value)} → ${decisionMeta.value.label}`)
  lines.push('仲裁阈值来源：rule_scene（pass=30 / mark=60 / reject=80），可配置')
  return lines.join('\n')
})

function arbitrateNote(v) {
  if (v < 30) return '＜ pass_threshold(30)'
  if (v < 60) return '∈ [30, 59]'
  if (v >= 80) return '≥ reject_threshold(80)'
  return '∈ [60, 79] 高风险档（命中不可挑战规则 → 直接人审）'
}

/* ------------------------------------------------------------------ 权限 */

const canDispose = computed(() => store.role === 'reviewer')
const isFinal = computed(() => ['disposed', 'closed', 'archived'].includes(status.value))
const disposalDisabled = computed(() => !canDispose.value || isFinal.value)

const disposeReason = computed(() => {
  if (!canDispose.value) return `当前角色（${store.roleLabel || store.role}）无案件处置权限`
  if (isFinal.value) return `案件已${statusLabel.value}，不可重复处置（SPEC code 2003）`
  return ''
})

const holdDisabled = computed(() => disposalDisabled.value || !['pending', 'reviewing'].includes(status.value))
const holdReason = computed(() => disposeReason.value || '仅「待审核 / 审核中」案件可暂存挂起')

const closeDisabled = computed(() => disposalDisabled.value || status.value !== 'pending')
const closeReason = computed(() => disposeReason.value || '仅「待审核」案件可关闭为误报')

/* ------------------------------------------------------------------ 联动效果预览 */

function fmtAmount(v) {
  const n = Number(v)
  if (Number.isNaN(n)) return '-'
  return `¥${n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

const previewRows = computed(() => {
  if (conclusion.value === 'pass') return []
  const rows = []
  const o = order.value
  if (o) {
    rows.push({
      obj: `订单 ${o.order_id || '-'}`, sub: fmtAmount(o.total_amount),
      from: o.order_status || '未支付', to: '已取消 cancelled',
    })
  }
  if (conclusion.value === 'blacklist') {
    rows.push({ obj: `账号 ${userId.value}`, sub: '', from: '不在名单', to: '写入黑名单（entity_type=user）' })
    if (deviceId.value) {
      rows.push({ obj: `设备 ${deviceId.value}`, sub: '', from: '不在名单', to: '写入黑名单（entity_type=device）' })
    }
  }
  return rows
})

/* ------------------------------------------------------------------ 二次确认动作清单（含订单号与金额） */

const dialogActions = computed(() => {
  if (confirmMode.value === 'hold') {
    return [
      `暂存挂起案件 ${caseId.value}，状态回退为 待审核`,
      remark.value ? `备注：${remark.value}` : '备注：（未填写）',
    ]
  }
  if (confirmMode.value === 'close') {
    return [
      `关闭案件 ${caseId.value} 为误报（状态 → 已关闭，不执行任何处置动作）`,
      '本次操作写入不可篡改审计日志',
    ]
  }
  const list = []
  if (conclusion.value === 'pass') {
    list.push(`放行案件 ${caseId.value}（证据不足，标记误报），不执行联动动作`)
  } else {
    const o = order.value
    if (o) list.push(`拦截订单 ${o.order_id}（${fmtAmount(o.total_amount)}），订单状态置为 已取消 cancelled`)
    if (conclusion.value === 'blacklist') {
      list.push(`将账号 ${userId.value} 加入黑名单（entity_type = user）`)
      if (deviceId.value) list.push(`将设备 ${deviceId.value} 加入黑名单（entity_type = device）`)
    }
  }
  list.push('本次处置将写入不可篡改审计日志（含变更前 / 变更后快照）')
  return list
})

const dialogTitle = computed(() => ({
  dispose: '确认执行处置', hold: '确认暂存挂起', close: '确认关闭为误报',
}[confirmMode.value] || '确认执行'))

/* ------------------------------------------------------------------ 交互 */

function fmt(iso) {
  if (!iso) return '-'
  return String(iso).replace('T', ' ').slice(0, 19)
}

async function load() {
  loading.value = true
  try {
    detail.value = await caseApi.detail(route.params.id)
    remark.value = detail.value?.remark || ''
    if (detail.value?.conclusion && ['pass', 'reject', 'blacklist'].includes(detail.value.conclusion)) {
      conclusion.value = detail.value.conclusion
    }
    error.value = ''
  } catch (err) {
    error.value = err.message
  } finally {
    loading.value = false
  }
}

function openConfirm(mode) {
  if (disposalDisabled.value) return
  if (mode === 'hold' && holdDisabled.value) return
  if (mode === 'close' && closeDisabled.value) return
  if (mode === 'dispose' && !remark.value.trim()) {
    toast.value = '请填写处置原因'
    return
  }
  confirmMode.value = mode
  confirmVisible.value = true
}

async function onConfirm() {
  submitting.value = true
  try {
    if (confirmMode.value === 'hold') {
      await caseApi.hold(caseId.value, { remark: remark.value })
      toast.value = '已暂存挂起，案件状态回退为「待审核」'
    } else if (confirmMode.value === 'close') {
      await caseApi.close(caseId.value, { remark: remark.value })
      toast.value = '已关闭为误报'
    } else {
      const res = await caseApi.dispose(caseId.value, {
        conclusion: conclusion.value,
        remark: remark.value,
        actions: ACTION_MAP[conclusion.value],
      })
      const disposals = res?.disposals || []
      const failed = disposals.filter((d) => d.result === 'failed')
      const okCount = disposals.filter((d) => d.result === 'success').length
      toast.value = failed.length
        ? `处置已提交，但 ${failed.length} 个动作失败：${failed.map((f) => f.action_type).join('、')}`
        : `处置成功，已执行 ${okCount} 个联动动作`
    }
    confirmVisible.value = false
    await load()
  } catch (err) {
    toast.value = `提交失败：${err.message}`
  } finally {
    submitting.value = false
  }
}

function cancel() {
  remark.value = detail.value?.remark || ''
  if (['pass', 'reject', 'blacklist'].includes(detail.value?.conclusion)) {
    conclusion.value = detail.value.conclusion
  }
  toast.value = ''
}

onMounted(load)
</script>

<template>
  <StateBlock
    :loading="loading" :error="error" :empty="!detail"
    loading-text="加载案件详情…" empty-text="案件不存在或已被移除"
  >
    <template v-if="detail">

      <!-- ============ 案件头 ============ -->
      <div class="row-between mb-16">
        <div class="row-gap-12">
          <h2 style="font-size:17px;font-weight:600">案件 {{ caseId }}</h2>
          <RiskTag kind="level" :value="level" />
          <RiskTag kind="status" :value="status" />
        </div>
        <span class="link" @click="router.push('/workbench')">← 返回列表</span>
      </div>

      <div v-if="toast" class="alert alert-info mb-16">
        <div>{{ toast }}</div>
      </div>

      <!-- ============ ① 三卡总览 · FR-24 ============ -->
      <div class="grid g3">

        <!-- 用户画像卡 -->
        <div class="card" style="margin-bottom:0">
          <div class="card-hd">
            <span>用户画像</span>
            <span class="muted">/profile/{{ userId }}</span>
          </div>
          <div class="card-bd">
            <div class="profile-hd">
              <div
                class="big-avatar"
                :style="{ background: 'var(--danger-bg)', color: levelMeta.color, borderColor: 'var(--danger-border)' }"
              >{{ profile.risk_score ?? '-' }}</div>
              <div style="min-width:0">
                <div style="font-weight:600;font-size:15px">{{ userId }}</div>
                <div class="muted" style="margin-top:3px">
                  累计画像评分 {{ profile.risk_score ?? '-' }}
                  <template v-if="profile.risk_level"> · 等级 {{ LEVEL_META[normLevel(profile.risk_level)].label }}</template>
                </div>
                <div class="row" style="gap:6px;margin-top:8px;flex-wrap:wrap">
                  <span v-for="t in (profile.risk_tags || [])" :key="t" class="tag tag-red">{{ t }}</span>
                  <span v-if="!profile.risk_tags?.length" class="tag tag-gray">暂无风险标签</span>
                </div>
              </div>
            </div>
            <div class="divider"></div>
            <div class="kv">
              <span class="k">同设备关联账号数(24h)</span>
              <span class="v" style="color:var(--danger-text)">
                {{ detail.features?.device_account_cnt_24h ?? profile.related_account_cnt ?? '-' }}
              </span>
            </div>
            <div class="kv"><span class="k">历史订单总数</span><span class="v">{{ profile.total_orders ?? '-' }}</span></div>
            <div class="kv"><span class="k">退款总次数</span><span class="v">{{ profile.total_refunds ?? '-' }}</span></div>
            <div class="kv">
              <span class="k">退款率</span>
              <span class="v" style="color:var(--danger-text)">{{ profile.refund_rate ?? '-' }}</span>
            </div>
            <div class="kv"><span class="k">投诉次数</span><span class="v">{{ profile.complaint_count ?? '-' }}</span></div>
            <div class="kv"><span class="k">收货地址数量</span><span class="v">{{ profile.address_count ?? '-' }}</span></div>
          </div>
        </div>

        <!-- 当前订单卡 -->
        <div class="card" style="margin-bottom:0">
          <div class="card-hd"><span>当前订单</span><span class="muted">biz_no</span></div>
          <div class="card-bd">
            <template v-if="order">
              <div class="kv"><span class="k">订单号</span><span class="v mono">{{ order.order_id || '-' }}</span></div>
              <div class="kv">
                <span class="k">订单金额</span>
                <span class="v" style="font-size:16px">{{ fmtAmount(order.total_amount) }}</span>
              </div>
              <div class="kv"><span class="k">商品</span><span class="v">{{ order.item_name || order.item || '-' }}</span></div>
              <div class="kv" v-if="deviceId"><span class="k">设备指纹</span><span class="v mono" style="font-size:12px">{{ deviceId }}</span></div>
              <div class="divider" style="margin:10px 0"></div>
              <div class="kv">
                <span class="k">订单状态</span>
                <span class="v"><span class="tag tag-gray">{{ order.order_status || '-' }}</span></span>
              </div>
            </template>
            <div v-else class="muted">该案件无关联订单</div>
          </div>
        </div>

        <!-- 决策摘要卡 -->
        <div class="card" style="margin-bottom:0">
          <div class="card-hd"><span>决策摘要</span><span class="muted">五档仲裁</span></div>
          <div class="card-bd">
            <div class="row" style="gap:14px;align-items:baseline;margin-bottom:12px">
              <div style="font-size:34px;font-weight:600;line-height:1" :style="{ color: levelMeta.color }">{{ score }}</div>
              <div>
                <div><RiskTag kind="level" :value="level" /></div>
                <div class="muted" style="margin-top:5px">
                  决策：<b :style="{ color: decisionMeta.color }">{{ decisionMeta.label }}</b>
                </div>
                <div style="margin-top:6px">
                  <span class="tag" :class="isVeto ? 'tag-red' : 'tag-gray'">
                    一票否决：{{ isVeto ? '已触发' : '未触发' }}
                  </span>
                </div>
              </div>
            </div>
            <div class="kv"><span class="k">案件分类</span><span class="v">{{ detail.case_category || '-' }}</span></div>
            <div class="kv"><span class="k">命中规则数</span><span class="v">{{ rules.length }} 条</span></div>
            <div class="kv"><span class="k">案件编号</span><span class="v mono" style="font-size:12.5px">{{ caseId }}</span></div>
            <div class="kv" v-if="detail.disposed_at"><span class="k">处置决策时间</span><span class="v mono" style="font-size:12.5px">{{ fmt(detail.disposed_at) }}</span></div>
            <div class="divider" style="margin:10px 0"></div>
            <div class="muted" style="margin-bottom:6px">评分计算过程</div>
            <div class="code" style="font-size:11.5px">{{ scoreCalc }}</div>
          </div>
        </div>
      </div>

      <!-- ============ ② 命中明细 + 特征快照 · FR-25 ============ -->
      <div class="grid g2">

        <div class="card" style="margin-bottom:0">
          <div class="card-hd"><span>命中规则明细</span><span class="muted">五档累加 · 一票否决 · 封顶 100</span></div>
          <table class="tb">
            <thead>
              <tr>
                <th style="width:74px">规则编码</th>
                <th style="width:120px">名称</th>
                <th style="width:86px">分类</th>
                <th style="width:58px">等级</th>
                <th>触发原因</th>
                <th class="num" style="width:58px">分值</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(h, i) in rules" :key="h.rule_id || h.rule_code || i">
                <td class="mono" style="font-weight:600">{{ h.rule_id || h.rule_code }}</td>
                <td>{{ h.rule_name }}</td>
                <td class="muted" style="font-size:12px">{{ h.rule_category || '-' }}</td>
                <td>
                  <span :style="{ color: LEVEL_META[ruleLevel(h)].color, fontWeight: 600, fontSize: '12.5px' }">
                    {{ LEVEL_META[ruleLevel(h)].label }}
                  </span>
                </td>
                <td class="mono" style="font-size:12px;color:var(--text2)">{{ h.trigger_reason || h.reason || '-' }}</td>
                <td class="num" :style="{ color: LEVEL_META[ruleLevel(h)].color, fontWeight: 600 }">
                  +{{ h.risk_score ?? h.score ?? 0 }}
                </td>
              </tr>
              <tr v-if="!rules.length">
                <td colspan="6" class="muted" style="text-align:center;padding:18px">
                  无规则命中（可能由名单短路或挑战升级建案）
                </td>
              </tr>
              <tr v-else style="background:#fafbfc">
                <td colspan="5" style="font-weight:600">
                  最终风险分
                  <span style="color:var(--text3);font-size:12px;font-weight:400">
                    · {{ maxCalc }} = {{ score }}<template v-if="isVeto">，且 ≥ veto_min_score(90)</template> → {{ decisionMeta.label }}
                  </span>
                </td>
                <td class="num" style="font-weight:600;font-size:14px">{{ score }}</td>
              </tr>
            </tbody>
          </table>
          <div class="card-bd tight" style="border-top:1px solid var(--border2)">
            <div class="code" style="border:none;background:transparent;padding:0">{{ scoreCalc }}</div>
          </div>
        </div>

        <div class="card" style="margin-bottom:0">
          <div class="card-hd">
            <span>实时特征快照对比</span>
            <span class="muted">33 个特征 · 4 维度 · 当前值 / 历史基线 · ● 命中</span>
          </div>
          <div class="card-bd">
            <FeatureTable
              :features="detail.features || {}"
              :baseline="detail.feature_baseline || {}"
              :rule-hits="rules"
            />
          </div>
        </div>
      </div>

      <!-- ============ ③ 关联实体图谱 · FR-26 ============ -->
      <div class="card">
        <div class="card-hd">
          <span>关联实体图谱</span>
          <span class="muted">/graph/{{ userId }} · SVG 径向布局 · 节点可点击下钻</span>
        </div>
        <div class="card-bd">
          <EntityGraph
            :nodes="graph.nodes || []"
            :edges="graph.edges || []"
            :center-id="graph.center || ''"
          />
        </div>
      </div>

      <!-- ============ ④ 研判与处置 · FR-27~30 ============ -->
      <div class="card">
        <div class="card-hd">
          <span>研判与处置</span>
          <span class="tag tag-orange">提交需二次确认</span>
        </div>
        <div class="card-bd">

          <div v-if="!canDispose" class="alert alert-warn mb-16">
            <div>{{ disposeReason }} —— 按 PRD §2.1.1，管理员对案件为<b>只读</b>，
              处置按钮已置灰禁用（而非隐藏）。</div>
          </div>

          <div v-if="isFinal" class="alert alert-info mb-16">
            <div>本案件已<b>{{ statusLabel }}</b>
              （结论 {{ detail.conclusion || '-' }}，处理人 {{ detail.assignee || '-' }}，
              时间 {{ fmt(detail.disposed_at) }}）。对终态案件重复提交将被后端拒绝（code 2003）。</div>
          </div>

          <div class="mb-16">
            <div class="sec-title" style="margin-bottom:10px"><span>处置结论</span><span class="muted">三选一</span></div>
            <div class="row-gap-12">
              <label
                v-for="opt in CONCLUSIONS" :key="opt.v"
                class="row-gap-8"
                style="font-size:13px;padding:7px 12px;border:1px solid var(--border);border-radius:6px;cursor:pointer"
                :style="conclusion === opt.v ? { borderColor: 'var(--danger)', background: 'var(--danger-bg)' } : {}"
              >
                <input
                  type="radio" v-model="conclusion" :value="opt.v"
                  :disabled="disposalDisabled" style="width:15px;height:15px"
                >
                {{ opt.t }}
              </label>
            </div>
            <div class="muted" style="margin-top:9px;font-size:12px">{{ ACTION_NOTE[conclusion] }}</div>
          </div>

          <div class="field mb-16">
            <label>处置原因<span class="req">*</span></label>
            <textarea
              v-model="remark" rows="2" :disabled="disposalDisabled"
              placeholder="请填写核查依据，将写入审计日志"
            />
          </div>

          <!-- 联动处置效果预览 -->
          <div class="field mb-16">
            <label>联动处置效果预览</label>
            <table v-if="previewRows.length" class="tb" style="margin-top:6px">
              <thead>
                <tr>
                  <th style="width:190px">联动对象</th>
                  <th style="width:150px">当前状态</th>
                  <th>处置后</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(r, i) in previewRows" :key="i">
                  <td>
                    {{ r.obj }}
                    <span v-if="r.sub" class="muted">（{{ r.sub }}）</span>
                  </td>
                  <td><span class="tag tag-gray">{{ r.from }}</span></td>
                  <td><span class="tag tag-red">{{ r.to }}</span></td>
                </tr>
              </tbody>
            </table>
            <div v-else class="muted" style="margin-top:6px">
              pass — 放行：不执行任何联动动作，仅将案件标记为误报并记录审计。
            </div>
          </div>

          <div class="row-between">
            <span class="muted">
              操作人：{{ store.displayName || '-' }}（{{ store.username || store.role }}）·
              提交后写入不可篡改审计日志（变更前 / 变更后）
            </span>
            <div class="row-gap-8">
              <button class="btn" :disabled="holdDisabled" :title="holdReason" @click="openConfirm('hold')">
                暂存挂起
              </button>
              <button class="btn" :disabled="closeDisabled" :title="closeReason" @click="openConfirm('close')">
                关闭为误报
              </button>
              <button class="btn" @click="cancel">取消</button>
              <button
                class="btn btn-danger"
                :class="{ 'is-disabled': disposalDisabled }"
                :disabled="disposalDisabled"
                :title="disposeReason"
                @click="openConfirm('dispose')"
              >
                确认提交
              </button>
            </div>
          </div>
        </div>
      </div>

      <!-- ============ ⑤ 二次确认弹窗 · FR-30 ============ -->
      <ConfirmDialog
        :visible="confirmVisible"
        :title="dialogTitle"
        :actions="dialogActions"
        warning="处置结论提交后不可撤销；审计日志写入后不支持修改与删除。"
        confirm-text="确认执行"
        :loading="submitting"
        @cancel="confirmVisible = false"
        @confirm="onConfirm"
      />
    </template>
  </StateBlock>
</template>
