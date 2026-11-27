<script setup lang="ts">
/** 可信审计（M7）：报告审计列表、证据账本、失败数字与重跑一致性。 */
import { computed, onMounted, ref } from 'vue'
import { api } from '../sse'
import { useToast } from '../toast'
import MdButton from '../components/MdButton.vue'
import MdCard from '../components/MdCard.vue'
import MdEmptyState from '../components/MdEmptyState.vue'
import MdIcon from '../components/MdIcon.vue'
import RunsPanel from '../components/RunsPanel.vue'

const { show } = useToast()
const tab = ref<'reports' | 'runs'>('reports')
const reports = ref<any[]>([])
const details = ref<Record<string, any>>({})
const loading = ref(true)
const loadingId = ref('')
const rerunning = ref('')

const summary = computed(() => ({
  total: reports.value.length,
  passed: reports.value.filter(r => r.audit_status === 'passed').length,
  blocked: reports.value.filter(r => r.audit_status === 'blocked').length,
  failed: reports.value.reduce((n, r) => n + (r.claims_failed || 0), 0)
}))

async function refresh() {
  loading.value = true
  try {
    const data = await api.auditReports()
    reports.value = data.reports || []
  } catch (e: any) {
    show(e.message || '审计列表加载失败', 'error')
  } finally {
    loading.value = false
  }
}

async function toggleDetail(reportId: string) {
  if (details.value[reportId]) {
    delete details.value[reportId]
    return
  }
  loadingId.value = reportId
  try {
    details.value[reportId] = await api.auditReport(reportId)
  } catch (e: any) {
    show(e.message || '审计详情加载失败', 'error')
  } finally {
    loadingId.value = ''
  }
}

async function rerun(reportId: string) {
  rerunning.value = reportId
  try {
    const result = await api.rerunReport(reportId)
    if (result.blocked) {
      show('重跑报告仍被审计阻断', 'error')
    } else {
      show(result.same ? '重跑一致：数字与哈希均相同' : '重跑存在差异，请查看详情')
    }
    await refresh()
    if (details.value[reportId]) delete details.value[reportId]
  } catch (e: any) {
    show(e.message || '重跑失败', 'error')
  } finally {
    rerunning.value = ''
  }
}

function statusLabel(status: string): string {
  if (status === 'passed') return '通过'
  if (status === 'blocked') return '已阻断'
  return status || '未审计'
}

function failedClaims(detail: any): any[] {
  return (detail?.claims || []).filter((c: any) => !c.passed).slice(0, 10)
}

function evidenceSample(detail: any): any[] {
  return (detail?.evidence || []).slice(0, 6)
}

onMounted(refresh)
</script>

<template>
  <div class="audit-view">
    <div class="intro">
      <div>
        <h2>可信审计</h2>
        <p class="muted">证据账本 + 数字比对：报告中的数字必须能回溯到数据单元格/分析结果，缺证据即阻断。</p>
      </div>
      <MdButton variant="text" icon="refresh" small :loading="loading" @click="refresh">刷新</MdButton>
    </div>

    <div class="tabs" role="tablist" aria-label="审计视图">
      <button :class="{ active: tab === 'reports' }" role="tab" :aria-selected="tab === 'reports'"
              @click="tab = 'reports'">报告审计</button>
      <button :class="{ active: tab === 'runs' }" role="tab" :aria-selected="tab === 'runs'"
              @click="tab = 'runs'">运行记录</button>
    </div>

    <template v-if="tab === 'reports'">
    <div class="summary-row">
      <span class="chip">报告 {{ summary.total }}</span>
      <span class="chip chip--ok">通过 {{ summary.passed }}</span>
      <span class="chip chip--bad">阻断 {{ summary.blocked }}</span>
      <span class="chip">缺证据数字 {{ summary.failed }}</span>
    </div>

    <div v-if="loading && !reports.length" class="skeleton" style="height: 180px" />

    <MdEmptyState v-else-if="!reports.length" icon="audit" title="还没有可审计的报告"
                  description="在「数据分析」页生成报告后，这里会显示证据数量、数字比对与重跑结果" />

    <div v-else class="report-list">
      <MdCard v-for="item in reports" :key="item.report_id" variant="elevated" class="report-item">
        <div class="report-item__head">
          <div class="report-item__title">
            <MdIcon name="audit" :size="20" />
            <div>
              <h3>{{ item.title }}</h3>
              <p class="muted num">{{ item.report_id }} · {{ item.created_at }}</p>
            </div>
          </div>
          <span :class="['status', `status--${item.audit_status || 'pending'}`]">
            {{ statusLabel(item.audit_status) }}
          </span>
        </div>

        <div class="report-item__meta">
          <span class="chip">证据 {{ item.evidence_count || 0 }}</span>
          <span class="chip">数字 {{ item.claims_total || 0 }}</span>
          <span :class="['chip', { 'chip--bad': (item.claims_failed || 0) > 0 }]">
            缺证据 {{ item.claims_failed || 0 }}
          </span>
          <span class="chip num">spec {{ item.spec_hash }}</span>
        </div>

        <div class="report-item__actions">
          <MdButton variant="outlined" icon="info" small :loading="loadingId === item.report_id"
                    @click="toggleDetail(item.report_id)">
            {{ details[item.report_id] ? '收起详情' : '审计详情' }}
          </MdButton>
          <MdButton variant="text" icon="refresh" small :loading="rerunning === item.report_id"
                    @click="rerun(item.report_id)">重跑并对比</MdButton>
        </div>

        <div v-if="details[item.report_id]" class="audit-detail">
          <div class="gates">
            <div v-for="gate in details[item.report_id].audit?.gates || []" :key="gate.gate"
                 :class="['gate', `gate--${gate.status}`]">
              <span>{{ gate.gate }}</span>
              <strong>{{ gate.status === 'pass' ? '通过' : '失败' }}</strong>
              <small v-if="gate.detail">{{ gate.detail }}</small>
            </div>
          </div>

          <div v-if="details[item.report_id].audit?.violations?.length" class="violations">
            <div v-for="(v, i) in details[item.report_id].audit.violations" :key="i">
              <MdIcon name="warning" :size="14" />
              <span>{{ v.gate }} · {{ v.detail }}</span>
            </div>
          </div>

          <div v-if="failedClaims(details[item.report_id]).length" class="table-block">
            <h4>缺证据数字</h4>
            <table>
              <thead><tr><th>块</th><th>数字</th><th>值</th><th>单位</th></tr></thead>
              <tbody>
                <tr v-for="c in failedClaims(details[item.report_id])" :key="c.id">
                  <td>{{ c.block_id }}</td><td>{{ c.claim }}</td>
                  <td class="num">{{ c.value }}</td><td>{{ c.unit || '—' }}</td>
                </tr>
              </tbody>
            </table>
          </div>

          <div class="table-block">
            <h4>证据样例（{{ details[item.report_id].evidence_count }} 条）</h4>
            <table>
              <thead><tr><th>块</th><th>来源</th><th>定位</th><th>值</th><th>hash</th></tr></thead>
              <tbody>
                <tr v-for="e in evidenceSample(details[item.report_id])" :key="e.id">
                  <td>{{ e.block_id }}</td><td>{{ e.source }}</td>
                  <td class="locator">{{ e.locator }}</td>
                  <td>{{ e.value_text || e.excerpt }}</td>
                  <td class="num">{{ e.value_hash }}</td>
                </tr>
              </tbody>
            </table>
          </div>

          <div class="manifest num">
            manifest spec {{ details[item.report_id].report?.manifest?.spec_hash }}
            · 数据集 {{ details[item.report_id].report?.manifest?.datasets?.length || 0 }}
          </div>
        </div>
      </MdCard>
    </div>
    </template>

    <RunsPanel v-else />
  </div>
</template>

<style scoped>
.audit-view {
  flex: 1; min-height: 0; overflow-y: auto;
  padding: var(--space-4) var(--space-5) var(--space-6);
  display: flex; flex-direction: column; gap: var(--space-3);
}
.intro { display: flex; align-items: flex-start; justify-content: space-between; gap: var(--space-3); }
.intro h2 { margin: 0 0 4px; font-size: 18px; }
.intro p { margin: 0; font-size: 12px; }
.muted { color: var(--text-muted); font-size: 12px; }
.tabs { display: flex; gap: 6px; }
.tabs button {
  height: 34px; padding: 0 16px; border-radius: var(--md-radius-full);
  background: transparent; color: var(--text-secondary);
  font-size: 13px; font-weight: 600;
  transition: background-color var(--motion-fade-in), color var(--motion-fade-in);
}
.tabs button:hover { background: var(--bg-hover); color: var(--text-primary); }
.tabs button.active { background: var(--md-secondary-container); color: var(--md-on-secondary-container); }
.summary-row, .report-item__meta { display: flex; flex-wrap: wrap; gap: var(--space-2); }
.chip {
  display: inline-flex; align-items: center; padding: 3px 10px;
  border-radius: var(--md-radius-full); background: var(--md-surface-container);
  color: var(--text-secondary); font-size: 11px;
}
.chip--ok { color: var(--down); }
.chip--bad { color: var(--md-error); }
.report-list { display: flex; flex-direction: column; gap: var(--space-3); }
.report-item { display: flex; flex-direction: column; gap: var(--space-3); }
.report-item__head { display: flex; align-items: flex-start; justify-content: space-between; gap: var(--space-3); }
.report-item__title { display: flex; align-items: flex-start; gap: var(--space-2); color: var(--md-primary); }
.report-item__title h3 { margin: 0 0 2px; font-size: 15px; color: var(--text-primary); }
.report-item__title p { margin: 0; }
.status {
  padding: 3px 10px; border-radius: var(--md-radius-full);
  font-size: 11px; font-weight: 700; white-space: nowrap;
  background: var(--md-surface-container); color: var(--text-secondary);
}
.status--passed { color: var(--down); background: color-mix(in srgb, var(--down) 10%, transparent); }
.status--blocked { color: var(--md-error); background: color-mix(in srgb, var(--md-error) 10%, transparent); }
.report-item__actions { display: flex; gap: var(--space-2); flex-wrap: wrap; }
.audit-detail { display: flex; flex-direction: column; gap: var(--space-3); padding-top: var(--space-2); border-top: 1px solid var(--border-subtle); }
.gates { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 6px; }
.gate {
  display: grid; grid-template-columns: 1fr auto; gap: 2px 8px;
  padding: 7px 10px; border-radius: var(--md-radius-s);
  background: var(--md-surface-container-lowest); font-size: 11px;
}
.gate strong { font-weight: 700; }
.gate small { grid-column: 1 / -1; color: var(--text-muted); }
.gate--pass strong { color: var(--down); }
.gate--fail strong { color: var(--md-error); }
.violations {
  display: flex; flex-direction: column; gap: 4px;
  padding: 8px 12px; border-radius: var(--md-radius-s);
  background: color-mix(in srgb, var(--md-error) 8%, transparent);
  color: var(--md-error); font-size: 12px;
}
.violations > div { display: flex; align-items: center; gap: 6px; }
.table-block h4 { margin: 0 0 6px; font-size: 12px; color: var(--text-secondary); }
.table-block { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-size: 11px; }
th, td { border-bottom: 1px solid var(--border-subtle); padding: 6px 8px; text-align: left; white-space: nowrap; }
th { color: var(--text-muted); font-weight: 600; }
.locator { max-width: 300px; overflow: hidden; text-overflow: ellipsis; }
.manifest { color: var(--text-muted); font-size: 10.5px; }
.skeleton { border-radius: var(--md-radius-l); background: var(--md-surface-container); animation: pulse 1.4s ease-in-out infinite; }
@keyframes pulse { 0%, 100% { opacity: 0.55; } 50% { opacity: 0.9; } }
@media (max-width: 768px) {
  .audit-view { padding: var(--space-3); }
  .report-item__head { flex-direction: column; }
}
</style>
