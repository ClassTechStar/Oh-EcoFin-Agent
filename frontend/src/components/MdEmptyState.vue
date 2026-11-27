<script setup lang="ts">
/** MD3 空态/加载骨架（CyreneCompass inspector-empty 交互模式 + MD3 色彩） */
import MdIcon from './MdIcon.vue'

withDefaults(defineProps<{
  icon?: string
  title: string
  description?: string
}>(), { icon: 'info', description: '' })
</script>

<template>
  <div class="md-empty">
    <div class="md-empty__icon"><MdIcon :name="icon" :size="40" /></div>
    <p class="md-empty__title">{{ title }}</p>
    <p v-if="description" class="md-empty__desc">{{ description }}</p>
    <div class="md-empty__actions"><slot /></div>
  </div>
</template>

<style scoped>
.md-empty {
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: var(--space-3);
  min-height: 180px; padding: var(--space-8) var(--space-5);
  text-align: center;
}
.md-empty__icon { color: var(--md-on-surface-variant); opacity: 0.6; }
.md-empty__title { margin: 0; font-size: var(--type-body-lg); color: var(--text-secondary); }
.md-empty__desc { margin: 0; font-size: var(--type-label); color: var(--text-muted); max-width: 420px; }
.md-empty__actions { display: flex; gap: var(--space-2); margin-top: var(--space-2); }
</style>
