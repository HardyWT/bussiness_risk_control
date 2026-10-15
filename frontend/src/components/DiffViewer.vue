<script setup>
/**
 * 审计变更前后 diff（FR-52 / SPEC §8.3 DiffViewer）。
 *
 * before / after 存的是 JSON 字符串（后端 R-MA-04），此处前后并排展示并标出变化字段。
 */
import { computed } from 'vue'

/** props 契约：{ before: Object|null, after: Object|null }（JSON 字符串会被自动解析） */
const props = defineProps({
  before: { type: [Object, Array, String, null], default: null },
  after: { type: [Object, Array, String, null], default: null },
})

/** 若传入 JSON 字符串则解析为对象，保证 diff 与格式化一致 */
function normalize(v) {
  if (typeof v !== 'string') return v
  try {
    return JSON.parse(v)
  } catch {
    return v
  }
}

function pretty(v) {
  const val = normalize(v)
  if (val === null || val === undefined) return '—'
  try {
    return JSON.stringify(val, null, 2)
  } catch {
    return String(val)
  }
}

/** 找出发生变化的顶层字段 */
const changedKeys = computed(() => {
  const b = normalize(props.before)
  const a = normalize(props.after)
  const bObj = b && typeof b === 'object' ? b : {}
  const aObj = a && typeof a === 'object' ? a : {}
  const keys = new Set([...Object.keys(bObj), ...Object.keys(aObj)])
  return [...keys].filter((k) => JSON.stringify(bObj[k]) !== JSON.stringify(aObj[k]))
})
</script>

<template>
  <div>
    <div class="diff">
      <div class="diff-col">
        <div class="h">变更前 before_value</div>
        <pre>{{ pretty(before) }}</pre>
      </div>
      <div class="diff-col">
        <div class="h">变更后 after_value</div>
        <pre>{{ pretty(after) }}</pre>
      </div>
    </div>

    <div v-if="changedKeys.length" class="muted" style="margin-top:10px;font-size:12px">
      变化字段：<span class="mono diff-add">{{ changedKeys.join('、') }}</span>
    </div>
  </div>
</template>

<style scoped>
.diff { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.diff-col { background: #fafbfc; border: 1px solid var(--border); border-radius: 6px; padding: 11px 13px; }
.diff-col .h { font-size: 11.5px; color: var(--text3); margin-bottom: 6px; font-weight: 600; }
.diff-col pre {
  margin: 0; font-family: var(--mono); font-size: 12px; line-height: 1.8;
  color: var(--text2); white-space: pre-wrap; word-break: break-all; max-height: 280px; overflow: auto;
}
</style>
