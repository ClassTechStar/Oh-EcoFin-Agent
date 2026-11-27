<script setup lang="ts">
/** MD3 按钮（对齐 mochi-compose MochiButtonDefaults）：
 *  - 样式：Filled(primary) / Tonal(secondaryContainer) / Outlined(outlineVariant+primary) / Text(primary)
 *  - 高度 48px、胶囊圆角、内边距 24×12（MochiButtonDefaults.Height/ContentPadding）
 *  - 状态：hover 叠 8% on-color、pressed 叠 12%（Outlined/Text 为 primary@10%）、
 *          disabled 容器 onSurface@12% + 内容 onSurface@38%（mochi 规则）
 *  - 按压形变：PressDown 220ms 压下、PressRelease 380ms 带回弹释放（pressScale 上限 1.03 收敛到 Web）
 */
import { computed } from 'vue'
import MdIcon from './MdIcon.vue'

const props = withDefaults(defineProps<{
  variant?: 'filled' | 'tonal' | 'outlined' | 'text'
  icon?: string
  trailingIcon?: string
  disabled?: boolean
  loading?: boolean
  block?: boolean
  small?: boolean
  type?: 'button' | 'submit'
}>(), {
  variant: 'filled',
  disabled: false,
  loading: false,
  block: false,
  small: false,
  type: 'button',
})

const emit = defineEmits<{ (e: 'click', ev: MouseEvent): void }>()

const cls = computed(() => [
  'md-btn', `md-btn--${props.variant}`,
  { 'md-btn--block': props.block, 'md-btn--small': props.small, 'md-btn--loading': props.loading },
])

function onClick(ev: MouseEvent) {
  if (!props.disabled && !props.loading) emit('click', ev)
}
</script>

<template>
  <button :type="type" :class="cls" :disabled="disabled || loading" @click="onClick">
    <MdIcon v-if="loading" name="refresh" :size="small ? 16 : 20" class="spin" />
    <MdIcon v-else-if="icon" :name="icon" :size="small ? 16 : 20" />
    <span class="md-btn__label"><slot /></span>
    <MdIcon v-if="trailingIcon" :name="trailingIcon" :size="small ? 16 : 20" />
  </button>
</template>

<style scoped>
.md-btn {
  position: relative;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: var(--space-2);
  height: 48px;
  padding: 12px 24px;
  border-radius: var(--md-radius-full);
  font-size: 14px;
  font-weight: 600;
  letter-spacing: 0.1px;
  white-space: nowrap;
  user-select: none;
  -webkit-tap-highlight-color: transparent;
  transition: transform var(--motion-press-down), box-shadow var(--motion-settle),
              background-color var(--motion-fade-in), color var(--motion-fade-in),
              opacity var(--motion-fade-in);
}
.md-btn:active:not(:disabled) { transform: scale(1.02); }
.md-btn--small { height: 36px; padding: 6px 14px; font-size: 13px; gap: 6px; }
.md-btn--block { display: flex; width: 100%; }
.md-btn:disabled { cursor: default; }

.md-btn__label { display: inline-flex; align-items: center; min-width: 0; }

/* Filled：primary/onPrimary */
.md-btn--filled { background: var(--md-primary); color: var(--md-on-primary); }
.md-btn--filled:hover:not(:disabled) { box-shadow: var(--md-elev-1); }
.md-btn--filled:disabled { background: var(--md-disabled-container); color: var(--md-disabled-content); }

/* Tonal：secondaryContainer */
.md-btn--tonal { background: var(--md-secondary-container); color: var(--md-on-secondary-container); }
.md-btn--tonal:hover:not(:disabled) { box-shadow: var(--md-elev-1); }
.md-btn--tonal:disabled { background: var(--md-disabled-container); color: var(--md-disabled-content); }

/* Outlined：透明底 + outlineVariant 描边 + primary 内容；按下 primary@10% */
.md-btn--outlined {
  background: transparent; color: var(--md-primary);
  border: 1px solid var(--md-outline-variant);
}
.md-btn--outlined:hover:not(:disabled) { background: var(--accent-50); }
.md-btn--outlined:disabled { border-color: var(--md-disabled-container); color: var(--md-disabled-content); background: transparent; }

/* Text：primary 内容，按下 primary@10% */
.md-btn--text { background: transparent; color: var(--md-primary); padding-inline: var(--space-3); }
.md-btn--text:hover:not(:disabled) { background: var(--accent-50); }
.md-btn--text:disabled { color: var(--md-disabled-content); background: transparent; }

.spin { animation: spin 0.9s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
</style>
