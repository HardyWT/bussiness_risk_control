<script setup>
/**
 * 判定链路四步时间线（v3.0 / FR-49 · R-FE2-16/17 · SPEC §8.3 LinkTrace）。
 *
 * steps = [{ step, name, status, detail }]，detail 按步骤类型渲染：
 *   ① 事件接收 → 字符串
 *   ② 特征提取 → { dimensions: { 用户/订单/地址/团伙: { key: value } }, total }（按维度分组折叠）
 *   ③ 规则命中 → [{ rule_id, rule_name, trigger_reason, risk_score, risk_level, challengeable }]
 *   ④ 综合决策 → { final_score, risk_level, decision, is_veto, rule_count, threshold, ... }
 */
import { computed, ref } from 'vue'

import RiskTag from './RiskTag.vue'

const props = defineProps({
  steps: { type: Array, default: () => [] },
  /** decision=Challenge 时第 ④ 步附带的二次验证指令 */
  challenge: { type: Object, default: null },
  /** 可选：特征字典（{key,name}），用于展示特征中文名（不改变既有契约） */
  featureDict: { type: Array, default: () => [] },
})

const emit = defineEmits(['challenge-resolve'])

const LEVEL_KEY = { 低: 'low', 中: 'medium', 高: 'high', 极高: 'critical' }
const DIM_ORDER = ['用户', '订单', '地址', '团伙']

const nameOf = (key) => props.featureDict.find((f) => f.key === key)?.name || ''

/* -------------------------------------------------- 步骤索引 */
const stepOf = (n) => props.steps.find((s) => s.step === n)
const step1 = computed(() => stepOf(1))
const step2 = computed(() => stepOf(2))
const step3 = computed(() => stepOf(3))
const step4 = computed(() => stepOf(4))

/* -------------------------------------------------- ② 特征分组 */
const dims = computed(() => {
  const d = step2.value?.detail
  if (!d || typeof d !== 'object') return []
  const source = d.dimensions && typeof d.dimensions === 'object' ? d.dimensions : { 全部特征: d }
  const keys = Object.keys(source)
  const ordered = DIM_ORDER.filter((k) => k in source).map((k) => [k, source[k]])
  for (const k of keys) if (!DIM_ORDER.includes(k) && k !== 'total') ordered.push([k, source[k]])
  return ordered.map(([dimension, values]) => ({
    dimension,
    entries: Object.entries(values || {}).map(([key, value]) => ({ key, value })),
  }))
})

/** 命中特征：出现在 step3 触发原因中的特征键（红标高亮，R-FE2-17） */
const hitKeys = computed(() => {
  const set = new Set()
  const hits = Array.isArray(step3.value?.detail) ? step3.value.detail : []
  const allKeys = dims.value.flatMap((g) => g.entries.map((e) => e.key))
  for (const h of hits) {
    const reason = String(h.trigger_reason || h.reason || '')
    for (const k of allKeys) if (reason.includes(k)) set.add(k)
  }
  return set
})

const openDims = ref({})
const isOpen = (dimension) => openDims.value[dimension] ?? dims.value.find((g) => g.dimension === dimension)?.entries.some((e) => hitKeys.value.has(e.key)) ?? false
const toggleDim = (dimension) => { openDims.value[dimension] = !isOpen(dimension) }

const fmt = (v) => {
  if (typeof v === 'boolean') return v ? '1' : '0'
  if (typeof v === 'number' && !Number.isInteger(v)) return v.toFixed(v === 0 ? 1 : 2)
  return v
}

/* -------------------------------------------------- ③ 命中规则 */
const hits = computed(() => (Array.isArray(step3.value?.detail) ? step3.value.detail : []))

/* -------------------------------------------------- ④ 决策 */
function statusClass(status) {
  if (status === 'ok') return 'ok'
  return 'skip'
}
function statusGlyph(status) {
  if (status === 'ok') return '✓'
  if (status === 'failed') return '!'
  return '—'
}

/* -------------------------------------------------- Challenge 模拟 */
const challengeResult = ref('')
function resolveChallenge(result) {
  challengeResult.value = result
  emit('challenge-resolve', result)
}
</script>

<template>
  <div class="steps">
    <div v-for="s in steps" :key="s.step" class="step">
      <span class="step-no" :class="statusClass(s.status)">{{ statusGlyph(s.status) }}</span>

      <div class="step-bd">
        <div class="step-name">{{ s.step }} {{ s.name }}</div>
        <div class="step-detail">

          <!-- ① 事件接收 -->
          <template v-if="s.step === 1">
            {{ s.detail || '未执行' }}
          </template>

          <!-- ② 特征提取 -->
          <template v-else-if="s.step === 2">
            <div v-if="!s.detail" class="muted">未执行</div>
            <template v-else>
              <div v-for="g in dims" :key="g.dimension" class="fx-group" :class="{ open: isOpen(g.dimension) }">
                <div class="fx-summary" @click="toggleDim(g.dimension)">
                  <span class="arrow">{{ isOpen(g.dimension) ? '▾' : '▸' }}</span>
                  <span>{{ g.dimension }}维度（{{ g.entries.length }}）</span>
                  <span class="cnt"> · 命中 {{ g.entries.filter(e => hitKeys.has(e.key)).length }}</span>
                </div>
                <table v-show="isOpen(g.dimension)" class="fx-table">
                  <tr v-for="e in g.entries" :key="e.key" :class="{ hit: hitKeys.has(e.key) }">
                    <td>
                      <span class="mono" style="font-size:11.5px">{{ e.key }}</span>
                      <span v-if="nameOf(e.key)" style="color:var(--text3);margin-left:6px">{{ nameOf(e.key) }}</span>
                    </td>
                    <td class="v">{{ fmt(e.value) }}</td>
                  </tr>
                </table>
              </div>
              <div class="muted" style="margin-top:5px">
                共 {{ s.detail.total ?? dims.reduce((n, g) => n + g.entries.length, 0) }} 个特征键（4 个维度分组）· 红色为已越阈值命中项
              </div>
            </template>
          </template>

          <!-- ③ 规则命中 -->
          <template v-else-if="s.step === 3">
            <div v-if="!hits.length" class="muted">未命中任何规则（名单短路或分值不足）</div>
            <table v-else class="tb" style="margin-top:6px">
              <thead>
                <tr>
                  <th style="width:64px">规则</th>
                  <th>触发原因</th>
                  <th style="width:62px">可挑战</th>
                  <th class="num" style="width:60px">分值</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="h in hits" :key="h.rule_id || h.rule_code">
                  <td class="mono"><b>{{ h.rule_id || h.rule_code }}</b></td>
                  <td class="mono" style="font-size:12px">
                    {{ h.trigger_reason || h.reason || h.rule_name }}
                    <span v-if="h.risk_level === '极高'" style="font-family:inherit;color:var(--danger-text)">（极高 · 一票否决）</span>
                  </td>
                  <td>
                    <span class="tag" :class="h.challengeable === 0 || h.challengeable === false ? 'tag-red' : 'tag-yellow'">
                      {{ h.challengeable === 0 || h.challengeable === false ? '否' : '是' }}
                    </span>
                  </td>
                  <td class="num" style="color:var(--danger-text);font-weight:600">+{{ h.risk_score ?? h.score }}</td>
                </tr>
              </tbody>
            </table>
          </template>

          <!-- ④ 综合决策 -->
          <template v-else-if="s.step === 4 && s.detail">
            <div class="row" style="gap:14px;align-items:baseline;margin:8px 0 10px">
              <div style="font-size:30px;font-weight:600;line-height:1;color:var(--danger-text)">
                {{ s.detail.final_score ?? s.detail.total_score }}
              </div>
              <div>
                <div><RiskTag kind="level" :value="LEVEL_KEY[s.detail.risk_level] || s.detail.risk_level" /></div>
                <div style="margin-top:6px"><RiskTag kind="decision" :value="s.detail.decision" /></div>
                <div style="margin-top:6px">
                  <span v-if="s.detail.is_veto" class="tag tag-red">一票否决：已触发（命中「极高」等级规则）</span>
                  <span v-else class="tag tag-gray">一票否决：未触发</span>
                </div>
              </div>
            </div>
            <div class="mono" style="font-size:12px;color:var(--text2);line-height:1.9">
              <template v-if="s.detail.calc">{{ s.detail.calc }}<br></template>
              命中规则数：{{ s.detail.rule_count ?? hits.length }}
              <template v-if="s.detail.threshold"><br>仲裁阈值：{{ s.detail.threshold }}</template>
              <template v-if="s.detail.hit_source"><br>命中来源：{{ s.detail.hit_source }}</template>
            </div>

            <!-- Challenge 分支 · R-FE2-16 -->
            <div v-if="challenge" class="alert alert-warn" style="margin-top:12px;display:block">
              <div style="margin-bottom:8px">
                <b>已下发二次验证</b> —— 判定为 Challenge，返回验证指令且<b>不建案</b>（PRD §2.4 第 3 档 / R-RE-13）。
              </div>
              <pre style="margin:0;font-family:var(--mono);font-size:12px;white-space:pre-wrap">{{ JSON.stringify(challenge, null, 2) }}</pre>

              <div style="margin:12px 0 8px">挑战窗口 <b>{{ challenge.expire_in ?? 120 }}s</b>，超时按失败处理（R-RE-15）。演示用模拟按钮：</div>
              <div class="row-gap-8">
                <button class="btn btn-sm" @click="resolveChallenge('pass')">模拟「验证通过」</button>
                <button class="btn btn-sm" @click="resolveChallenge('fail')">模拟「验证失败」</button>
              </div>

              <table class="tb" style="margin-top:12px;background:#fff">
                <thead><tr><th>验证结果</th><th>最终决策</th><th>是否建案</th></tr></thead>
                <tbody>
                  <tr><td>通过</td><td><RiskTag kind="decision" value="Pass" /></td><td>否</td></tr>
                  <tr><td>失败 / 超时 / 拒绝</td><td><RiskTag kind="decision" value="Review" /></td><td style="color:var(--warning-text);font-weight:600">是</td></tr>
                </tbody>
              </table>
              <div v-if="challengeResult" class="alert alert-info" style="margin-top:10px">
                <div>
                  模拟结果：<b>{{ challengeResult === 'pass' ? '验证通过' : '验证失败' }}</b> →
                  决策升级为 <b>{{ challengeResult === 'pass' ? 'Pass（不建案）' : 'Review（创建案件）' }}</b>。
                </div>
              </div>
            </div>
          </template>

          <template v-else>
            <span class="muted">—</span>
          </template>

        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.fx-group {
  border: 1px solid var(--border); border-radius: 6px; background: #fafbfc;
  padding: 6px 12px; margin-bottom: 8px;
}
.fx-summary { cursor: pointer; font-size: 12.5px; font-weight: 600; color: var(--text2); outline: none; user-select: none; }
.fx-summary .arrow { color: var(--text3); margin-right: 4px; }
.fx-summary .cnt { font-weight: 400; color: var(--text3); font-size: 11.5px; }
.fx-table { width: 100%; border-collapse: collapse; font-size: 12.5px; margin-top: 4px; }
.fx-table td { padding: 3px 0; border: none; }
.fx-table td.v { text-align: right; font-family: var(--mono); }
.fx-table tr.hit td { color: var(--danger-text); font-weight: 600; }
</style>
