<script setup>
/**
 * 页面七 /strategy/features —— 特征字典与阈值配置（v3.0）
 *
 * 特征字典：33 个特征 / 4 维度（用户 14 / 订单 8 / 地址 3 / 团伙 8），API-2.1。
 * 风险阈值：五档阈值 pass_threshold 30 / mark_threshold 60 / reject_threshold 80 /
 * veto_min_score 90 / multi_rule_bonus 3（API-3.7 / 3.8）。
 * 权限：特征字典编辑仅 strategist；风险阈值修改仅 admin（R-AU-11 / R-FE2-14 置灰禁用 + tooltip）。
 */
import { computed, onMounted, reactive, ref } from 'vue'

import { ruleApi } from '@/api'
import ConfirmDialog from '@/components/ConfirmDialog.vue'
import StateBlock from '@/components/StateBlock.vue'
import { useUserStore } from '@/stores/user'

const store = useUserStore()
// v3.1：特征字典对**全部角色只读**（PRD §1.4 / FR-10），不再区分「谁可编辑字典」。
// 阈值修改仅 admin（PRD §2.1.1）；后端 STRATEGY/ADMIN 分工见 SPEC R-AU-11。
const canEditThreshold = computed(() => store.role === 'admin')

const tab = ref('dict')
const loading = ref(true)
const error = ref('')
const toast = ref('')
const features = ref([])
const scenes = ref([])
const dimFilter = ref('')

const DIM_ORDER = ['用户', '订单', '地址', '团伙']
const DIM_LABEL = { 用户: '用户', 订单: '订单', 地址: '地址', 团伙: '设备与团伙' }

const dimCounts = computed(() => {
  const c = {}
  for (const f of features.value) c[f.dimension] = (c[f.dimension] || 0) + 1
  return c
})

const dimTabs = computed(() => [
  { value: '', label: `全部 ${features.value.length}` },
  ...DIM_ORDER.filter((d) => dimCounts.value[d]).map((d) => ({ value: d, label: `${DIM_LABEL[d]} ${dimCounts.value[d]}` })),
])

/** 按维度分组（保留维度在数据中的相对顺序，未识别维度兜底） */
const groupedFeatures = computed(() => {
  const map = {}
  for (const f of features.value) (map[f.dimension] || (map[f.dimension] = [])).push(f)
  const ordered = DIM_ORDER.filter((d) => map[d]).map((d) => ({ dimension: d, items: map[d] }))
  for (const d of Object.keys(map)) if (!DIM_ORDER.includes(d)) ordered.push({ dimension: d, items: map[d] })
  return dimFilter.value ? ordered.filter((g) => g.dimension === dimFilter.value) : ordered
})

const windowTag = (w) => (/d$/.test(w || '') && w !== '全周期' ? 'tag-orange' : 'tag-blue')

/* ---------------------------------------------------------------- 阈值表单 */
const form = reactive({
  pass_threshold: 30, mark_threshold: 60, reject_threshold: 80,
  veto_min_score: 90, multi_rule_bonus: 3,
})
const snapshot = ref(null)
const confirmVisible = ref(false)
const saving = ref(false)

/**
 * 阈值是**全局**配置，后端 `PUT /rules/scenes/{code}` 会把新值同步写进全部 7 条场景
 * （SPEC R-FE2-22）。因此前端只提交一次（对 `all` 场景），
 * **不要**遍历 7 个场景码逐个调用 —— 那会产生 7 条重复审计与 7 倍写放大。
 */
const GLOBAL_SCENE = 'all'

const band = computed(() => {
  const p = Number(form.pass_threshold), m = Number(form.mark_threshold), r = Number(form.reject_threshold)
  return [
    { color: '#52c41a', flex: Math.max(p, 1), label: `Pass 0~${p - 1}` },
    { color: '#faad14', flex: Math.max(m - p, 1), label: `Mark ${p}~${m - 1}` },
    { color: '#d46b08', flex: Math.max(r - m, 1), label: `Challenge / Review ${m}~${r - 1}` },
    { color: '#cf1322', flex: Math.max(101 - r, 1), label: `Reject ${r}~100` },
  ]
})

const thresholdError = computed(() => {
  const p = Number(form.pass_threshold), m = Number(form.mark_threshold)
  const r = Number(form.reject_threshold), v = Number(form.veto_min_score), b = Number(form.multi_rule_bonus)
  for (const [v2, name] of [[p, 'pass'], [m, 'mark'], [r, 'reject'], [v, 'veto']]) {
    if (!Number.isInteger(v2) || v2 < 0 || v2 > 100) return `${name}_threshold 必须为 0~100 的整数`
  }
  if (!Number.isInteger(b) || b < 0 || b > 50) return 'multi_rule_bonus 必须为 0~50 的整数'
  if (!(p < m && m < r)) return '放行阈值必须小于标记阈值、标记阈值必须小于拦截阈值（pass < mark < reject）'
  if (v < r) return '一票否决最低分必须 ≥ 拦截阈值（veto_min_score ≥ reject_threshold）'
  return ''
})

const changes = computed(() => {
  if (!snapshot.value) return []
  const s = snapshot.value
  const out = []
  const push = (label, before, after) => { if (String(before) !== String(after)) out.push(`${label}：${before} → ${after}`) }
  push('pass_threshold', s.pass_threshold, form.pass_threshold)
  push('mark_threshold', s.mark_threshold, form.mark_threshold)
  push('reject_threshold', s.reject_threshold, form.reject_threshold)
  push('veto_min_score', s.veto_min_score, form.veto_min_score)
  push('multi_rule_bonus', s.multi_rule_bonus, form.multi_rule_bonus)
  return out
})

function applyScene(s) {
  form.pass_threshold = s.pass_threshold ?? 30
  form.mark_threshold = s.mark_threshold ?? 60
  form.reject_threshold = s.reject_threshold ?? 80
  form.veto_min_score = s.veto_min_score ?? 90
  form.multi_rule_bonus = s.multi_rule_bonus ?? 3
  snapshot.value = {
    pass_threshold: form.pass_threshold, mark_threshold: form.mark_threshold,
    reject_threshold: form.reject_threshold, veto_min_score: form.veto_min_score,
    multi_rule_bonus: form.multi_rule_bonus,
  }
}

async function load() {
  loading.value = true
  try {
    const [f, s] = await Promise.all([ruleApi.featureDict(), ruleApi.scenes()])
    features.value = f || []
    scenes.value = s || []
    const base = scenes.value.find((x) => x.scene_code === 'all') || scenes.value[0]
    if (base) applyScene(base)
    error.value = ''
  } catch (err) {
    error.value = err.message
  } finally {
    loading.value = false
  }
}

function restore() {
  if (snapshot.value) Object.assign(form, snapshot.value)
}

function requestSave() {
  if (!canEditThreshold.value || thresholdError.value) return
  confirmVisible.value = true
}

async function doSave() {
  saving.value = true
  try {
    const payload = {
      pass_threshold: Number(form.pass_threshold),
      mark_threshold: Number(form.mark_threshold),
      reject_threshold: Number(form.reject_threshold),
      veto_min_score: Number(form.veto_min_score),
      multi_rule_bonus: Number(form.multi_rule_bonus),
    }
    // 提交一次即可：后端会把阈值同步到全部 7 条场景（R-FE2-22）
    const result = await ruleApi.updateScene(GLOBAL_SCENE, payload)
    const synced = result?.synced_scenes ?? '全部'
    confirmVisible.value = false
    toast.value = `阈值已更新为 Pass<${form.pass_threshold} / Mark<${form.mark_threshold} / `
      + `Reject≥${form.reject_threshold}，已同步 ${synced} 个场景并写入审计（即时生效）`
    await load()
  } catch (err) {
    toast.value = `保存失败：${err.message}`
  } finally {
    saving.value = false
  }
}

onMounted(load)
</script>

<template>
  <div>
    <div v-if="toast" class="alert alert-info mb-16"><div>{{ toast }}</div></div>

    <div class="card">
      <div class="tabs">
        <span :class="{ on: tab === 'dict' }" @click="tab = 'dict'">特征字典（{{ features.length }}）</span>
        <span :class="{ on: tab === 'threshold' }" @click="tab = 'threshold'">风险阈值</span>
      </div>

      <StateBlock :loading="loading" :error="error">

        <!-- ================= 特征字典 ================= -->
        <template v-if="tab === 'dict'">
          <div class="card-bd tight" style="border-bottom:1px solid var(--border2);padding-top:14px;padding-bottom:14px">
            <div class="row-between">
              <span class="muted">
                数据源 <span class="mono">GET /api/v1/features/dict</span> ·
                同时作为规则条件编辑器「特征字段」下拉的数据来源（R-FE2-07，不硬编码）
              </span>
              <span class="row-gap-8">
                <!-- v3.1：特征字典对**全部角色只读**（PRD §1.4 / FR-10）。
                     33 个特征与计算函数一一对应，属代码内常量。
                     v3.0 这里有一个「+ 新增特征」按钮，但没有任何 click 处理器
                     —— 死控件，删掉比留着诚实（SPEC R-FE2-21）。 -->
                <span class="tag tag-gray" title="33 个特征是代码内常量，新增/编辑需引入特征定义表，本期不做（PRD §1.4）">
                  只读 · 不支持新增 / 编辑
                </span>
              </span>
            </div>
          </div>

          <!-- 维度快速筛选 -->
          <div class="tabs" style="border-bottom:none;padding-top:12px">
            <span v-for="t in dimTabs" :key="t.value" :class="{ on: dimFilter === t.value }" @click="dimFilter = t.value">
              {{ t.label }}
            </span>
          </div>

          <table class="tb" style="margin-top:10px">
            <thead>
              <tr>
                <th style="width:92px">维度</th>
                <th style="width:186px">特征编码</th>
                <th style="width:176px">特征名称</th>
                <th style="width:72px">窗口</th>
                <th style="width:60px">类型</th>
                <th>计算口径与数据源</th>
              </tr>
            </thead>
            <tbody>
              <template v-for="g in groupedFeatures" :key="g.dimension">
                <tr class="dim-row">
                  <td colspan="6">{{ DIM_LABEL[g.dimension] || g.dimension }}维度 · {{ g.items.length }} 个</td>
                </tr>
                <tr v-for="f in g.items" :key="f.key">
                  <td><span class="tag tag-gray">{{ DIM_LABEL[f.dimension] || f.dimension }}</span></td>
                  <td class="mono" style="font-size:12.5px">{{ f.key }}</td>
                  <td>{{ f.name }}</td>
                  <td><span class="tag" :class="windowTag(f.window)">{{ f.window }}</span></td>
                  <td class="mono" style="font-size:12px">{{ f.dtype }}</td>
                  <td style="color:var(--text2);font-size:12.5px">{{ f.desc }}</td>
                </tr>
              </template>
            </tbody>
          </table>

          <div class="card-bd tight" style="border-top:1px solid var(--border2);font-size:12px;color:var(--text3);line-height:1.9">
            共 <b>{{ features.length }}</b> 个特征，分 4 个维度（用户 14 / 订单 8 / 地址 3 / 设备与团伙 8）。窗口以<b>事件时间戳</b>为基准而非服务器当前时间，
            保证仿真回放结果可复现（R-FE-01）；单特征计算超 500ms 则返回默认值并告警，<b>不阻断决策</b>（R-FE-03）。
            ⚠ <span class="mono">addr_account_cnt_7d</span> 使用 7 天窗口，与 <span class="mono">device_account_cnt_24h</span>（24h）口径不同，不可混用（R-FE-06）。
            事件类型共六类：<span class="mono">login / coupon_receive / order_create / order_pay / after_sale_apply / logistics_complaint</span>。
          </div>
        </template>

        <!-- ================= 风险阈值 ================= -->
        <template v-else>
          <div class="grid g2" style="padding:18px;margin-bottom:0">

            <div>
              <div v-if="!canEditThreshold" class="alert alert-warn mb-16">
                <div>按 PRD §2.1.1 与 R-AU-11，<b>风险阈值修改仅管理员</b>可操作（阈值直接决定拦截率，权限更高）。
                  当前角色为 {{ store.roleLabel }}，「保存」按钮已置灰禁用。</div>
              </div>

              <!-- 四段分档可视化（Pass 0~29 绿 / Mark 30~59 黄 / Challenge|Review 60~79 橙 / Reject 80~100 深红） -->
              <div style="display:flex;height:34px;border-radius:6px;overflow:hidden;font-size:12px;margin:6px 0 4px">
                <div v-for="(b, i) in band" :key="i"
                     :style="{ flex: b.flex, background: b.color }"
                     style="display:flex;align-items:center;justify-content:center;color:#fff;font-weight:600;text-align:center;padding:0 4px">
                  {{ b.label }}
                </div>
              </div>
              <div class="muted" style="margin-bottom:18px;font-size:12px">
                决策五档中 <b>Challenge 二次验证</b> 与 <b>Review 人审</b> 共用 60~79 区间：
                命中规则<b>全部可挑战</b>时下发二次验证（Challenge），含<b>不可挑战</b>规则时直接人审（Review）（PRD §2.4）。
              </div>

              <div class="thr">
                <div class="lb">pass_threshold · Pass 上界
                  <div class="d">综合分 &lt; 此值判 Pass 放行（区间 0~{{ form.pass_threshold - 1 }}）。默认 30</div>
                </div>
                <input v-model="form.pass_threshold" type="number" min="0" max="100" :disabled="!canEditThreshold">
              </div>
              <div class="thr">
                <div class="lb">mark_threshold · Mark 上界
                  <div class="d">综合分 &lt; 此值（且 ≥ pass_threshold）判 Mark 标记观察。默认 60</div>
                </div>
                <input v-model="form.mark_threshold" type="number" min="0" max="100" :disabled="!canEditThreshold">
              </div>
              <div class="thr">
                <div class="lb">reject_threshold · Reject 下界
                  <div class="d">综合分 ≥ 此值判 Reject 拦截并建案（区间 {{ form.reject_threshold }}~100）。默认 80</div>
                </div>
                <input v-model="form.reject_threshold" type="number" min="0" max="100" :disabled="!canEditThreshold">
              </div>
              <div class="thr">
                <div class="lb">veto_min_score · 一票否决最低分
                  <div class="d">命中「极高」等级规则触发一票否决，最终分不低于此值。默认 90</div>
                </div>
                <input v-model="form.veto_min_score" type="number" min="0" max="100" :disabled="!canEditThreshold">
              </div>
              <div class="thr">
                <div class="lb">multi_rule_bonus · 多规则命中加分
                  <div class="d">每多命中 1 条规则的分值加成，公式见下方。默认 3</div>
                </div>
                <input v-model="form.multi_rule_bonus" type="number" min="0" max="50" :disabled="!canEditThreshold">
              </div>
              <!-- v3.1：删除「不可挑战规则直接人审」开关。
                   该行为由每条规则的 challengeable 字段逐条决定，是 FR-16
                   五档仲裁的**固定语义**，不是全局可调参数；后端
                   SceneThresholdUpdate 里从来没有这个字段，保存时被 Pydantic
                   静默丢弃 —— 用户拨动开关、看到「保存成功」、实际什么都没变。
                   「有 handler 但后端不认」比没有控件更糟（SPEC R-FE2-21）。 -->

              <div class="code" style="margin-top:16px">评分算法：final = min( max(命中分值) + multi_rule_bonus × (命中数 − 1), 100 )
一票否决：命中任一「极高」等级规则 → final = max(final, veto_min_score)
中间档仲裁：命中含 challengeable = 0 的规则 → 直接 Review 人审；全部可挑战 → 下发 Challenge
示例：max(90,75,70) + 3 × (3−1) = 96，且 ≥ 90 → 最终分 96</div>

              <div class="alert alert-warn" style="margin:16px 0 0">
                <div>校验规则：<span class="mono">pass_threshold &lt; mark_threshold &lt; reject_threshold</span> 且
                  <span class="mono">veto_min_score ≥ reject_threshold</span>，否则保存被拒绝并提示。</div>
              </div>
              <div v-if="thresholdError" class="alert alert-danger" style="margin-top:12px">
                <div>{{ thresholdError }}</div>
              </div>

              <div class="row-gap-8" style="justify-content:flex-end;margin-top:16px">
                <button class="btn" :disabled="!canEditThreshold" @click="restore">恢复当前值</button>
                <button
                  class="btn btn-primary"
                  :class="{ 'is-disabled': !canEditThreshold || !!thresholdError }"
                  :disabled="!canEditThreshold || !!thresholdError"
                  :title="canEditThreshold ? '' : '当前角色无阈值修改权限（仅管理员）'"
                  @click="requestSave"
                >保存</button>
              </div>
            </div>

            <!-- 场景阈值明细 -->
            <div>
              <div class="sec-title" style="margin-top:0">
                <span>场景阈值明细</span>
                <span class="muted">rule_scene 表 · {{ scenes.length }} 条</span>
              </div>
              <table class="tb">
                <thead>
                  <tr>
                    <th>场景</th><th>绑定事件</th>
                    <th class="num">pass</th><th class="num">mark</th><th class="num">reject</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="s in scenes" :key="s.scene_code">
                    <td>{{ s.scene_code }} <span class="muted">{{ s.scene_name }}</span></td>
                    <td class="mono" style="font-size:12px">{{ s.biz_event }}</td>
                    <td class="num mono">{{ s.pass_threshold }}</td>
                    <td class="num mono">{{ s.mark_threshold }}</td>
                    <td class="num mono">{{ s.reject_threshold }}</td>
                  </tr>
                </tbody>
              </table>
              <div class="card-bd tight" style="border-top:1px solid var(--border2);font-size:12px;color:var(--text3);line-height:1.8">
                仲裁以 <span class="mono">pass_threshold</span> / <span class="mono">mark_threshold</span> / <span class="mono">reject_threshold</span> 为准，
                <span class="mono">veto_min_score</span> 为全局一票否决下限，不随场景变化。阈值<b>不可硬编码</b>，一律从
                <span class="mono">rule_scene</span> 表读取（R-RE-10）。保存后即时生效并写审计日志。
              </div>
            </div>
          </div>
        </template>
      </StateBlock>
    </div>

    <ConfirmDialog
      :visible="confirmVisible"
      title="确认修改风险阈值"
      :actions="changes.length ? [...changes, '新阈值将立即对线上事件生效（无需重启）', '本次变更将写入审计日志（含变更前 / 变更后快照）'] : ['未检测到任何变更']"
      warning="阈值直接决定全站拦截率，请确认改动范围已在仿真页验证。"
      confirm-text="确认保存"
      :loading="saving"
      @cancel="confirmVisible = false"
      @confirm="doSave"
    />
  </div>
</template>

<style scoped>
.thr {
  display: grid; grid-template-columns: 1fr 132px; gap: 12px; align-items: center;
  padding: 12px 0; border-bottom: 1px solid var(--border2);
}
.thr:last-of-type { border-bottom: none; }
.thr .lb { font-size: 13px; }
.thr .lb .d { font-size: 11.5px; color: var(--text3); margin-top: 2px; line-height: 1.6; }
.dim-row td { background: #fafbfc; font-weight: 600; font-size: 12.5px; color: var(--text2); }
</style>
