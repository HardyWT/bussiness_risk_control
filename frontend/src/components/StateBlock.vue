<script setup>
/**
 * 统一状态块：加载中 / 空态 / 错误（T-12 全局状态页补齐）。
 * 用法：<StateBlock :loading="loading" :error="error" :empty="items.length === 0" />
 */
defineProps({
  loading: { type: Boolean, default: false },
  error: { type: String, default: '' },
  empty: { type: Boolean, default: false },
  emptyText: { type: String, default: '暂无数据' },
  loadingText: { type: String, default: '加载中…' },
})
</script>

<template>
  <div v-if="loading" class="loading-mask">
    <div class="spinner" />
    {{ loadingText }}
  </div>

  <div v-else-if="error" class="loading-mask" style="color:var(--danger-text)">
    <div style="font-size:22px;opacity:.5;margin-bottom:6px">!</div>
    {{ error }}
  </div>

  <div v-else-if="empty" class="empty">
    <span class="ic">◇</span>
    {{ emptyText }}
  </div>

  <slot v-else />
</template>
