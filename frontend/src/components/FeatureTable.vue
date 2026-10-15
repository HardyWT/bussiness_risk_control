<script setup>
/**
 * 特征快照对比表（FR-25 / R-FE2-17 / SPEC §8.3 FeatureTable）。
 *
 * v3.0：特征字典扩为 **33 个 / 4 维度**（用户 14 / 订单 8 / 地址 3 / 团伙 8），
 * 按维度分组展示「当前值 / 历史基线」，被命中规则引用到的特征行高亮标红。
 */
import { computed } from 'vue'

const props = defineProps({
  features: { type: Object, default: () => ({}) },
  baseline: { type: Object, default: () => ({}) },
  /** triggered_rules 数组，用于反查哪些特征被引用（trigger_reason / reason） */
  ruleHits: { type: Array, default: () => [] },
  /** 是否按 4 个维度分组（SPEC §8.3，默认 true） */
  groupByDimension: { type: Boolean, default: true },
})

/** 4 个维度及其特征编码（顺序与 SPEC §2.3.1 ~ §2.3.4 一致） */
const DIMENSIONS = [
  {
    key: 'user',
    label: '用户维度',
    codes: [
      'user_total_orders', 'user_orders_30d', 'user_orders_7d', 'user_total_amount',
      'user_avg_order_amount', 'user_max_order_amount', 'user_refund_count',
      'user_postsale_count', 'user_refund_rate', 'user_postsale_rate',
      'user_refund_amount', 'user_cancel_count', 'user_complaint_count',
      'user_address_count',
    ],
  },
  {
    key: 'order',
    label: '订单维度',
    codes: [
      'order_total_amount', 'order_item_count', 'order_sku_count',
      'order_discount_amount', 'order_discount_rate', 'order_pay_interval_sec',
      'order_is_night', 'order_category_count',
    ],
  },
  {
    key: 'addr',
    label: '地址维度',
    codes: ['addr_total_count', 'addr_province_count', 'addr_is_new'],
  },
  {
    key: 'group',
    label: '设备与团伙维度',
    codes: [
      'device_account_cnt_24h', 'device_order_cnt_1h', 'device_coupon_cnt_1h',
      'ip_account_cnt_24h', 'ip_order_cnt_1h', 'addr_account_cnt_7d',
      'phone_refund_rate_24h', 'user_event_cnt_1h',
    ],
  },
]

/** 特征中文名与窗口（对齐 SPEC §2.3 特征字典，共 33 条） */
const META = {
  // 用户维度 · 14
  user_total_orders: { name: '历史订单总数', window: '全量' },
  user_orders_30d: { name: '近30天订单数', window: '30d' },
  user_orders_7d: { name: '近7天订单数', window: '7d' },
  user_total_amount: { name: '历史总消费额', window: '全量' },
  user_avg_order_amount: { name: '平均订单金额', window: '全量' },
  user_max_order_amount: { name: '最大单笔订单金额', window: '全量' },
  user_refund_count: { name: '退款总次数', window: '全量' },
  user_postsale_count: { name: '售后总次数', window: '全量' },
  user_refund_rate: { name: '退款率', window: '全量' },
  user_postsale_rate: { name: '售后率', window: '全量' },
  user_refund_amount: { name: '退款总金额', window: '全量' },
  user_cancel_count: { name: '取消订单次数', window: '全量' },
  user_complaint_count: { name: '投诉次数', window: '全量' },
  user_address_count: { name: '收货地址数量', window: '全量' },
  // 订单维度 · 8
  order_total_amount: { name: '订单总金额', window: '单笔' },
  order_item_count: { name: '明细行数', window: '单笔' },
  order_sku_count: { name: '商品总件数', window: '单笔' },
  order_discount_amount: { name: '折扣总额', window: '单笔' },
  order_discount_rate: { name: '折扣率', window: '单笔' },
  order_pay_interval_sec: { name: '下单到支付间隔(秒)', window: '单笔' },
  order_is_night: { name: '是否夜间下单', window: '单笔' },
  order_category_count: { name: '商品类别数', window: '单笔' },
  // 地址维度 · 3
  addr_total_count: { name: '地址总数', window: '全量' },
  addr_province_count: { name: '不同省份数', window: '全量' },
  addr_is_new: { name: '是否新地址', window: '单笔' },
  // 设备与团伙维度 · 8
  device_account_cnt_24h: { name: '同设备关联账号数', window: '24h' },
  device_order_cnt_1h: { name: '同设备下单次数', window: '1h' },
  device_coupon_cnt_1h: { name: '同设备领券次数', window: '1h' },
  ip_account_cnt_24h: { name: '同IP注册账号数', window: '24h' },
  ip_order_cnt_1h: { name: '同IP下单次数', window: '1h' },
  addr_account_cnt_7d: { name: '同地址关联账号数', window: '7d' },
  phone_refund_rate_24h: { name: '同手机号退款率', window: '24h' },
  user_event_cnt_1h: { name: '单用户1小时事件总数', window: '1h' },
}

/** 被规则命中文本引用到的特征编码集合（按词边界匹配，避免子串误判） */
const hitCodes = computed(() => {
  const text = props.ruleHits
    .map((h) => `${h.trigger_reason || ''} ${h.reason || ''}`)
    .join(' ')
  const set = new Set()
  if (!text.trim()) return set
  Object.keys(META).forEach((code) => {
    const re = new RegExp(`(^|[^A-Za-z0-9_])${code}([^A-Za-z0-9_]|$)`)
    if (re.test(text)) set.add(code)
  })
  return set
})

function buildRow(code) {
  const meta = META[code] || {}
  return {
    code,
    name: meta.name || '',
    window: meta.window || '',
    current: props.features?.[code],
    baseline: props.baseline?.[code],
    hit: hitCodes.value.has(code),
  }
}

/** 按 4 个维度分组的行数据 */
const groups = computed(() =>
  DIMENSIONS.map((d) => {
    const rows = d.codes.map(buildRow)
    return {
      key: d.key,
      label: d.label,
      total: rows.length,
      hitCount: rows.filter((r) => r.hit).length,
      rows,
    }
  }),
)

/** 平铺行（groupByDimension = false 时使用） */
const flatRows = computed(() => groups.value.flatMap((g) => g.rows))

/** 整数原样，浮点保留 2 位；缺失显示 - */
function fmt(v) {
  if (v === undefined || v === null || v === '') return '-'
  const n = typeof v === 'number' ? v : Number(v)
  if (Number.isNaN(n)) return String(v)
  return Number.isInteger(n) ? String(n) : n.toFixed(2)
}
</script>

<template>
  <!-- 按 4 个维度分组 -->
  <template v-if="groupByDimension">
    <template v-for="g in groups" :key="g.key">
      <div class="sec-title">
        <span>{{ g.label }} · {{ g.total }} 个</span>
        <span class="muted">命中 {{ g.hitCount }}</span>
      </div>
      <table class="tb ft">
        <thead>
          <tr>
            <th>特征（窗口）</th>
            <th class="num">当前值</th>
            <th class="num">历史基线</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in g.rows" :key="row.code" :class="{ hit: row.hit }">
            <td class="k">
              <span class="mono" style="font-size:11.5px">{{ row.code }}</span>
              <div class="cell-sub">{{ row.name }} · {{ row.window }}</div>
            </td>
            <td class="v">{{ fmt(row.current) }}<span v-if="row.hit"> ●</span></td>
            <td class="v" style="color:var(--text3)">{{ fmt(row.baseline) }}</td>
          </tr>
        </tbody>
      </table>
    </template>
  </template>

  <!-- 平铺（不分组） -->
  <table v-else class="tb ft">
    <thead>
      <tr>
        <th>特征（窗口）</th>
        <th class="num">当前值</th>
        <th class="num">历史基线</th>
      </tr>
    </thead>
    <tbody>
      <tr v-for="row in flatRows" :key="row.code" :class="{ hit: row.hit }">
        <td class="k">
          <span class="mono" style="font-size:11.5px">{{ row.code }}</span>
          <div class="cell-sub">{{ row.name }} · {{ row.window }}</div>
        </td>
        <td class="v">{{ fmt(row.current) }}<span v-if="row.hit"> ●</span></td>
        <td class="v" style="color:var(--text3)">{{ fmt(row.baseline) }}</td>
      </tr>
    </tbody>
  </table>
</template>
