<script setup lang="ts">
/** ask_user 选项卡片组：挂起式提问（多轮交互通用基础设施）。 */
defineProps<{ ask: { qid: string; question: string; options: string[] } }>()
const emit = defineEmits<{ (e: 'choose', qid: string, choice: string): void }>()
</script>

<template>
  <div class="ask card">
    <div class="q">❓ {{ ask.question }}</div>
    <div class="ask-options">
      <button v-for="(opt, i) in ask.options" :key="i" @click="emit('choose', ask.qid, opt)">
        {{ opt }}<span class="idx">（{{ i + 1 }}）</span>
      </button>
    </div>
  </div>
</template>

<style scoped>
.ask { padding: 12px 14px; border-left: 3px solid var(--accent); }
.q { font-weight: 600; margin-bottom: 10px; }
.idx { color: var(--text-3); font-size: 12px; }
</style>
