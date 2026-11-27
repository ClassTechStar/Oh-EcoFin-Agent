<script setup lang="ts">
/** 成本看板视图：今日成本 / 熔断进度 / 分模型聚合表（MD3 卡片 + 进度 + 数据表） */
import { onMounted, ref } from 'vue'
import { api } from '../sse'
import MdCard from '../components/MdCard.vue'
import MdProgress from '../components/MdProgress.vue'
import MdButton from '../components/MdButton.vue'
import MdIcon from '../components/MdIcon.vue'
import MdEmptyState from '../components/MdEmptyState.vue'
import { useToast } from '../toast'

const summary = ref<any>(null)
const loading = ref(true)
const { show } = useToast()

async function refresh() {
  loading.value = true
  try { summary.value = await api.costSummary() }
  catch { show('成本数据加载失败', 'error') }
  finally { loading.value = false }
}
onMounted(refresh)

function yuan(cents: number | undefined): string {
  return `¥${((cents ?? 0) / 100).toFixed(2)}`
}
const usage = () => (summary.value ? Math.min(summary.value.today_cents / summary.value.cap_cents, 1) : 0)
const fuseOpen = () => !!summary.value?.fuse_open
</script>

<template>
  <div class="cost">
    <div v-if="loading && !summary" class="cost__grid">
      <div class="skeleton" style="height: 120px" />
      <div class="skeleton" style="height: 120px" />
      <div class="skeleton" style="height: 260px" />
    </div>

    <template v-else-if="summary">
      <div class="cost__grid">
        <!-- 今日成本 -->
        <MdCard variant="elevated" class="stat">
          <div class="stat__label"><MdIcon name="dashboard" :size="18" /> 今日 LLM 成本</div>
          <div class="stat__value num" :class="{ 'pct-up': fuseOpen() }">{{ yuan(summary.today_cents) }}</div>
          <div class="stat__sub">日熔断上限 {{ yuan(summary.cap_cents) }}（config.yaml llm.daily_cost_cap_cny）</div>
          <MdProgress type="linear" :value="usage()" />
          <div class="stat__foot" :class="{ 'pct-up': fuseOpen() }">
            {{ fuseOpen() ? '⚠️ 成本熔断已触发：新调用被拒绝' : `已用 ${(usage() * 100).toFixed(1)}%` }}
          </div>
        </MdCard>

        <!-- 预算说明 -->
        <MdCard variant="filled" class="stat">
          <div class="stat__label"><MdIcon name="info" :size="18" /> 三层成本控制</div>
          <ul class="stat__list">
            <li><strong>tier 路由</strong>：辩论/摘要走 economy 缓存价（省钱）</li>
            <li><strong>缓存复用</strong>：新闻 30min / K线 1h（免钱）</li>
            <li><strong>日熔断</strong>：超限即拒绝新调用（兜底）</li>
          </ul>
          <div class="stat__sub">逐次调用明细在 llm_calls 表，导出见 scripts/export_cost.py</div>
        </MdCard>

        <!-- 分模型聚合 -->
        <MdCard variant="outlined" class="table-card">
          <div class="table-head">
            <h3>分模型聚合</h3>
            <MdButton variant="text" icon="refresh" small @click="refresh">刷新</MdButton>
          </div>
          <MdEmptyState v-if="!summary.by_model?.length" icon="info"
                        title="暂无调用记录" description="发起一次对话后这里会出现分模型 token 与成本明细" />
          <table v-else class="cost-table num">
            <thead>
              <tr><th>模型</th><th>调用</th><th>输入 tok</th><th>输出 tok</th><th>缓存 tok</th><th>成本</th></tr>
            </thead>
            <tbody>
              <tr v-for="row in summary.by_model" :key="row.model + row.tier">
                <td>{{ row.model }}<span class="tier-tag">{{ row.tier }}</span></td>
                <td>{{ row.calls }}</td>
                <td>{{ row.tokens_in }}</td>
                <td>{{ row.tokens_out }}</td>
                <td>{{ row.cached_tokens }}</td>
                <td class="pct-up">{{ yuan(row.cost_cents) }}</td>
              </tr>
            </tbody>
          </table>
        </MdCard>
      </div>
      <p class="cost__disclaimer">{{ summary.disclaimer }}</p>
    </template>
  </div>
</template>

<style scoped>
.cost { flex: 1; overflow-y: auto; overscroll-behavior: none; padding: var(--space-4); }
.cost__grid {
  max-width: 920px; margin: 0 auto;
  display: grid; grid-template-columns: 1fr 1fr; gap: var(--space-4);
}
.table-card { grid-column: 1 / -1; }
.stat { display: flex; flex-direction: column; gap: var(--space-3); }
.stat__label { display: flex; align-items: center; gap: 8px; color: var(--text-secondary); font-size: 13px; font-weight: 600; }
.stat__value { font-size: 40px; line-height: 1.1; font-weight: 700; }
.stat__sub { font-size: var(--type-label); color: var(--text-muted); }
.stat__foot { font-size: 12px; color: var(--text-secondary); }
.stat__list { margin: 0; padding-left: 18px; display: flex; flex-direction: column; gap: 6px; font-size: 13px; color: var(--text-secondary); }
.table-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: var(--space-3); }
.table-head h3 { margin: 0; font-size: var(--type-title); }
.cost-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.cost-table th { text-align: right; color: var(--text-muted); font-weight: 500; padding: 8px; border-bottom: 1px solid var(--border-default); }
.cost-table th:first-child, .cost-table td:first-child { text-align: left; }
.cost-table td { padding: 9px 8px; border-bottom: 1px solid var(--border-subtle); text-align: right; }
.tier-tag {
  margin-left: 8px; padding: 1px 7px;
  font-size: 10px; border-radius: var(--md-radius-full);
  background: var(--accent-50); color: var(--accent);
}
.cost__disclaimer { max-width: 920px; margin: var(--space-4) auto 0; text-align: center; font-size: 11px; color: var(--text-muted); }
@media (max-width: 768px) {
  .cost__grid { grid-template-columns: 1fr; }
  .cost-table { font-size: 12px; }
}
</style>
