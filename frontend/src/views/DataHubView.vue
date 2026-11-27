<script setup lang="ts">
/** 数据工作台（M8）：数据集与标的目录两个面板。 */
import { ref } from 'vue'
import DatasetsView from './DatasetsView.vue'
import CatalogView from './CatalogView.vue'

const tab = ref<'datasets' | 'catalog'>('datasets')
</script>

<template>
  <div class="data-hub">
    <div class="tabs" role="tablist" aria-label="数据工作台">
      <button :class="{ active: tab === 'datasets' }" role="tab" :aria-selected="tab === 'datasets'"
              @click="tab = 'datasets'">数据集</button>
      <button :class="{ active: tab === 'catalog' }" role="tab" :aria-selected="tab === 'catalog'"
              @click="tab = 'catalog'">标的目录</button>
    </div>
    <div class="body">
      <DatasetsView v-show="tab === 'datasets'" />
      <CatalogView v-show="tab === 'catalog'" />
    </div>
  </div>
</template>

<style scoped>
.data-hub { flex: 1; min-height: 0; display: flex; flex-direction: column; }
.tabs {
  display: flex; gap: 6px; padding: var(--space-2) var(--space-5) 0;
  flex: 0 0 auto;
}
.tabs button {
  height: 34px; padding: 0 16px; border-radius: var(--md-radius-full);
  background: transparent; color: var(--text-secondary);
  font-size: 13px; font-weight: 600;
  transition: background-color var(--motion-fade-in), color var(--motion-fade-in);
}
.tabs button:hover { background: var(--bg-hover); color: var(--text-primary); }
.tabs button.active { background: var(--md-secondary-container); color: var(--md-on-secondary-container); }
.body { flex: 1; min-height: 0; display: flex; }
.body > * { flex: 1; min-width: 0; }
@media (max-width: 768px) {
  .tabs { padding: var(--space-2) var(--space-3) 0; }
}
</style>
