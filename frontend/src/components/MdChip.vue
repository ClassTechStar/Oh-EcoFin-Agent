<script setup lang="ts">
/** MD3 Chip（assist/filter）：胶囊形、outlineVariant 描边、选中态 secondaryContainer。
 *  高度 32px（MD3 chip 规格）。 */
import MdIcon from './MdIcon.vue'

const props = withDefaults(defineProps<{
  selected?: boolean
  disabled?: boolean
  icon?: string
}>(), { selected: false, disabled: false, icon: '' })

const emit = defineEmits<{ (e: 'click', ev: MouseEvent): void }>()
function onClick(ev: MouseEvent) { if (!props.disabled) emit('click', ev) }
</script>

<template>
  <button :class="['md-chip', { 'md-chip--selected': selected }]" :disabled="disabled" @click="onClick">
    <MdIcon v-if="icon" :name="icon" :size="16" />
    <span class="md-chip__label"><slot /></span>
    <MdIcon v-if="selected" name="check" :size="15" />
  </button>
</template>

<style scoped>
.md-chip {
  display: inline-flex; align-items: center; gap: 6px;
  height: 32px; padding: 0 12px;
  border-radius: var(--md-radius-full);
  border: 1px solid var(--md-outline-variant);
  background: transparent; color: var(--md-on-surface-variant);
  font-size: 13px; font-weight: 500;
  user-select: none; -webkit-tap-highlight-color: transparent;
  transition: background-color var(--motion-fade-in), border-color var(--motion-fade-in),
              color var(--motion-fade-in), transform var(--motion-press-down);
}
.md-chip:hover:not(:disabled) { background: var(--accent-50); color: var(--text-primary); }
.md-chip:active:not(:disabled) { transform: scale(0.97); }
.md-chip--selected {
  background: var(--md-secondary-container); color: var(--md-on-secondary-container);
  border-color: transparent;
}
.md-chip:disabled { opacity: 0.45; }
.md-chip__label { white-space: nowrap; }
</style>
