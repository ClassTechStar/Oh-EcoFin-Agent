<script setup lang="ts">
/** MD3 Switch：56×32 轨道，thumb 24→40 拖伸动效（PressDown 档），选中 primary。 */
const props = withDefaults(defineProps<{ modelValue: boolean; disabled?: boolean; label?: string }>(),
  { disabled: false, label: '' })
const emit = defineEmits<{ (e: 'update:modelValue', v: boolean): void }>()
function toggle() { if (!props.disabled) emit('update:modelValue', !props.modelValue) }
</script>

<template>
  <button :class="['md-switch', { 'md-switch--on': modelValue, 'md-switch--disabled': disabled }]"
          role="switch" :aria-checked="modelValue" :aria-label="label || '开关'" :disabled="disabled"
          @click="toggle">
    <span class="md-switch__thumb" />
  </button>
</template>

<style scoped>
.md-switch {
  position: relative; width: 56px; height: 32px; flex: 0 0 auto;
  border-radius: var(--md-radius-full);
  background: var(--md-surface-container-highest);
  border: 2px solid var(--md-outline);
  transition: background-color var(--motion-settle), border-color var(--motion-settle);
}
.md-switch--on { background: var(--md-primary); border-color: var(--md-primary); }
.md-switch--disabled { opacity: 0.45; }
.md-switch__thumb {
  position: absolute; left: 4px; top: 50%;
  width: 16px; height: 16px; margin-top: -8px;
  border-radius: 50%; background: var(--md-outline);
  transition: left var(--motion-settle), width var(--motion-press-down), height var(--motion-press-down),
              background-color var(--motion-settle);
}
.md-switch--on .md-switch__thumb { left: 22px; width: 24px; height: 24px; margin-top: -12px; background: var(--md-on-primary); }
.md-switch:not(.md-switch--on):hover .md-switch__thumb { width: 18px; height: 18px; }
</style>
