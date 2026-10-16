<script setup>
/**
 * 页面二 /dashboard —— 风控态势大盘（SPEC 模块 6 · API-6.1 ~ 6.7 / FR-41 ~ FR-46）。
 *
 * 五档决策配色（v3.0）：Pass 绿 / Mark 黄 / Challenge 暗黄 / Review 橙 / Reject 深红。
 * 四档风险等级配色：low 绿 / medium 黄 / high 橙 / critical 深红。
 * 图表每 5s 轮询刷新（R-FE2-01），卸载时 clearInterval + chart.dispose()。
 */
import * as echarts from 'echarts'
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'

import { dashboardApi } from '@/api'
import EventSnapshotPanel from '@/components/EventSnapshotPanel.vue'
import RiskTag from '@/components/RiskTag.vue'
import StateBlock from '@/components/StateBlock.vue'

const loading = ref(true)
const error = ref('')
const overview = ref({})
const riskDist = ref({ items: [], total: 0 })
const topRules = ref([])
const events = ref([])
const loss = ref({ items: [], total: 0, discount: 0 })

/**
 * 展开查看事件快照的行（值为 event_id，空串表示都收起）。
 *
 * ⚠ 原型图 02-dashboard 把滚屏写死为「时间 / 用户 / 事件类型 / 决策结果」**四列**，
 *   所以这里**不动列**，改用「点一下多出一行」的加法来露出事件报文
 *   （金额 / 来源单号 / 设备 / IP / `event_data` 快照）。
 */
const expandedEvent = ref('')

function toggleEvent(eventId) {
  expandedEvent.value = expandedEvent.value === eventId ? '' : eventId
}

const trendEl = ref(null)
const donutEl = ref(null)
const decisionEl = ref(null)
const rulesEl = ref(null)

const RULE_RANK_LIMIT = 5
const TREND_DAYS = 7

/* ---------- v3.0 配色令牌（与 main.css 语义色一致） ---------- */
const DECISION_COLORS = {
  Pass: '#52c41a', Mark: '#faad14', Challenge: '#d4b106', Review: '#fa8c16', Reject: '#cf1322',
}
const DECISION_LABELS = {
  Pass: 'Pass 放行', Mark: 'Mark 标记观察', Challenge: 'Challenge 二次验证',
  Review: 'Review 人审', Reject: 'Reject 拦截',
}
const DECISION_ORDER = ['Pass', 'Mark', 'Challenge', 'Review', 'Reject']

const LEVEL_COLORS = { low: '#52c41a', medium: '#faad14', high: '#fa8c16', critical: '#cf1322' }
const LEVEL_NAMES = { low: '低 low', medium: '中 medium', high: '高 high', critical: '极高 critical' }
const LEVEL_ORDER = ['low', 'medium', 'high', 'critical']

/* ---------- 数值格式化 ---------- */
/** 比例 → 百分比，1 位小数 */
function pct(v) {
  return `${((Number(v) || 0) * 100).toFixed(1)}%`
}
/** 金额千分位 */
function money(v) {
  return `¥${Number(v || 0).toLocaleString('zh-CN', { maximumFractionDigits: 0 })}`
}
/** 整数千分位 */
function num(v) {
  return Number(v || 0).toLocaleString('zh-CN')
}
/**
 * HH:mm:ss
 *
 * ⚠ 调用时传的必须是后端返回的 `create_time`。早期滚屏读的是 `created_at`
 *   （后端从不返回该键），`!iso` 分支兜底成 `-` 且不抛错 —— 于是「实时事件滚屏」
 *   的时间列**整列显示 `-`**，而冒烟脚本只收 JS 异常，什么都没抓到。
 */
function fmtTime(iso) {
  if (!iso) return '-'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return String(iso).slice(11, 19) || '-'
  const p = (n) => String(n).padStart(2, '0')
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}
/** 趋势 x 轴标签：按小时（含 T）显示 HH:mm，按天显示 MM-DD（兼容 days / hours 两种粒度） */
function axisLabel(raw) {
  const s = String(raw || '')
  return s.includes('T') ? s.slice(11, 16) : s.slice(5, 10)
}

/* ---------- 指标卡派生值（兼容 v2/v3 字段命名） ---------- */
const assessTotal = computed(() => overview.value.assess_total ?? overview.value.event_total ?? 0)
const assessDelta = computed(() => overview.value.assess_delta ?? overview.value.event_delta ?? 0)
const highRiskCount = computed(() => {
  if (overview.value.high_risk_count != null) return overview.value.high_risk_count
  return (riskDist.value.items || [])
    .filter((i) => i.level === 'high' || i.level === 'critical')
    .reduce((a, i) => a + (i.count || 0), 0)
})
const highRiskDelta = computed(() => overview.value.high_risk_delta ?? overview.value.reject_delta ?? 0)
const pendingCases = computed(() => overview.value.pending_cases ?? 0)
const caseDelta = computed(() => overview.value.case_delta ?? 0)
const passRate = computed(() => {
  if (overview.value.pass_rate != null) return overview.value.pass_rate
  const d = overview.value.decision_dist || {}
  const total = Object.values(d).reduce((a, b) => a + (Number(b) || 0), 0)
  if (total) return (Number(d.Pass) || 0) / total
  // 兼容 v2 字段：pass_count / event_total
  const evTotal = Number(overview.value.assess_total ?? overview.value.event_total ?? 0)
  if (evTotal && overview.value.pass_count != null) return Number(overview.value.pass_count) / evTotal
  return 0
})
const passRateDelta = computed(() => overview.value.pass_rate_delta ?? 0)
const lossPrevented = computed(() => overview.value.loss_prevented ?? 0)
const lossDelta = computed(() => overview.value.loss_delta ?? 0)

/* ---------- ECharts 实例管理 ---------- */
const charts = new Map()

function setChart(el, option) {
  if (!el) return
  let inst = echarts.getInstanceByDom(el)
  if (!inst) inst = echarts.init(el)
  inst.setOption(option, true)
  charts.set(el, inst)
}

function renderTrend(points) {
  const raw = points.map((p) => String(p.date || p.time || ''))
  const x = raw.map(axisLabel)
  const rates = points.map((p) => +((Number(p.reject_rate) || 0) * 100).toFixed(2))
  const counts = points.map((p) => p.assess_cnt ?? p.event_cnt ?? 0)
  setChart(trendEl.value, {
    // ⚠ 图例位置不能再用 `right: 6, top: 2`（v3.1 修）：
    //   ECharts 的值轴默认把 `name` 画在轴顶端（右上角），而图例也放右上时，
    //   「事件量」三个字会被图例的色块与文字**压在一起**（截图可见）。
    //   放顶部居中 + 给 grid.top 留出高度，两者各占一处，互不遮挡。
    grid: { left: 48, right: 54, top: 46, bottom: 30 },
    tooltip: { trigger: 'axis' },
    legend: {
      data: ['拦截率', '事件量'], left: 'center', top: 4, itemWidth: 12, itemHeight: 8,
      textStyle: { fontSize: 11, color: '#595959' },
    },
    xAxis: {
      type: 'category', data: x, boundaryGap: false,
      axisLine: { lineStyle: { color: '#e8ebf0' } },
      axisLabel: { color: '#8c8c8c', fontSize: 11, interval: Math.max(Math.ceil(x.length / 8) - 1, 0) },
    },
    yAxis: [
      { type: 'value', name: '拦截率%', nameTextStyle: { color: '#8c8c8c', fontSize: 10 }, axisLabel: { color: '#8c8c8c', fontSize: 11 }, splitLine: { lineStyle: { color: '#f0f2f5' } } },
      { type: 'value', name: '事件量', nameTextStyle: { color: '#8c8c8c', fontSize: 10 }, axisLabel: { color: '#8c8c8c', fontSize: 11 }, splitLine: { show: false } },
    ],
    series: [
      {
        name: '拦截率', type: 'line', smooth: true, data: rates, yAxisIndex: 0,
        itemStyle: { color: '#cf1322' }, lineStyle: { width: 2 },
        areaStyle: { color: 'rgba(207,19,34,.08)' }, showSymbol: false,
      },
      {
        name: '事件量', type: 'bar', data: counts, yAxisIndex: 1,
        itemStyle: { color: 'rgba(22,119,255,.24)' }, barMaxWidth: 14,
      },
    ],
  })
}

function renderDonut(items) {
  const byLevel = Object.fromEntries((items || []).map((i) => [i.level, i]))
  const data = LEVEL_ORDER
    .filter((lv) => byLevel[lv])
    .map((lv) => ({
      name: LEVEL_NAMES[lv], value: byLevel[lv].count || 0,
      itemStyle: { color: LEVEL_COLORS[lv] },
    }))
  setChart(donutEl.value, {
    tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },
    legend: { bottom: 0, itemWidth: 10, itemHeight: 8, textStyle: { fontSize: 12, color: '#595959' } },
    series: [{
      type: 'pie', radius: ['52%', '74%'], center: ['50%', '44%'], avoidLabelOverlap: true,
      label: { show: false }, labelLine: { show: false },
      itemStyle: { borderColor: '#fff', borderWidth: 2 },
      data,
    }],
  })
}

function renderDecision(dist) {
  const d = dist || {}
  const sorted = [...DECISION_ORDER].reverse()
  setChart(decisionEl.value, {
    grid: { left: 150, right: 56, top: 8, bottom: 8 },
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    xAxis: { type: 'value', axisLabel: { color: '#8c8c8c', fontSize: 11 }, splitLine: { lineStyle: { color: '#f0f2f5' } } },
    yAxis: {
      type: 'category', data: sorted.map((k) => DECISION_LABELS[k]),
      axisLabel: { color: '#595959', fontSize: 11.5 }, axisLine: { lineStyle: { color: '#e8ebf0' } }, axisTick: { show: false },
    },
    series: [{
      type: 'bar', barMaxWidth: 16,
      data: sorted.map((k) => ({
        value: Number(d[k]) || 0, itemStyle: { color: DECISION_COLORS[k], borderRadius: [0, 4, 4, 0] },
      })),
      label: { show: true, position: 'right', fontSize: 11, color: '#595959', formatter: (p) => num(p.value) },
    }],
  })
}

function renderRules(items) {
  const sorted = [...(items || [])].reverse()
  setChart(rulesEl.value, {
    grid: { left: 150, right: 52, top: 8, bottom: 8 },
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    xAxis: { type: 'value', axisLabel: { color: '#8c8c8c', fontSize: 11 }, splitLine: { lineStyle: { color: '#f0f2f5' } } },
    yAxis: {
      type: 'category',
      data: sorted.map((r) => `${r.rule_code || r.rule_id || ''} ${r.rule_name || ''}`.trim()),
      axisLabel: { color: '#595959', fontSize: 11.5 }, axisLine: { lineStyle: { color: '#e8ebf0' } }, axisTick: { show: false },
    },
    series: [{
      type: 'bar', barMaxWidth: 16,
      data: sorted.map((r) => ({
        value: r.hit_count || 0,
        itemStyle: { color: LEVEL_COLORS[normalizeLevel(r.level || r.risk_level)] || '#1677ff', borderRadius: [0, 4, 4, 0] },
      })),
      label: { show: true, position: 'right', fontSize: 11, color: '#595959', formatter: (p) => num(p.value) },
    }],
  })
}

function normalizeLevel(lv) {
  return { 低: 'low', 中: 'medium', 高: 'high', 极高: 'critical' }[lv] || lv
}

/* ---------- 数据加载 ---------- */
async function load() {
  let trendPoints = []
  try {
    const [ov, dist, rules, evts, ls, tr] = await Promise.all([
      dashboardApi.overview(),
      dashboardApi.riskDist(),
      dashboardApi.topRules(RULE_RANK_LIMIT),
      dashboardApi.realtimeEvents(12),
      dashboardApi.lossPrevented(),
      dashboardApi.trend(TREND_DAYS),
    ])
    overview.value = ov || {}
    riskDist.value = dist || { items: [] }
    topRules.value = rules?.items || []
    events.value = evts?.items || []
    loss.value = ls || { items: [], total: 0, discount: 0 }
    trendPoints = tr?.points || []
    error.value = ''
  } catch (err) {
    error.value = err.message || '加载失败'
  } finally {
    loading.value = false
  }

  // 等 StateBlock 渲染出图表容器（ref 就绪）后再初始化，避免首帧空白
  await nextTick()
  if (error.value) return
  renderTrend(trendPoints)
  renderDonut(riskDist.value.items || [])
  renderDecision(overview.value.decision_dist || {})
  renderRules(topRules.value)
}

function resize() {
  charts.forEach((c) => c && !c.isDisposed() && c.resize())
}

let timer = null

onMounted(async () => {
  await load()
  timer = setInterval(load, 5000) // R-FE2-01
  window.addEventListener('resize', resize)
})

onBeforeUnmount(() => {
  clearInterval(timer)
  window.removeEventListener('resize', resize)
  charts.forEach((c) => c && !c.isDisposed() && c.dispose())
  charts.clear()
})
</script>

<template>
  <StateBlock :loading="loading" :error="error">
    <!-- ① 五个核心指标卡 -->
    <div class="grid" style="grid-template-columns:repeat(5,1fr)">
      <div class="metric">
        <div class="lb">今日评估量</div>
        <div class="val">{{ num(assessTotal) }}</div>
        <div class="sub">
          较昨日
          <em :class="assessDelta >= 0 ? 'up' : 'down'">
            {{ assessDelta >= 0 ? '↑' : '↓' }} {{ pct(Math.abs(assessDelta)) }}
          </em>
        </div>
      </div>
      <div class="metric">
        <div class="lb">高风险数（高+极高）</div>
        <div class="val" style="color:var(--danger-text)">{{ num(highRiskCount) }}</div>
        <div class="sub">
          较昨日
          <em :class="highRiskDelta >= 0 ? 'down' : 'up'">
            {{ highRiskDelta >= 0 ? '↑' : '↓' }} {{ pct(Math.abs(highRiskDelta)) }}
          </em>
        </div>
      </div>
      <div class="metric">
        <div class="lb">待审案件</div>
        <div class="val" style="color:var(--warning-text)">{{ num(pendingCases) }}</div>
        <div class="sub">较昨日 <em class="down">↑ {{ caseDelta }}</em></div>
      </div>
      <div class="metric">
        <div class="lb">通过率</div>
        <div class="val" style="color:var(--success-text)">{{ pct(passRate) }}</div>
        <div class="sub">
          较昨日
          <em :class="passRateDelta >= 0 ? 'up' : 'down'">
            {{ passRateDelta >= 0 ? '↑' : '↓' }} {{ pct(Math.abs(passRateDelta)) }}
          </em>
        </div>
      </div>
      <div class="metric">
        <div class="lb">预估挽回资损</div>
        <div class="val" style="color:var(--success-text)">{{ money(lossPrevented) }}</div>
        <div class="sub">较昨日 <em class="up">↑ {{ money(lossDelta) }}</em></div>
      </div>
    </div>

    <!-- ② 拦截率趋势 + 风险等级分布 -->
    <div class="grid g-2-1">
      <div class="card" style="margin-bottom:0">
        <div class="card-hd">
          <span>拦截率与事件量趋势</span>
          <!-- 图表自身的 legend 已经能点击隐藏系列，这里不再重复画一套色块图例。
               两套图例并排会让卡片头看起来很挤，而且颜色对不上时更让人困惑。 -->
          <span class="muted">近 {{ TREND_DAYS }} 天 · 每 5s 刷新</span>
        </div>
        <div class="card-bd"><div ref="trendEl" class="chart-box" style="height:236px" /></div>
      </div>

      <div class="card" style="margin-bottom:0">
        <div class="card-hd"><span>风险等级分布</span><span class="muted">四档 · 全量</span></div>
        <div class="card-bd"><div ref="donutEl" class="chart-box" style="height:236px" /></div>
      </div>
    </div>

    <!-- ③ 决策分布（五档） -->
    <div class="card">
      <div class="card-hd">
        <span>决策分布</span>
        <span class="legend">
          <span><i style="background:#52c41a" />Pass</span>
          <span><i style="background:#faad14" />Mark</span>
          <span><i style="background:#d4b106" />Challenge</span>
          <span><i style="background:#fa8c16" />Review</span>
          <span><i style="background:#cf1322" />Reject</span>
        </span>
      </div>
      <div class="card-bd">
        <div ref="decisionEl" class="chart-box" style="height:190px" />
      </div>
    </div>

    <!-- ④ 规则命中排行 + 实时事件滚屏 -->
    <div class="grid g-2-1">
      <div class="card" style="margin-bottom:0">
        <div class="card-hd"><span>规则命中排行</span><span class="muted">TOP {{ RULE_RANK_LIMIT }} · 今日</span></div>
        <div class="card-bd"><div ref="rulesEl" class="chart-box" style="height:210px" /></div>
      </div>

      <div class="card" style="margin-bottom:0">
        <div class="card-hd">
          <span>实时事件滚屏</span>
          <span class="row" style="gap:6px">
            <span class="muted">点击行查看事件快照</span>
            <span style="width:7px;height:7px;border-radius:50%;background:var(--success);display:inline-block" />
            <span class="muted">5s 轮询</span>
          </span>
        </div>
        <div class="card-bd tight">
          <div class="ticker" :class="{ open: !!expandedEvent }">
            <template v-for="e in events" :key="e.event_id">
              <div
                class="ticker-row clickable"
                :class="{ sel: expandedEvent === e.event_id }"
                @click="toggleEvent(e.event_id)"
              >
                <span class="t">{{ fmtTime(e.create_time) }}</span>
                <span class="u">{{ e.user_id }}</span>
                <span class="e">{{ e.event_type }}</span>
                <RiskTag kind="decision" :value="e.decision" />
              </div>
              <div v-if="expandedEvent === e.event_id" class="ticker-snap">
                <EventSnapshotPanel :event-id="e.event_id" />
              </div>
            </template>
            <div v-if="!events.length" class="muted" style="padding:16px 0;text-align:center">暂无事件</div>
          </div>
        </div>
      </div>
    </div>

    <!-- ⑤ 资损防护统计（按场景拆分） -->
    <div class="card" style="margin-bottom:0">
      <div class="card-hd"><span>资损防护统计</span><span class="muted">按场景 · 判定 Reject 的订单金额 × 折扣系数</span></div>
      <div class="card-bd">
        <div v-for="i in loss.items" :key="i.scene" class="kv">
          <span class="k">{{ i.name }}</span>
          <span class="v" style="color:var(--success-text)">{{ money(i.amount) }}</span>
        </div>
        <div class="divider" style="margin:10px 0" />
        <div class="kv">
          <span class="k" style="font-weight:600;color:var(--text1)">合计挽回</span>
          <span class="v" style="color:var(--success-text);font-size:15px">{{ money(loss.total) }}</span>
        </div>
        <div style="font-size:11.5px;color:var(--text3);margin-top:8px;line-height:1.7">
          口径：判定为 Reject 的 order_* 事件 amount 累加 × {{ loss.discount || 0.6 }} 折扣系数（SPEC R-MA-01）
        </div>
      </div>
    </div>
  </StateBlock>
</template>

<style scoped>
/*
 * ⚠ `.ticker` 在 main.css 里是 `max-height:236px; overflow:hidden` ——
 *   滚屏只需要「一排一排往下走」，不需要滚动条。但展开事件快照后，
 *   那点高度会把面板切掉一半。所以只在**有行展开时**放开高度并允许滚动。
 */
.ticker.open { max-height: 420px; overflow-y: auto; }
.ticker-row.clickable { cursor: pointer; }
.ticker-row.clickable:hover { background: #fafbfd; }
.ticker-row.sel { background: #f5f8ff; }
.ticker-snap {
  padding: 12px 14px 14px;
  border-bottom: 1px solid var(--border2);
  background: #fbfcfe;
}
</style>
