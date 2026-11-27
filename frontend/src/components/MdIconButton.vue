<script setup lang="ts">
/** MD3 图标按钮（mochi MochiButtonDefaults.BarItemSize=44 / IconSize=48）：
 *  bar 变体 44px 用于顶栏（onSurface 内容 + 底色不变禁用），默认 48px 圆形触摸目标。 */
import MdIcon from './MdIcon.vue'

const props = withDefaults(defineProps<{
  icon: string
  label: string
  size?: number | string
  variant?: 'standard' | 'bar'
  disabled?: boolean
}>(), { variant: 'standard', disabled: false })

const emit = defineEmits<{ (e: 'click', ev: MouseEvent): void }>()
function onClick(ev: MouseEvent) { if (!props.disabled) emit('click', ev) }
</script>

<template>
  <button :class="['md-icon-btn', `md-icon-btn--${variant}`]" :disabled="disabled"
          :aria-label="label" :title="label" @click="onClick">
    <MdIcon :name="icon" :size="size || 22" />
  </button>
</template>

<style scoped>
.md-icon-btn {
  display: grid; place-items: center;
  border-radius: var(--md-radius-full);
  color: var(--text-secondary);
  transition: background-color var(--motion-fade-in), color var(--motion-fade-in),
              transform var(--motion-press-down);
  -webkit-tap-highlight-color: transparent;
}
.md-icon-btn:hover:not(:disabled) { background: var(--bg-hover); color: var(--accent); }
.md-icon-btn:active:not(:disabled) { transform: scale(0.95); }
.md-icon-btn:disabled { color: var(--md-disabled-content); }
.md-icon-btn--standard { width: 48px; height: 48px; }
.md-icon-btn--bar { width: 44px; height: 44px; background: var(--bg-card-solid); color: var(--text-primary); }
</style>
