<script setup lang="ts">
/** MD3 卡片：elevated / filled / outlined 三变体。
 *  - 圆角 large(16)，内边距 20（CyreneCompass setting-card 节奏）
 *  - elevated: surfaceContainerLowest + elev-1；filled: surfaceContainerLow；
 *    outlined: surface + outlineVariant 描边
 */
withDefaults(defineProps<{ variant?: 'elevated' | 'filled' | 'outlined'; pad?: boolean }>(),
  { variant: 'filled', pad: true })
</script>

<template>
  <div :class="['md-card-comp', `md-card-comp--${variant}`, { 'md-card-comp--pad': pad }]">
    <slot />
  </div>
</template>

<style scoped>
.md-card-comp { border-radius: var(--md-radius-l); transition: box-shadow var(--motion-settle); }
.md-card-comp--pad { padding: var(--space-5); }
.md-card-comp--elevated { background: var(--md-surface-container-lowest); box-shadow: var(--md-elev-1); }
.md-card-comp--filled { background: var(--bg-card); }
.md-card-comp--outlined { background: var(--md-surface); border: 1px solid var(--border-default); }
.md-card-comp--elevated:hover { box-shadow: var(--md-elev-2); }
</style>
