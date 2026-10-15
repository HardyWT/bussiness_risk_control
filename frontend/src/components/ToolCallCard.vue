<script setup>
/**
 * AI 工具调用卡片（SPEC §8.3 / R-FE2-18）。
 *
 * 展示单个工具的调用过程：工具名、入参、耗时、结果摘要（可折叠展开）。
 */
import { computed, ref } from 'vue'

const props = defineProps({
  toolName: { type: String, required: true },
  args: { type: Object, default: () => ({}) },
  /** 后端返回的是 result_summary 字符串（已截断） */
  resultSummary: { type: String, default: '' },
  costMs: { type: Number, default: 0 },
  status: { type: String, default: 'ok' },
})

const open = ref(false)

const ok = computed(() => props.status === 'ok')

/** 入参摘要：k=v 拼接，过长截断 */
const argsText = computed(() => {
  const entries = Object.entries(props.args || {})
  if (!entries.length) return '（无参数）'
  const text = entries.map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(', ')
  return text.length > 60 ? `${text.slice(0, 59)}…` : text
})

/** 尝试把结果摘要格式化为 JSON，失败则原样展示 */
const prettyResult = computed(() => {
  if (!props.resultSummary) return '（无返回）'
  try {
    return JSON.stringify(JSON.parse(props.resultSummary), null, 2)
  } catch {
    return props.resultSummary
  }
})
</script>

<template>
  <div class="tool-card">
    <div class="tool-hd" @click="open = !open">
      <span :style="{ color: ok ? 'var(--success-text)' : 'var(--danger-text)', fontWeight: 600 }">
        {{ ok ? '✓' : '✗' }}
      </span>
      <span class="nm">{{ toolName }}</span>
      <span class="muted">{{ argsText }}</span>
      <span v-if="costMs" class="muted">· {{ costMs }}ms</span>
      <span class="muted" style="margin-left:auto">{{ open ? '收起 ▴' : '展开 ▾' }}</span>
    </div>
    <div v-if="open" class="tool-bd">{{ prettyResult }}</div>
  </div>
</template>

<style scoped>
.tool-card {
  margin-top: 10px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: #fafbfc;
  overflow: hidden;
}
.tool-hd {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 12px;
  font-size: 12px;
  color: var(--text2);
  cursor: pointer;
  background: #f5f7fa;
  border-bottom: 1px solid var(--border2);
}
.tool-hd .nm {
  font-family: var(--mono);
  color: var(--purple);
  font-weight: 600;
}
.tool-bd {
  padding: 10px 12px;
  font-family: var(--mono);
  font-size: 11.5px;
  line-height: 1.8;
  color: var(--text2);
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 260px;
  overflow: auto;
}
</style>
