<script setup>
/**
 * 统一标签：五档决策 / 风险等级 / 案件状态 / 名单类型 / 命中来源。
 *
 * 颜色语义固定（SPEC §8 颜色约定 / R-FE2-19）：
 *   深红 = 极高·拒绝，橙 = 高·人审，黄 = 中·标记，绿 = 低·放行，蓝 = 信息。
 * ⚠ 这是风控后台语义，与股票涨跌的红绿约定无关。
 */
import { computed } from 'vue'

const props = defineProps({
  /**
   * decision : Pass / Mark / Challenge / Review / Reject       （五档决策）
   * level    : low / medium / high / critical                  （风险等级四档）
   * status   : pending / reviewing / disposed / closed / archived（案件五态）
   * listType : black / white / gray                            （名单类型）
   * hitSource: whitelist / blacklist / gray / rule             （命中来源）
   */
  kind: { type: String, default: 'decision' },
  value: { type: String, required: true },
  dot: { type: Boolean, default: false },
})

const MAPS = {
  decision: {
    Pass: { cls: 'tag-green', text: 'Pass 放行', dot: 'dot-green' },
    Mark: { cls: 'tag-yellow', text: 'Mark 标记观察', dot: 'dot-yellow' },
    Challenge: { cls: 'tag-yellow', text: 'Challenge 挑战', dot: 'dot-yellow' },
    Review: { cls: 'tag-orange', text: 'Review 人审', dot: 'dot-orange' },
    Reject: { cls: 'tag-red', text: 'Reject 拦截', dot: 'dot-red' },
  },
  level: {
    low: { cls: 'tag-green', text: '低', dot: 'dot-green' },
    medium: { cls: 'tag-yellow', text: '中', dot: 'dot-yellow' },
    high: { cls: 'tag-orange', text: '高', dot: 'dot-orange' },
    // 极高：使用 tag-red + 深红文字，突出最严重档（SPEC §8 颜色约定）
    critical: { cls: 'tag-red', text: '极高', style: 'color:#cf1322', dot: 'dot-red' },
  },
  status: {
    pending: { cls: 'tag-orange', text: '待审核', dot: 'dot-orange' },
    reviewing: { cls: 'tag-blue', text: '审核中', dot: 'dot-blue' },
    disposed: { cls: 'tag-green', text: '已处置', dot: 'dot-green' },
    closed: { cls: 'tag-gray', text: '已关闭', dot: 'dot-gray' },
    archived: { cls: 'tag-gray', text: '已归档', dot: 'dot-gray' },
  },
  listType: {
    black: { cls: 'tag-red', text: '黑名单', dot: 'dot-red' },
    white: { cls: 'tag-green', text: '白名单', dot: 'dot-green' },
    gray: { cls: 'tag-gray', text: '灰名单', dot: 'dot-gray' },
  },
  hitSource: {
    whitelist: { cls: 'tag-green', text: '白名单', dot: 'dot-green' },
    blacklist: { cls: 'tag-red', text: '黑名单', dot: 'dot-red' },
    gray: { cls: 'tag-gray', text: '灰名单', dot: 'dot-gray' },
    rule: { cls: 'tag-blue', text: '规则引擎', dot: 'dot-blue' },
  },
}

const meta = computed(
  () => MAPS[props.kind]?.[props.value] || { cls: 'tag-gray', text: props.value, dot: 'dot-gray' },
)
</script>

<template>
  <span class="tag" :class="meta.cls" :style="meta.style">
    <span v-if="dot" class="dot" :class="meta.dot" style="margin-right:2px" />
    {{ meta.text }}
  </span>
</template>
