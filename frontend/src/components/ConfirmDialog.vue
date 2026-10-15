<script setup>
/**
 * 通用二次确认弹窗（FR-30 / SPEC §8.3 ConfirmDialog）。
 *
 * ⚠ 必须列出即将执行的具体动作清单（SPEC §7.3 交互约定），不能只问「确定吗」。
 */
defineProps({
  visible: { type: Boolean, default: false },
  title: { type: String, default: '确认执行' },
  /** 具体动作清单，逐条展示 */
  actions: { type: Array, default: () => [] },
  /** 额外提示（如不可撤销） */
  warning: { type: String, default: '' },
  confirmText: { type: String, default: '确认执行' },
  danger: { type: Boolean, default: true },
  loading: { type: Boolean, default: false },
})

const emit = defineEmits(['confirm', 'cancel'])
</script>

<template>
  <div v-if="visible" class="modal-mask" @click.self="emit('cancel')">
    <div class="modal-box">
      <div class="modal-hd">
        <span>{{ title }}</span>
        <span class="muted" style="font-size:16px;cursor:pointer" @click="emit('cancel')">×</span>
      </div>

      <div class="modal-bd">
        <div class="muted" style="margin-bottom:12px">即将执行以下动作，请确认：</div>

        <div style="background:#fafbfc;border:1px solid var(--border);border-radius:6px;padding:13px 15px">
          <div v-if="actions.length" style="font-size:12.5px;line-height:2.1;color:var(--text2)">
            <div v-for="(a, i) in actions" :key="i">· {{ a }}</div>
          </div>
          <div v-else class="muted">（未提供动作清单）</div>
        </div>

        <div v-if="warning" class="alert alert-warn" style="margin-top:13px">
          <div>{{ warning }}</div>
        </div>
      </div>

      <div class="modal-ft">
        <button class="btn" :disabled="loading" @click="emit('cancel')">取消</button>
        <button
          class="btn"
          :class="danger ? 'btn-danger' : 'btn-primary'"
          :disabled="loading"
          @click="emit('confirm')"
        >
          {{ loading ? '执行中…' : confirmText }}
        </button>
      </div>
    </div>
  </div>
</template>
