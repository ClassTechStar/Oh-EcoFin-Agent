<script setup lang="ts">
/** MD3 进度：linear（熔断用量条）与 ring（快照刷新中）。 */
withDefaults(defineProps<{
  type?: 'linear' | 'ring'
  value?: number          // 0..1；ring 无值 = 不确定态旋转
  size?: number
}>(), { type: 'linear', value: undefined, size: 24 })
</script>

<template>
  <div v-if="type === 'linear'" class="md-linear" role="progressbar"
       :aria-valuenow="value != null ? Math.round(value * 100) : undefined">
    <div class="md-linear__bar" :style="{ width: `${Math.min((value ?? 0) * 100, 100)}%` }" />
  </div>
  <svg v-else :width="size" :height="size" viewBox="0 0 24 24" class="md-ring"
       :class="{ 'md-ring--indeterminate': value == null }" role="progressbar">
    <circle class="md-ring__track" cx="12" cy="12" r="9" fill="none" stroke-width="3" />
    <circle class="md-ring__head" cx="12" cy="12" r="9" fill="none" stroke-width="3"
            :stroke-dasharray="value != null ? `${value * 56.5} 56.5` : '13 44'"
            stroke-linecap="round" />
  </svg>
</template>

<style scoped>
.md-linear {
  height: 6px; width: 100%;
  border-radius: var(--md-radius-full);
  background: var(--md-surface-container-high);
  overflow: hidden;
}
.md-linear__bar {
  height: 100%; border-radius: inherit;
  background: var(--md-primary);
  transition: width var(--motion-settle);
}
.md-ring { display: inline-block; }
.md-ring__track { stroke: var(--md-surface-container-highest); }
.md-ring__head { stroke: var(--md-primary); transform-origin: center; }
.md-ring--indeterminate .md-ring__head { animation: ring-spin 0.9s linear infinite; }
@keyframes ring-spin { to { transform: rotate(360deg); } }
</style>
