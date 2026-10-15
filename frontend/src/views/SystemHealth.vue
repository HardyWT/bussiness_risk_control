<script setup>
/**
 * 页面十二 /system/health —— 系统健康度（SPEC 模块 6 · API-6.10 / 6.11 / FR-51、FR-55）。
 * 仅管理员 / 运维可见。每 10s 轮询刷新。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

import { systemApi } from '@/api'
import StateBlock from '@/components/StateBlock.vue'

const loading = ref(true)
const error = ref('')
const health = ref({})
const stats = ref({})

let timer = null

/** 预警线（演示值，与原型 10-health.html 一致） */
const LINES = { error_rate: 0.01, latency: 150, pool_pct: 0.8, backlog: 50 }

function pct(v, d = 1) {
  return `${((Number(v) || 0) * 100).toFixed(d)}%`
}
function num(v) {
  return Number(v || 0).toLocaleString('zh-CN')
}
function uptime(sec) {
  const s = Number(sec || 0)
  const d = Math.floor(s / 86400)
  const h = Math.floor((s % 86400) / 3600)
  const m = Math.floor((s % 3600) / 60)
  return `${d} 天 ${h} 小时 ${m} 分`
}

/* 连接池：优先 health，其次 stats */
const poolUsed = computed(() => health.value.pool_used ?? stats.value.pool_used ?? 0)
const poolSize = computed(() => health.value.pool_size ?? stats.value.pool_size ?? 0)
const poolPct = computed(() => (poolSize.value ? poolUsed.value / poolSize.value : 0))

/* 数据统计：优先 stats，其次 health（SPEC 常量为 17 业务表 / 8 风控表） */
const businessTables = computed(() => stats.value.business_table_count ?? health.value.business_table_count ?? 17)
const riskTables = computed(() => stats.value.risk_table_count ?? health.value.risk_table_count ?? 8)
const tableCounts = computed(() => stats.value.table_counts || health.value.table_counts || {})
const tableRows = computed(() => {
  const t = tableCounts.value
  const keys = Object.keys(t)
  return keys.length ? keys.reduce((a, k) => a + (Number(t[k]) || 0), 0) : (health.value.table_rows ?? 0)
})
const dbSize = computed(() => stats.value.db_size_mb ?? health.value.db_size_mb ?? 0)

const breached = computed(() => ({
  error: (health.value.api_error_rate || 0) > LINES.error_rate,
  latency: (health.value.api_avg_latency_ms || 0) > LINES.latency,
  pool: poolPct.value > LINES.pool_pct,
  backlog: (health.value.queue_backlog || 0) > LINES.backlog,
}))
const healthy = computed(() => !Object.values(breached.value).some(Boolean))

async function load() {
  const [hRes, sRes] = await Promise.allSettled([systemApi.health(), systemApi.stats()])
  if (hRes.status === 'fulfilled') {
    health.value = hRes.value || {}
    error.value = ''
  } else {
    error.value = hRes.reason?.message || '加载失败'
  }
  // /system/stats 为扩展统计，失败时降级为 health 内嵌统计而不阻断整页
  stats.value = sRes.status === 'fulfilled' ? (sRes.value || {}) : {}
  loading.value = false
}

onMounted(async () => {
  await load()
  timer = setInterval(load, 10000)
})

onBeforeUnmount(() => clearInterval(timer))
</script>

<template>
  <StateBlock :loading="loading" :error="error">
    <!-- ① 五项核心指标 -->
    <div class="grid" style="grid-template-columns:repeat(5,1fr)">
      <div class="metric">
        <div class="lb">事件吞吐量</div>
        <div class="val">{{ health.event_qps ?? 0 }} <span class="unit">QPS</span></div>
        <div class="sub">近 1h 均值</div>
      </div>
      <div class="metric" :class="{ warn: breached.error }">
        <div class="lb">接口错误率</div>
        <div class="val" :class="{ 'val-ok': !breached.error }">{{ pct(health.api_error_rate) }}</div>
        <div v-if="breached.error" class="warnline">⚠ 已超预警线 {{ pct(LINES.error_rate) }}</div>
        <div v-else class="sub">预警线 {{ pct(LINES.error_rate) }} · 当前正常</div>
      </div>
      <div class="metric" :class="{ warn: breached.latency }">
        <div class="lb">平均耗时</div>
        <div class="val" :class="{ 'val-ok': !breached.latency }">
          {{ health.api_avg_latency_ms ?? 0 }} <span class="unit">ms</span>
        </div>
        <div v-if="breached.latency" class="warnline">⚠ 已超预警线 {{ LINES.latency }} ms</div>
        <div v-else class="sub">目标 &lt; 200 ms（G-01）</div>
      </div>
      <div class="metric" :class="{ warn: breached.pool }">
        <div class="lb">连接池使用率</div>
        <div class="val" :class="{ 'val-ok': !breached.pool }">
          {{ pct(poolPct) }}
          <span class="unit">{{ poolUsed }} / {{ poolSize }}</span>
        </div>
        <div v-if="breached.pool" class="warnline">⚠ 已超预警线 {{ pct(LINES.pool_pct) }}</div>
        <div v-else class="sub">预警线 {{ pct(LINES.pool_pct) }} · 池连接 {{ poolUsed }}/{{ poolSize }}</div>
      </div>
      <div class="metric" :class="{ warn: breached.backlog }">
        <div class="lb">队列积压</div>
        <div class="val" :class="{ 'val-ok': !breached.backlog }">{{ num(health.queue_backlog) }}</div>
        <div v-if="breached.backlog" class="warnline">⚠ 已超预警线 {{ LINES.backlog }}</div>
        <div v-else class="sub">异步落库队列 · 无积压</div>
      </div>
    </div>

    <!-- ② 数据统计区 -->
    <div class="grid g-2-1">
      <div class="card" style="margin-bottom:0">
        <div class="card-hd"><span>数据统计</span><span class="muted">业务表 + 风控表</span></div>
        <div class="card-bd">
          <div class="stat-mini">
            <div>
              <div class="n">{{ businessTables }}</div>
              <div class="l">业务表数</div>
            </div>
            <div>
              <div class="n">{{ riskTables }}</div>
              <div class="l">风控表数</div>
            </div>
            <div>
              <div class="n">{{ num(tableRows) }}</div>
              <div class="l">表行数合计</div>
            </div>
            <div>
              <div class="n">{{ dbSize }} <span style="font-size:13px;font-weight:400;color:var(--text3)">MB</span></div>
              <div class="l">数据库大小</div>
            </div>
            <div>
              <div class="n">{{ uptime(health.uptime_seconds) }}</div>
              <div class="l">服务运行时长（uptime {{ health.uptime_seconds ?? 0 }} s）</div>
            </div>
          </div>
        </div>
      </div>

      <div class="card" style="margin-bottom:0">
        <div class="card-hd"><span>各表行数</span><span class="muted">core tables</span></div>
        <div class="card-bd tight" style="padding:0">
          <table class="tb">
            <thead><tr><th>表名</th><th class="num">行数</th></tr></thead>
            <tbody>
              <tr v-for="(v, k) in tableCounts" :key="k">
                <td class="mono" style="font-size:12px">{{ k }}</td>
                <td class="num mono">{{ num(v) }}</td>
              </tr>
              <tr v-if="!Object.keys(tableCounts).length">
                <td colspan="2" class="muted" style="text-align:center;padding:18px">暂无表统计</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- ③ 接口健康度表 -->
    <div class="grid g-1-2">
      <div class="card" style="margin-bottom:0">
        <div class="card-hd"><span>接口健康度</span><span class="muted">近 1h · /system/health</span></div>
        <div class="card-bd tight" style="padding:0">
          <table class="tb">
            <thead>
              <tr>
                <th>接口</th>
                <th class="num">调用数</th>
                <th class="num">错误数</th>
                <th class="num">错误率</th>
                <th class="num">平均耗时</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="api in health.api_health || []" :key="api.path">
                <td class="mono" style="font-size:12px">{{ api.path }}</td>
                <td class="num">{{ num(api.calls) }}</td>
                <td class="num">{{ num(api.errors) }}</td>
                <td class="num" :class="{ 'err-warn': (api.error_rate || 0) > LINES.error_rate, 'val-ok': (api.error_rate || 0) <= LINES.error_rate }">
                  {{ pct(api.error_rate) }}
                </td>
                <td class="num">{{ num(api.avg_latency_ms) }} ms</td>
              </tr>
              <tr v-if="!(health.api_health || []).length">
                <td colspan="5" class="muted" style="text-align:center;padding:18px">暂无接口健康数据</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <div class="card" style="margin-bottom:0">
        <div class="card-hd"><span>预警线设定</span><span class="muted">越过即切换警示态</span></div>
        <div class="card-bd">
          <div class="alert" :class="healthy ? 'alert-info' : 'alert-warn'" style="margin-bottom:14px">
            <div>服务健康度：<b>{{ healthy ? '正常' : '告警' }}</b> · 每 10s 自动刷新</div>
          </div>
          <table class="tb">
            <thead><tr><th>指标</th><th class="num">预警线</th></tr></thead>
            <tbody>
              <tr><td>接口错误率</td><td class="num mono">&gt; {{ pct(LINES.error_rate) }}</td></tr>
              <tr><td>平均耗时</td><td class="num mono">&gt; {{ LINES.latency }} ms</td></tr>
              <tr><td>连接池使用率</td><td class="num mono">&gt; {{ pct(LINES.pool_pct) }}</td></tr>
              <tr><td>队列积压</td><td class="num mono">&gt; {{ LINES.backlog }}</td></tr>
            </tbody>
          </table>
          <div class="muted" style="margin-top:10px;font-size:11.5px">
            指标越过预警线时，对应指标卡切换为橙警示态。
          </div>
        </div>
      </div>
    </div>

    <div class="alert alert-info mt-16">
      <div>本页<b>仅管理员与运维可见</b>（PRD §2.1.1）；审核员 / 策略师 / 分析师直接访问本页会渲染 403。
        数据源 <span class="mono">GET /api/v1/system/health</span> 与 <span class="mono">GET /api/v1/system/stats</span>（SPEC API-6.10 / 6.11）。</div>
    </div>
  </StateBlock>
</template>

<style scoped>
.metric.warn { border-color: var(--warning-border); background: var(--warning-bg); }
.metric.warn .val { color: var(--warning-text); }
.metric .warnline { font-size: 11.5px; color: var(--warning-text); margin-top: 4px; }
.metric .val .unit { font-size: 14px; font-weight: 400; color: var(--text3); }
.metric .val-ok { color: var(--success-text); }
.metric.warn .val.val-ok { color: var(--warning-text); }
.err-warn { color: var(--warning-text); font-weight: 600; }
.val-ok { color: var(--success-text); }
</style>
