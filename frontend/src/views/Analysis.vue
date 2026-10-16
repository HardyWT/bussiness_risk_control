<script setup>
/**
 * 页面十 /analysis —— 趋势与规则效果分析（SPEC 模块 6 · API-6.2 / 6.3 / 6.7 / FR-42、43、47）。
 *
 * 图表：
 *   ① 评估量与高风险数趋势（近 N 天双轴折线）
 *   ② 决策分布堆叠条形（近 7 天，五档配色）
 *   ③ 风险等级分布趋势（近 14 天四线折线）
 * 表格：规则效果分析（零命中规则整行高亮）。
 */
import * as echarts from 'echarts'
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'

import { dashboardApi, fetchAnalysisCsv } from '@/api'
import RiskTag from '@/components/RiskTag.vue'
import StateBlock from '@/components/StateBlock.vue'

const loading = ref(true)
const error = ref('')
const ruleLoading = ref(true)
const ruleError = ref('')
const points = ref([])
const ruleItems = ref([])
const ruleTotal = ref(0)
const riskDist = ref({ items: [] })

const rangeDays = ref(30)
const eventType = ref('')

/** 图表实际展示窗口：不超过所选时间范围，过长会挤成一团 */
const decisionWindow = computed(() => Math.min(rangeDays.value, 7))
const levelWindow = computed(() => Math.min(rangeDays.value, 14))

const RANGE_OPTIONS = [
  { value: 7, label: '近 7 天' },
  { value: 30, label: '近 30 天' },
  { value: 90, label: '近 90 天' },
]
const EVENT_TYPES = [
  { value: '', label: '全部类型' },
  { value: 'order_create', label: 'order_create 下单' },
  { value: 'order_pay', label: 'order_pay 支付' },
  { value: 'after_sale_apply', label: 'after_sale_apply 售后' },
  { value: 'coupon_receive', label: 'coupon_receive 领券' },
  { value: 'login', label: 'login 登录' },
  { value: 'logistics_complaint', label: 'logistics_complaint 投诉' },
]

const DECISION_COLORS = {
  Pass: '#52c41a', Mark: '#faad14', Challenge: '#d4b106', Review: '#fa8c16', Reject: '#cf1322',
}
const DECISION_ORDER = ['Pass', 'Mark', 'Challenge', 'Review', 'Reject']

const LEVEL_COLORS = { low: '#52c41a', medium: '#faad14', high: '#fa8c16', critical: '#cf1322' }
const LEVEL_NAMES = { low: '低', medium: '中', high: '高', critical: '极高' }
const LEVEL_ORDER = ['low', 'medium', 'high', 'critical']

const trendEl = ref(null)
const decisionEl = ref(null)
const levelEl = ref(null)
const charts = new Map()

function pct(v, d = 1) {
  return `${((Number(v) || 0) * 100).toFixed(d)}%`
}
function num(v) {
  return Number(v || 0).toLocaleString('zh-CN')
}
function normalizeLevel(lv) {
  return { 低: 'low', 中: 'medium', 高: 'high', 极高: 'critical' }[lv] || lv
}
/** 趋势 x 轴标签：含 T 视为小时粒度，否则按天（MM-DD） */
function axisLabel(raw) {
  const s = String(raw || '')
  return s.includes('T') ? s.slice(11, 16) : s.slice(5, 10)
}

/* ---------- 从趋势点提取五档决策（兼容 v2/v3 字段命名） ---------- */
function extractDecisions(p) {
  const total = Number(p.assess_cnt ?? p.event_cnt ?? 0)
  const reject = Number(p.reject_cnt ?? p.Reject ?? 0)
  const review = Number(p.review_cnt ?? p.Review ?? 0)
  const challenge = Number(p.challenge_cnt ?? p.Challenge ?? 0)
  const pass = p.pass_cnt ?? p.Pass ?? Math.max(total - reject - review - challenge, 0)
  const mark = Number(p.mark_cnt ?? p.Mark ?? 0)
  return { total, Pass: pass, Mark: mark, Challenge: challenge, Review: review, Reject: reject }
}

/* ---------- 从趋势点提取四档风险等级数；缺失返回 null ---------- */
function extractLevels(p) {
  const keys = ['low', 'medium', 'high', 'critical']
  const has = keys.some((k) => p[`${k}_cnt`] != null || p[k] != null)
  if (!has) return null
  const out = {}
  keys.forEach((k) => { out[k] = Number(p[`${k}_cnt`] ?? p[k] ?? 0) })
  return out
}

function setChart(el, option) {
  if (!el) return
  let inst = echarts.getInstanceByDom(el)
  if (!inst) inst = echarts.init(el)
  inst.setOption(option, true)
  charts.set(el, inst)
}

function renderTrend(list) {
  const x = list.map((p) => axisLabel(p.date || p.time || ''))
  const assess = list.map((p) => p.assess_cnt ?? p.event_cnt ?? 0)
  const high = list.map((p) => p.high_risk_cnt ?? 0)
  setChart(trendEl.value, {
    // ⚠ 图例改「顶部居中」+ 抬高 grid.top（v3.1 修）：
    //   原来是 `right: 6, top: 2`，而右侧值轴的 `name: '高风险数'` 默认画在轴顶端，
    //   于是「高风险数」被图例压在下面 —— 截图里那一处文字重叠就是它。
    grid: { left: 52, right: 56, top: 46, bottom: 30 },
    tooltip: { trigger: 'axis' },
    legend: {
      data: ['评估量', '高风险数'], left: 'center', top: 4, itemWidth: 12, itemHeight: 8,
      textStyle: { fontSize: 11, color: '#595959' },
    },
    xAxis: {
      type: 'category', data: x, boundaryGap: false,
      axisLine: { lineStyle: { color: '#e8ebf0' } },
      axisLabel: { color: '#8c8c8c', fontSize: 11, interval: Math.max(Math.ceil(x.length / 10) - 1, 0) },
    },
    yAxis: [
      { type: 'value', name: '评估量', nameTextStyle: { color: '#8c8c8c', fontSize: 10 }, axisLabel: { color: '#8c8c8c', fontSize: 11 }, splitLine: { lineStyle: { color: '#f0f2f5' } } },
      { type: 'value', name: '高风险数', nameTextStyle: { color: '#8c8c8c', fontSize: 10 }, axisLabel: { color: '#8c8c8c', fontSize: 11 }, splitLine: { show: false } },
    ],
    series: [
      {
        name: '评估量', type: 'line', smooth: true, yAxisIndex: 0, data: assess,
        itemStyle: { color: '#1677ff' }, lineStyle: { width: 2 }, showSymbol: false,
        areaStyle: { color: 'rgba(22,119,255,.08)' },
      },
      {
        name: '高风险数', type: 'line', smooth: true, yAxisIndex: 1, data: high,
        itemStyle: { color: '#cf1322' }, lineStyle: { width: 2, type: 'dashed' }, showSymbol: false,
      },
    ],
  })
}

function renderDecisionStack(list) {
  const win = list.slice(-decisionWindow.value)
  const rows = win.map((p) => ({ date: axisLabel(p.date || p.time || ''), d: extractDecisions(p) }))
  const ordered = [...rows].reverse()
  const series = DECISION_ORDER.map((k) => ({
    name: k, type: 'bar', stack: 'total', barMaxWidth: 20,
    itemStyle: { color: DECISION_COLORS[k] },
    label: {
      show: true, fontSize: 10.5, color: '#fff', fontWeight: 600,
      formatter: (p) => (p.value >= 3 ? `${p.value.toFixed(1)}%` : ''),
    },
    data: ordered.map((r) => (r.d.total ? +(r.d[k] / r.d.total * 100).toFixed(1) : 0)),
  }))
  setChart(decisionEl.value, {
    // ⚠ 本图**不放图内 legend**（v3.1 修）：
    //   横向堆叠柱的五段颜色由卡片头的 HTML 图例说明即可，
    //   卡片头 + 图内各画一套会让同一张卡片出现两行完全相同的图例（截图可见），
    //   而且图内那套还会顶到 y 轴第一行日期上。
    grid: { left: 72, right: 76, top: 10, bottom: 28 },
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, valueFormatter: (v) => `${v}%` },
    xAxis: { type: 'value', max: 100, axisLabel: { color: '#8c8c8c', fontSize: 11, formatter: '{value}%' }, splitLine: { lineStyle: { color: '#f0f2f5' } } },
    yAxis: {
      type: 'category', data: ordered.map((r) => r.date),
      axisLabel: { color: '#595959', fontSize: 11.5 }, axisLine: { lineStyle: { color: '#e8ebf0' } }, axisTick: { show: false },
    },
    series: [
      ...series,
      {
        name: '总量', type: 'bar', stack: 'total', silent: true, barMaxWidth: 20,
        itemStyle: { color: 'transparent' },
        label: { show: true, position: 'right', fontSize: 11, color: '#8c8c8c', formatter: (p) => num(ordered[p.dataIndex].d.total) },
        data: ordered.map(() => 0),
      },
    ],
  })
}

function renderLevelTrend(list) {
  const win = list.slice(-levelWindow.value)
  // 仅保留「有评估量且含等级明细」的日期：空日不参与，避免整列 0% 的假断点
  const rows = win
    .map((p) => ({
      date: axisLabel(p.date || p.time || ''),
      lv: extractLevels(p),
      total: Number(p.assess_cnt ?? p.event_cnt ?? 0),
    }))
    .filter((r) => r.lv && r.total > 0)
  // 后端已给出每日四档明细；仅当整段缺失时才退化为全量分布常数（保底不空图）
  const useAggregate = rows.length === 0
  const xData = useAggregate
    ? win.map((p) => axisLabel(p.date || p.time || ''))
    : rows.map((r) => r.date)
  const aggregate = Object.fromEntries((riskDist.value.items || []).map((i) => [normalizeLevel(i.level), i.ratio || 0]))
  const series = LEVEL_ORDER.map((k) => ({
    name: LEVEL_NAMES[k], type: 'line', smooth: true, showSymbol: false,
    itemStyle: { color: LEVEL_COLORS[k] }, lineStyle: { width: 2 },
    data: useAggregate
      ? xData.map(() => +((aggregate[k] || 0) * 100).toFixed(1))
      : rows.map((r) => {
          const lv = r.lv || {}
          const total = LEVEL_ORDER.reduce((a, x) => a + (lv[x] || 0), 0)
          return total ? +((lv[k] || 0) / total * 100).toFixed(1) : 0
        }),
  }))
  setChart(levelEl.value, {
    // 四条折线必须靠图例区分，所以**保留图内 legend**，只是放正、让出高度
    grid: { left: 46, right: 20, top: 34, bottom: 30 },
    tooltip: { trigger: 'axis', valueFormatter: (v) => `${v}%` },
    legend: {
      data: LEVEL_ORDER.map((k) => LEVEL_NAMES[k]), left: 'center', top: 2,
      itemWidth: 10, itemHeight: 8,
      textStyle: { fontSize: 11, color: '#595959' },
    },
    xAxis: {
      type: 'category', data: xData,
      axisLine: { lineStyle: { color: '#e8ebf0' } },
      axisLabel: { color: '#8c8c8c', fontSize: 11, interval: Math.max(Math.ceil(xData.length / 6) - 1, 0) },
    },
    yAxis: { type: 'value', axisLabel: { color: '#8c8c8c', fontSize: 11, formatter: '{value}%' }, splitLine: { lineStyle: { color: '#f0f2f5' } } },
    series,
  })
}

/* ---------- 规则效果表 ---------- */
const zeroHitCount = computed(() => ruleItems.value.filter((r) => Number(r.hit_count) === 0).length)

function ruleCode(r) { return r.rule_code || r.rule_id || '-' }
function ruleCategory(r) { return r.category || r.rule_category || '-' }
function ruleLevel(r) { return normalizeLevel(r.level || r.risk_level || '') }
/**
 * 规则是否启用。
 *
 * ⚠ 后端 `rule_effectiveness` 返回的键是 `is_enabled`（0/1），
 *   早期这里只读 `enabled` / `status` —— 两个键后端都不返回，
 *   于是三次判断全部落空、恒 `return true`，**「状态」列对每条规则都显示「启用」**，
 *   连 `is_enabled=0` 的停用规则也显示成启用（筛选/判定列永远不对，且不报错）。
 *   `is_enabled` 必须排在第一位；后两个键保留只为兼容别处的返回形态。
 */
function ruleEnabled(r) {
  if (r.is_enabled != null) return Number(r.is_enabled) !== 0
  if (r.enabled != null) return Boolean(r.enabled)
  if (r.status != null) return !['disabled', '停用', 'inactive'].includes(r.status)
  return true
}
function ruleEffect(r) {
  if (r.effect) return r.effect
  if (r.effect_note) return r.effect_note
  if (Number(r.hit_count) === 0) return '⚠ 零命中 —— 建议核查阈值与条件'
  return '命中率健康'
}

/* ---------- 加载（各接口独立容错） ---------- */
async function load() {
  loading.value = true
  ruleLoading.value = true
  const [trRes, rdRes, reRes] = await Promise.allSettled([
    dashboardApi.trend(rangeDays.value, eventType.value),
    dashboardApi.riskDist(),
    dashboardApi.ruleEffectiveness({
      days: rangeDays.value,
      ...(eventType.value ? { event_type: eventType.value } : {}),
    }),
  ])

  if (trRes.status === 'fulfilled') {
    points.value = trRes.value?.points || []
    error.value = ''
  } else {
    points.value = []
    error.value = trRes.reason?.message || '加载失败'
  }
  riskDist.value = rdRes.status === 'fulfilled' ? (rdRes.value || { items: [] }) : { items: [] }

  if (reRes.status === 'fulfilled') {
    ruleItems.value = reRes.value?.items || []
    ruleTotal.value = reRes.value?.total ?? ruleItems.value.length
    ruleError.value = ''
  } else {
    ruleItems.value = []
    ruleTotal.value = 0
    ruleError.value = reRes.reason?.message || '加载失败'
  }

  loading.value = false
  ruleLoading.value = false

  // 等 StateBlock 渲染出图表容器（ref 就绪）后再初始化，避免首帧空白
  await nextTick()
  if (!error.value && points.value.length) {
    renderTrend(points.value)
    renderDecisionStack(points.value)
    renderLevelTrend(points.value)
  }
}

/**
 * 分析师专属：导出**分析报告**（后端合成 CSV，带 UTF-8 BOM）。
 *
 * ⚠ 为什么走接口而不是前端拼 CSV：
 *   前端自己拼的 CSV 没有 BOM，Excel 打开中文全是乱码（PRD §10.2 约定必须是
 *   带 BOM 的 UTF-8）。报告还包含「每日趋势 + 规则效果」两块，
 *   让后端一次生成，口径与页面上看到的完全一致。
 */
const exporting = ref(false)
const toast = ref('')

async function exportReport() {
  exporting.value = true
  try {
    // ⚠ `fetchAnalysisCsv` 用 `responseType: 'blob'`，响应拦截器对 Blob 走
    //   「非标准包装直接返回」分支（api/index.js:61），**返回值本身就是 Blob**。
    //   早期写成 `new Blob([resp.data])`，而 Blob 上没有 `.data` →
    //   包出来的是字符串 `undefined`，导出的 CSV 打开只有一行 "undefined"。
    const resp = await fetchAnalysisCsv(rangeDays.value, eventType.value || '')
    const url = URL.createObjectURL(new Blob([resp], { type: 'text/csv;charset=utf-8' }))
    const a = document.createElement('a')
    a.href = url
    a.download = `ercs-analysis-${rangeDays.value}d.csv`
    a.click()
    URL.revokeObjectURL(url)
    toast.value = '已导出分析报告（近 ' + rangeDays.value + ' 天，UTF-8 BOM）'
  } catch (err) {
    toast.value = `导出失败：${err.message}`
  } finally {
    exporting.value = false
  }
}


function resize() {
  charts.forEach((c) => c && !c.isDisposed() && c.resize())
}

onMounted(async () => {
  await load()
  window.addEventListener('resize', resize)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', resize)
  charts.forEach((c) => c && !c.isDisposed() && c.dispose())
  charts.clear()
})
</script>

<template>
  <div>
    <div v-if="toast" class="alert alert-info mb-16"><div>{{ toast }}</div></div>

    <!-- 筛选区 -->
    <div class="card">
      <div class="card-bd tight" style="padding-top:14px;padding-bottom:14px">
        <div class="row-gap-12">
          <div class="field" style="min-width:160px">
            <label>时间范围</label>
            <select v-model.number="rangeDays" @change="load">
              <option v-for="o in RANGE_OPTIONS" :key="o.value" :value="o.value">{{ o.label }}</option>
            </select>
          </div>
          <div class="field" style="min-width:180px">
            <label>事件类型</label>
            <select v-model="eventType" @change="load">
              <option v-for="e in EVENT_TYPES" :key="e.value" :value="e.value">{{ e.label }}</option>
            </select>
          </div>
          <div style="flex:1" />
          <div class="row-gap-8" style="margin-bottom:1px">
            <button class="btn btn-primary" @click="load">刷新</button>
            <button class="btn" @click="exportReport" :disabled="exporting">
              {{ exporting ? '导出中…' : '导出报告' }}
            </button>
          </div>
        </div>
      </div>
    </div>

    <!-- 图表区 -->
    <StateBlock :loading="loading" :error="error" :empty="!points.length" empty-text="暂无趋势数据">
      <div class="card">
        <div class="card-hd">
          <span>评估量与高风险数趋势</span>
          <!-- 图内 legend 已经标注两个系列（且可点击隐藏），这里只留范围说明，
               避免同一张卡片出现两套内容相同的图例。 -->
          <span class="muted">左轴 评估量 · 右轴 高风险数 · 近 {{ rangeDays }} 天</span>
        </div>
        <div class="card-bd"><div ref="trendEl" class="chart-box" style="height:232px" /></div>
      </div>

      <div class="grid g-2-1">
        <div class="card" style="margin-bottom:0">
          <div class="card-hd">
            <span>决策分布 <span class="muted" style="font-weight:400">（近 {{ decisionWindow }} 天）</span></span>
            <span class="legend">
              <span><i style="background:#52c41a" />Pass</span>
              <span><i style="background:#faad14" />Mark</span>
              <span><i style="background:#d4b106" />Challenge</span>
              <span><i style="background:#fa8c16" />Review</span>
              <span><i style="background:#cf1322" />Reject</span>
            </span>
          </div>
          <div class="card-bd">
            <div ref="decisionEl" class="chart-box" style="height:250px" />
            <div class="muted" style="font-size:11.5px;margin-top:10px;line-height:1.8">
              五档口径：Pass 0~29 / Mark 30~59 / Challenge 60~79（全可挑战）/ Review 60~79（含不可挑战规则）/ Reject ≥80。
              Challenge 占比低属预期 —— 团伙化规则多为「不可挑战」，命中即直接转人审。
            </div>
          </div>
        </div>

        <div class="card" style="margin-bottom:0">
          <div class="card-hd"><span>风险等级分布趋势</span><span class="muted">近 {{ levelWindow }} 天</span></div>
          <div class="card-bd">
            <div ref="levelEl" class="chart-box" style="height:238px" />
            <div class="muted" style="font-size:11.5px;margin-top:10px;line-height:1.8">
              四档：低 low / 中 medium / 高 high / 极高 critical。按当日评估量占比绘制，仅统计有评估量的日期。
            </div>
          </div>
        </div>
      </div>
    </StateBlock>

    <!-- 规则效果分析 -->
    <div class="card">
      <div class="card-hd">
        <span>规则效果分析</span>
        <span class="row-gap-8">
          <span class="tag tag-gray">共 {{ ruleTotal }} 条</span>
          <span class="tag tag-red">零命中 {{ zeroHitCount }}</span>
          <span class="muted">近 {{ rangeDays }} 天</span>
        </span>
      </div>

      <StateBlock
        :loading="ruleLoading" :error="ruleError" :empty="ruleItems.length === 0"
        empty-text="暂无规则效果数据"
      >
        <table class="tb">
          <thead>
            <tr>
              <th style="width:90px">规则编码</th>
              <th style="width:180px">规则名称</th>
              <th style="width:110px">分类</th>
              <th style="width:80px">等级</th>
              <th class="num" style="width:90px">命中次数</th>
              <th class="num" style="width:90px">命中率</th>
              <th style="width:90px">状态</th>
              <th>判定</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="r in ruleItems" :key="ruleCode(r)" :class="{ 'zero-hit': Number(r.hit_count) === 0 }">
              <td class="mono"><b>{{ ruleCode(r) }}</b></td>
              <td>{{ r.rule_name || '-' }}</td>
              <td>{{ ruleCategory(r) }}</td>
              <td><RiskTag kind="level" :value="ruleLevel(r)" /></td>
              <td class="num">{{ num(r.hit_count) }}</td>
              <td class="num">{{ pct(r.hit_rate) }}</td>
              <td>
                <span class="tag" :class="ruleEnabled(r) ? 'tag-green' : 'tag-gray'">
                  {{ ruleEnabled(r) ? '启用' : '停用' }}
                </span>
              </td>
              <td style="font-size:12.5px">{{ ruleEffect(r) }}</td>
            </tr>
          </tbody>
        </table>
      </StateBlock>

      <div class="card-bd tight" style="border-top:1px solid var(--border2);font-size:12px;color:var(--text3);line-height:1.9">
        「命中率」口径 = 该规则命中次数 ÷ 同期总评估数。「零命中」规则以警示色标出，用于识别长期失效的规则。
      </div>
    </div>
  </div>
</template>

<style scoped>
/* 零命中规则整行高亮（原型 13-analysis.html .zero-hit） */
.zero-hit td {
  background: var(--danger-bg);
  color: var(--danger-text);
}
.zero-hit td b { color: var(--danger-text); }
.zero-hit:hover td { background: var(--danger-bg); }
</style>
