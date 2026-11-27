<script setup lang="ts">
/** 决策记忆视图：同标的历史研判 + T+N 结算 + 反思文本（"记得上次的错误"证据链） */
import { ref, watch } from 'vue'
import { api } from '../sse'
import MdCard from '../components/MdCard.vue'
import MdChip from '../components/MdChip.vue'
import MdTextField from '../components/MdTextField.vue'
import MdButton from '../components/MdButton.vue'
import MdEmptyState from '../components/MdEmptyState.vue'
import MdIcon from '../components/MdIcon.vue'

const symbol = ref('')
const submitted = ref('')
const decisions = ref<any[]>([])
const loading = ref(false)

async function load(sym: string) {
  if (!/^\d{6}$/.test(sym)) return
  loading.value = true
  submitted.value = sym
  try {
    const r = await api.decisions(sym)
    decisions.value = r.decisions || []
  } catch { decisions.value = [] }
  finally { loading.value = false }
}

function verdictChip(v: string) {
  if (v === 'bullish') return { label: '看多', cls: 'pct-up', icon: 'trend-up' }
  if (v === 'bearish') return { label: '看空', cls: 'pct-down', icon: 'trend-down' }
  return { label: v === 'watch' ? '观望' : '中性', cls: '', icon: 'info' }
}

function fmtReturn(v: any): string {
  if (typeof v !== 'number') return '未结算'
  return `${v >= 0 ? '+' : ''}${(v * 100).toFixed(2)}%`
}

watch(symbol, (v) => { if (/^\d{6}$/.test(v)) load(v) })
</script>

<template>
  <div class="decisions">
    <MdCard variant="filled" class="query">
      <div class="query__row">
        <div class="query__field">
          <MdTextField v-model="symbol" icon="history" label="标的代码"
                       placeholder="输入 6 位代码，如 600519"
                       supporting="查询该标的历史研判、T+N 结算结果与反思教训" @enter="load(symbol)" />
        </div>
        <MdButton icon="refresh" :disabled="!/^\d{6}$/.test(symbol)" @click="load(symbol)">查询</MdButton>
      </div>
    </MdCard>

    <div v-if="loading" class="decisions__list">
      <div class="skeleton" style="height: 110px" />
      <div class="skeleton" style="height: 110px" />
    </div>

    <MdEmptyState v-else-if="!submitted" icon="history"
      title="查询一个标的开始复盘" description="每次多空研判都会落库：标的 / 结论 / 当时数据快照；T+N 个交易日后自动结算盈亏并生成 ≤80 字教训，注入后续分析" />

    <MdEmptyState v-else-if="!decisions.length" icon="info"
      :title="`${submitted} 暂无已结算的决策记录`" description="对该标的发起一次多空研判（辩论模式），5 个交易日后回来看结算与反思" />

    <div v-else class="decisions__list">
      <MdCard v-for="d in decisions" :key="d.id" variant="outlined" class="decision">
        <div class="decision__head">
          <span class="decision__symbol num">{{ d.symbol }}</span>
          <span :class="['decision__verdict', verdictChip(d.verdict).cls]">
            <MdIcon :name="verdictChip(d.verdict).icon" :size="15" /> {{ verdictChip(d.verdict).label }}
          </span>
          <span class="decision__date num">{{ (d.data_as_of || '').slice(0, 10) }}</span>
          <span :class="['decision__return num', (d.settled ?? 0) >= 0 ? 'pct-up' : 'pct-down']">
            {{ fmtReturn(d.settled) }}
          </span>
        </div>
        <p class="decision__thesis">{{ d.thesis_digest }}</p>
        <div v-if="d.reflection" class="decision__reflection">
          <MdIcon name="spark" :size="14" />
          <span>{{ d.reflection }}</span>
        </div>
        <div class="decision__foot num">
          <MdChip icon="dashboard">T+{{ d.horizon_days ?? 5 }} 交易日</MdChip>
          <span v-if="d.settled_at" class="decision__settled">结算日 {{ (d.settled_at || '').slice(0, 10) }}</span>
        </div>
      </MdCard>
    </div>
  </div>
</template>

<style scoped>
.decisions { flex: 1; overflow-y: auto; overscroll-behavior: none; padding: var(--space-4); display: flex; flex-direction: column; gap: var(--space-4); }
.query__row { display: flex; align-items: flex-end; gap: var(--space-3); }
.query__field { flex: 1; min-width: 0; }
.decisions__list {
  max-width: 720px; width: 100%; margin: 0 auto;
  display: flex; flex-direction: column; gap: var(--space-3);
}
.decision { display: flex; flex-direction: column; gap: var(--space-2); }
.decision__head { display: flex; align-items: center; gap: var(--space-3); flex-wrap: wrap; }
.decision__symbol { font-weight: 700; font-size: 16px; }
.decision__verdict { display: inline-flex; align-items: center; gap: 4px; font-weight: 600; font-size: 13px; }
.decision__date { color: var(--text-muted); font-size: 12px; }
.decision__return { margin-left: auto; font-weight: 700; font-size: 18px; }
.decision__thesis { margin: 0; font-size: 13px; color: var(--text-secondary); line-height: 1.6; }
.decision__reflection {
  display: flex; gap: 6px; align-items: flex-start;
  padding: 10px 12px;
  border-radius: var(--md-radius-m);
  background: var(--accent-50); color: var(--accent-dark);
  font-size: 13px; line-height: 1.6;
}
:root[data-theme="dark"] .decision__reflection { color: var(--accent-light); }
.decision__foot { display: flex; align-items: center; gap: var(--space-3); }
.decision__settled { color: var(--text-muted); font-size: 12px; }
@media (max-width: 768px) {
  .decisions { padding: var(--space-3); }
  .query__row { flex-direction: column; align-items: stretch; }
}
</style>
