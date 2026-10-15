<script setup>
/**
 * 页面八 /simulate —— 事件仿真测试（v3.0）
 *
 * FR-47~51 / R-FE2-06 / R-FE2-16 / SPEC 模块 7：
 *   六类事件动态渲染参数字段 → POST /simulate/run → 四步判定链路（LinkTrace）。
 *   R-SM-01：仿真不写 risk_event，不污染大盘；R-SM-02：走完整决策链路。
 */
import { computed, onMounted, reactive, ref } from 'vue'

import { ruleApi, simulateApi } from '@/api'
import LinkTrace from '@/components/LinkTrace.vue'
import RiskTag from '@/components/RiskTag.vue'
import StateBlock from '@/components/StateBlock.vue'

/** 六类事件（SPEC §2 / 模块 7） */
const EVENT_TYPES = [
  { value: 'order_create', label: 'order_create 下单' },
  { value: 'coupon_receive', label: 'coupon_receive 领券' },
  { value: 'order_pay', label: 'order_pay 支付' },
  { value: 'after_sale_apply', label: 'after_sale_apply 售后申请' },
  { value: 'login', label: 'login 登录' },
  { value: 'logistics_complaint', label: 'logistics_complaint 物流投诉' },
]

/** 条件必填规则（SPEC §1.4） */
const REQUIRED_DEVICE = ['login', 'order_create', 'order_pay', 'coupon_receive']
const REQUIRED_BIZ = ['coupon_receive', 'order_create', 'order_pay', 'after_sale_apply', 'logistics_complaint']
const REQUIRED_AMOUNT = ['order_create', 'order_pay', 'after_sale_apply']

const loading = ref(true)
const error = ref('')
const running = ref(false)
const runError = ref('')
const templates = ref([])
const features = ref([])
const result = ref(null)
const activeTemplate = ref('')
const showRaw = ref(false)
const jsonMode = ref(false)
const jsonText = ref('')
const toast = ref('')

const form = reactive({
  event_type: 'order_create',
  user_id: 'U-88213',
  device_id: 'DEV-A9F2',
  ip: '10.22.33.44',
  phone: '13800001234',
  address: '浙A-XX小区',
  biz_no: 'O-77123',
  amount: 2880,
})

const needDevice = computed(() => REQUIRED_DEVICE.includes(form.event_type))
const needBiz = computed(() => REQUIRED_BIZ.includes(form.event_type))
const needAmount = computed(() => REQUIRED_AMOUNT.includes(form.event_type))

const requiredHint = computed(() => {
  const parts = ['user_id']
  if (needDevice.value) parts.push('device_id')
  if (needBiz.value) parts.push('biz_no')
  if (needAmount.value) parts.push('amount')
  return parts.join(' / ')
})

const steps = computed(() => result.value?.steps || [])
const challenge = computed(() =>
  result.value?.raw_response?.challenge
  || steps.value.find((s) => s.step === 4)?.challenge
  || null)
const decision = computed(() => result.value?.raw_response?.decision || '')
const finalScore = computed(() => {
  const s4 = steps.value.find((s) => s.step === 4)
  return s4?.detail?.final_score ?? s4?.detail?.total_score ?? ''
})

function buildPayload() {
  const p = { event_type: form.event_type, user_id: form.user_id }
  if (form.device_id) p.device_id = form.device_id
  if (form.ip) p.ip = form.ip
  if (form.phone) p.phone = form.phone
  if (form.address) p.address = form.address
  if (form.biz_no) p.biz_no = form.biz_no
  if (form.amount !== '' && form.amount !== null) p.amount = Number(form.amount)
  return p
}

async function run() {
  running.value = true
  runError.value = ''
  try {
    if (jsonMode.value) {
      let parsed
      try { parsed = JSON.parse(jsonText.value) } catch (e) { throw new Error(`JSON 解析失败：${e.message}`) }
      result.value = await simulateApi.run(parsed)
    } else {
      result.value = await simulateApi.run(buildPayload())
    }
    activeTemplate.value = ''
    showRaw.value = false
  } catch (err) {
    runError.value = err.message
    result.value = null
  } finally {
    running.value = false
  }
}

async function runTemplate(tpl) {
  running.value = true
  runError.value = ''
  try {
    result.value = await simulateApi.runTemplate(tpl.template_code)
    activeTemplate.value = tpl.template_code
    if (tpl.request) {
      Object.assign(form, {
        event_type: tpl.request.event_type || tpl.event_type,
        user_id: tpl.request.user_id || '',
        device_id: tpl.request.device_id || '',
        ip: tpl.request.ip || '',
        phone: tpl.request.phone || '',
        address: tpl.request.address || '',
        biz_no: tpl.request.biz_no || '',
        amount: tpl.request.amount ?? '',
      })
    }
    jsonMode.value = false
    showRaw.value = false
  } catch (err) {
    runError.value = err.message
  } finally {
    running.value = false
  }
}

function reset() {
  Object.assign(form, {
    event_type: 'order_create', user_id: 'U-88213', device_id: 'DEV-A9F2',
    ip: '10.22.33.44', phone: '13800001234', address: '浙A-XX小区',
    biz_no: 'O-77123', amount: 2880,
  })
  result.value = null
  runError.value = ''
  activeTemplate.value = ''
}

function switchToJson() {
  jsonText.value = JSON.stringify(buildPayload(), null, 2)
  jsonMode.value = true
}

const expectTags = (expect) => String(expect || '').split('/').map((s) => s.trim()).filter(Boolean)

/**
 * 二次验证结果回调（SPEC API-3.15 / R-FE2-16）。
 *
 * 仿真执行是 `dry_run`，不落库、也就不产生 `risk_challenge_record`，
 * 因此**没有可回调的真实挑战**。这里如实告知用户：
 * 想看真实的 Challenge → Review 升级链路，需经由事件网关
 * （`POST /risk/check`，非 dry_run）产生一条真实 Challenge，
 * 再由策略师在页面八或直接调 `POST /challenge/{event_id}/resolve` 回调。
 *
 * v3.0 这里只弹一句「模拟…通过/失败」的提示，看起来像已经回调成功，
 * 但前端从未调用任何接口 —— 属「控件在、功能不在」（SPEC R-FE2-21）。
 */
function onChallengeResolve(result) {
  const label = result === 'pass' ? '验证通过' : '验证失败'
  toast.value = result === 'pass'
    ? `${label}：真实链路的下一步是「决策升级 Pass，不建案」（R-RE-14）。`
      + '注意：仿真为 dry_run，未落库，因此没有可回调的真实挑战记录。'
    : `${label}：真实链路的下一步是「决策升级 Review 并创建案件」（R-RE-14）。`
      + '注意：仿真为 dry_run，未落库，因此没有可回调的真实挑战记录。'
}

onMounted(async () => {
  try {
    const [t, f] = await Promise.all([simulateApi.templates(), ruleApi.featureDict()])
    templates.value = t || []
    features.value = f || []
    error.value = ''
  } catch (err) {
    error.value = err.message
  } finally {
    loading.value = false
  }
})
</script>

<template>
  <StateBlock :loading="loading" :error="error">
    <div v-if="toast" class="alert alert-info mb-16"><div>{{ toast }}</div></div>

    <div class="grid" style="grid-template-columns: 340px 1fr; align-items: start">

      <!-- ================= 左：输入区 ================= -->
      <div class="stack-16">
        <div class="card" style="margin-bottom:0">
          <div class="card-hd">
            <span>事件参数输入</span>
            <span class="link" @click="jsonMode ? (jsonMode = false) : switchToJson()">
              {{ jsonMode ? '切换表单' : '切换 JSON' }}
            </span>
          </div>

          <div class="card-bd">
            <!-- JSON 模式 -->
            <div v-if="jsonMode" class="field">
              <label>事件 JSON</label>
              <textarea v-model="jsonText" rows="16" class="mono" style="font-size:12px" />
            </div>

            <!-- 表单模式（R-FE2-06：按事件类型动态渲染字段） -->
            <div v-else class="stack-12">
              <div class="field">
                <label>事件类型<span class="req">*</span></label>
                <select v-model="form.event_type">
                  <option v-for="t in EVENT_TYPES" :key="t.value" :value="t.value">{{ t.label }}</option>
                </select>
              </div>

              <div class="muted" style="font-size:11.5px;margin-top:-4px">
                当前事件必填：<span class="mono">{{ requiredHint }}</span>
              </div>

              <div class="field">
                <label>用户编号<span class="req">*</span></label>
                <input v-model.trim="form.user_id" type="text" class="mono">
              </div>

              <div class="field">
                <label>设备指纹<span v-if="needDevice" class="req">*</span></label>
                <input v-model.trim="form.device_id" type="text" class="mono" placeholder="DEV-A9F2">
              </div>

              <div class="field">
                <label>IP 地址</label>
                <input v-model.trim="form.ip" type="text" class="mono" placeholder="合法 IPv4">
              </div>

              <div class="field">
                <label>收货手机</label>
                <input v-model.trim="form.phone" type="text" class="mono" placeholder="11 位数字">
              </div>

              <div class="field">
                <label>收货地址</label>
                <input v-model.trim="form.address" type="text">
              </div>

              <div class="field">
                <label>业务单号 biz_no<span v-if="needBiz" class="req">*</span></label>
                <input v-model.trim="form.biz_no" type="text" class="mono">
              </div>

              <div class="field">
                <label>金额 amount<span v-if="needAmount" class="req">*</span></label>
                <input v-model="form.amount" type="number" min="0">
              </div>
            </div>

            <div class="row-gap-8" style="margin-top:12px">
              <button class="btn btn-primary" style="flex:1;justify-content:center" :disabled="running" @click="run">
                {{ running ? '检测中…' : '执行检测' }}
              </button>
              <button class="btn" style="flex:1;justify-content:center" @click="reset">重置</button>
            </div>

            <div class="alert alert-info" style="margin-top:14px">
              <div>仿真<b>不写 risk_event 表</b>，不污染大盘统计（R-SM-01）；
                但走<b>完整决策链路</b>（名单 → 特征 → 规则 → 一票否决 → 五档仲裁），与线上一致（R-SM-02）。</div>
            </div>
          </div>
        </div>

        <!-- 用例模板 -->
        <div class="card" style="margin-bottom:0">
          <div class="card-hd"><span>用例模板</span><span class="muted">一键回放</span></div>
          <div class="card-bd tight">
            <div class="stack-12" style="gap:7px">
              <div
                v-for="t in templates" :key="t.template_code"
                class="tpl" :class="{ on: activeTemplate === t.template_code }"
                @click="runTemplate(t)"
              >
                <div style="min-width:0">
                  <div style="font-weight:500">{{ t.name }}</div>
                  <div class="muted" style="font-size:11.5px">{{ t.template_code }} · {{ t.desc }}</div>
                </div>
                <span class="row-gap-8" style="gap:3px">
                  <template v-for="(e, i) in expectTags(t.expect)" :key="i">
                    <span v-if="i" class="muted">/</span>
                    <RiskTag kind="decision" :value="e" />
                  </template>
                </span>
              </div>
            </div>
            <div class="muted" style="margin-top:9px;font-size:11.5px">
              预期结果标签对应五档：Pass 放行 / Mark 标记观察 / Challenge 二次验证 / Review 人审 / Reject 拦截。
            </div>
          </div>
        </div>
      </div>

      <!-- ================= 右：链路区 ================= -->
      <div class="stack-16">
        <div v-if="runError" class="alert alert-danger"><div>{{ runError }}</div></div>

        <div class="card" style="margin-bottom:0">
          <div class="card-hd">
            <span>判定链路</span>
            <span v-if="result" class="row-gap-8">
              <span v-if="result.template" class="tag tag-gray">{{ result.template.template_code }} {{ result.template.name }}</span>
              <span v-if="decision" class="tag" :class="decision === 'Reject' ? 'tag-red' : decision === 'Review' ? 'tag-orange' : decision === 'Challenge' || decision === 'Mark' ? 'tag-yellow' : 'tag-green'">
                {{ decision }} {{ finalScore }} 分
              </span>
            </span>
          </div>

          <div class="card-bd">
            <div v-if="!result" class="empty" style="padding:64px 20px">
              <span class="ic">⚡</span>
              请在左侧填写事件参数或点击用例模板，然后执行检测
            </div>

            <template v-else>
              <LinkTrace :steps="steps" :challenge="challenge" :feature-dict="features" @challenge-resolve="onChallengeResolve" />

              <!-- 模板预期对照 -->
              <div v-if="result.template" class="alert mt-16"
                   :class="result.template.matched ? 'alert-info' : 'alert-warn'">
                <div>
                  模板预期 <b>{{ result.template.expect }}</b> ·
                  实测 <b>{{ result.template.observed }}</b>
                  <span v-if="result.template.matched"> —— 一致 ✓</span>
                  <span v-else> —— <b>不一致</b>，请核对规则集与阈值配置（本页不掩盖差异）。</span>
                </div>
              </div>

              <div class="row-between" style="margin-top:16px">
                <span class="muted">原始响应可展开查看完整 JSON</span>
                <button class="btn btn-sm" @click="showRaw = !showRaw">
                  {{ showRaw ? '收起原始 JSON' : '查看原始 JSON 返回' }}
                </button>
              </div>

              <div v-if="showRaw" class="code" style="margin-top:10px;max-height:340px;overflow:auto">{{
                JSON.stringify(result.raw_response, null, 2)
              }}</div>
            </template>
          </div>
        </div>
      </div>
    </div>
  </StateBlock>
</template>

<style scoped>
.tpl {
  display: flex; align-items: center; justify-content: space-between; gap: 10px;
  padding: 9px 12px; border: 1px solid var(--border); border-radius: 6px;
  font-size: 13px; cursor: pointer; background: #fff; transition: all .15s;
}
.tpl:hover { border-color: var(--primary); background: #fafcff; }
.tpl.on { border-color: var(--primary); background: var(--primary-bg); }
</style>
