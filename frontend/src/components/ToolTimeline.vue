<script setup lang="ts">
/** 工具调用时间线：折叠面板（名称/参数/耗时/结果摘要，展开看原始返回）。 */

function statusIcon(s: string): string {
  if (s === 'start') return '⏳'
  if (s === 'error') return '❌'
  return '✅'
}

function shortArgs(args: string): string {
  try {
    const o = JSON.parse(args)
    return Object.values(o).map(v => String(v)).join(' · ').slice(0, 60)
  } catch { return (args || '').slice(0, 60) }
}

function pretty(t: any): string {
  return `▶ ${t.name}(${t.args || '{}'})` + (t.result_digest ? `\n◀ ${t.result_digest}` : '')
}

defineProps<{ tools: any[] }>()
</script>

<template>
  <div class="timeline">
    <details v-for="t in tools" :key="t.id + t.name" class="tool-call">
      <summary>
        <span>{{ statusIcon(t.status) }}</span>
        <strong>{{ t.name }}</strong>
        <span class="args num">{{ shortArgs(t.args) }}</span>
        <span v-if="t.ms != null" class="num">{{ t.ms }}ms</span>
        <span v-if="t.cached" class="cached">缓存</span>
      </summary>
      <div class="raw">{{ pretty(t) }}</div>
    </details>
  </div>
</template>

<style scoped>
.timeline { display: flex; flex-direction: column; gap: 2px; }
.args { color: var(--text-3); flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.cached { color: var(--ok); font-size: 11px; }
</style>
