<script setup lang="ts">
/** 报告中心（M5）：已生成报告的列表与 HTML/Word/Excel 下载。 */
import { onMounted, ref } from 'vue'
import { api } from '../sse'
import { useToast } from '../toast'
import MdButton from '../components/MdButton.vue'
import MdCard from '../components/MdCard.vue'
import MdEmptyState from '../components/MdEmptyState.vue'
import MdIcon from '../components/MdIcon.vue'

const { show } = useToast()
const reports = ref<any[]>([])
const loading = ref(true)

async function refresh() {
  loading.value = true
  try {
    const data = await api.reports()
    reports.value = data.reports || []
  } catch (e: any) {
    show(e.message || '报告列表加载失败', 'error')
  } finally {
    loading.value = false
  }
}

function openFile(reportId: string, format: string) {
  window.open(api.reportFileUrl(reportId, format), '_blank')
}

onMounted(refresh)
</script>

<template>
  <div class="reports-view">
    <div class="intro">
      <div>
        <h2>报告中心</h2>
        <p class="muted">报告中的数字与图表均带数据集 sha256、result_hash 与图表 spec_hash，可回溯。</p>
      </div>
      <MdButton variant="text" icon="refresh" small :loading="loading" @click="refresh">刷新</MdButton>
    </div>

    <div v-if="loading && !reports.length" class="skeleton" style="height: 180px" />

    <MdEmptyState v-else-if="!reports.length" icon="report" title="还没有报告"
                  description="到「数据分析」页运行分析，点“加入报告”，再生成 HTML/Word/Excel" />

    <div v-else class="report-list">
      <MdCard v-for="item in reports" :key="item.report_id" variant="elevated" class="report-item">
        <div class="report-item__head">
          <div class="report-item__title">
            <MdIcon name="report" :size="20" />
            <div>
              <h3>{{ item.title }}</h3>
              <p v-if="item.subtitle" class="muted">{{ item.subtitle }}</p>
            </div>
          </div>
          <span class="chip num">{{ item.report_id }}</span>
        </div>

        <div class="report-item__meta">
          <span class="chip">生成 {{ item.created_at }}</span>
          <span class="chip">{{ item.block_count }} 个块</span>
          <span class="chip">{{ item.dataset_count }} 个数据集</span>
          <span v-if="item.audit_status" :class="['chip', item.audit_status === 'blocked' ? 'chip--bad' : 'chip--ok']">
            审计 {{ item.audit_status === 'passed' ? '通过' : '阻断' }}
          </span>
          <span v-if="item.claims_failed" class="chip chip--bad">缺证据 {{ item.claims_failed }}</span>
          <span class="chip num">spec {{ item.spec_hash }}</span>
        </div>

        <div v-if="item.files?.length" class="report-item__files">
          <MdButton v-for="file in item.files" :key="file.format" variant="outlined"
                    icon="monitor" small @click="openFile(item.report_id, file.format)">
            下载 {{ file.format.toUpperCase() }} · {{ (file.bytes / 1024).toFixed(1) }} KB
          </MdButton>
        </div>
        <p v-else class="muted">审计未通过，暂无可下载文件；请到「审计」页查看缺证据数字。</p>
      </MdCard>
    </div>
  </div>
</template>

<style scoped>
.reports-view {
  flex: 1; min-height: 0; overflow-y: auto;
  padding: var(--space-4) var(--space-5) var(--space-6);
  display: flex; flex-direction: column; gap: var(--space-3);
}
.intro { display: flex; align-items: flex-start; justify-content: space-between; gap: var(--space-3); }
.intro h2 { margin: 0 0 4px; font-size: 18px; }
.intro p { margin: 0; font-size: 12px; }
.muted { color: var(--text-muted); font-size: 12px; }
.chip--ok { color: var(--down); }
.chip--bad { color: var(--md-error); }
.report-list { display: flex; flex-direction: column; gap: var(--space-3); }
.report-item { display: flex; flex-direction: column; gap: var(--space-3); }
.report-item__head { display: flex; align-items: flex-start; justify-content: space-between; gap: var(--space-3); }
.report-item__title { display: flex; align-items: flex-start; gap: var(--space-2); color: var(--md-primary); }
.report-item__title h3 { margin: 0 0 2px; font-size: 15px; color: var(--text-primary); }
.report-item__title p { margin: 0; }
.report-item__meta { display: flex; flex-wrap: wrap; gap: var(--space-2); }
.chip {
  display: inline-flex; align-items: center; padding: 3px 10px;
  border-radius: var(--md-radius-full); background: var(--md-surface-container);
  color: var(--text-secondary); font-size: 11px;
}
.report-item__files { display: flex; flex-wrap: wrap; gap: var(--space-2); }
.skeleton { border-radius: var(--md-radius-l); background: var(--md-surface-container); animation: pulse 1.4s ease-in-out infinite; }
@keyframes pulse { 0%, 100% { opacity: 0.55; } 50% { opacity: 0.9; } }
@media (max-width: 768px) {
  .reports-view { padding: var(--space-3); }
  .report-item__head { flex-direction: column; }
}
</style>
