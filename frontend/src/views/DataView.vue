<script setup lang="ts">
/** 数据分析工作台（M4）：数据集选择 → 模块参数 → 确定性结果 + ECharts 图表。
 *  导出 PNG/HTML 均由前端完成（HTML 内嵌 PNG 与数据表，无外部 CDN）。 */
import { computed, onMounted, ref, watch } from 'vue'
import { api } from '../sse'
import { useToast } from '../toast'
import MdButton from '../components/MdButton.vue'
import MdCard from '../components/MdCard.vue'
import MdEmptyState from '../components/MdEmptyState.vue'
import MdIcon from '../components/MdIcon.vue'
import AnalysisChart from '../components/AnalysisChart.vue'

type ParamMeta = {
  name: string
  label: string
  kind: 'columns' | 'column' | 'enum' | 'number' | 'string'
  required?: boolean
  default?: any
  options?: string[]
  column_types?: string[]
  help?: string
  split?: boolean
}
type ModuleInfo = {
  name: string
  label: string
  description: string
  params: ParamMeta[]
}
type Dataset = {
  dataset_id: string
  name: string
  table_name: string
  version: number
  rows: number
  columns: number
  sha256: string
  schema: Record<string, string>
}

const { show } = useToast()
const datasets = ref<Dataset[]>([])
const modules = ref<ModuleInfo[]>([])
const datasetId = ref('')
const moduleName = ref('describe')
const values = ref<Record<string, any>>({})
const result = ref<any>(null)
const loading = ref(false)
const uploading = ref(false)
const loadingBase = ref(true)
const fileInput = ref<HTMLInputElement | null>(null)
const chartRef = ref<any>(null)
const draft = ref<any[]>([])
const reportTitle = ref('数据分析报告')
const reportSubtitle = ref('')
const composing = ref(false)
const composed = ref<any>(null)

const dataset = computed(() => datasets.value.find(d => d.dataset_id === datasetId.value))
const currentModule = computed(() => modules.value.find(m => m.name === moduleName.value))

const NUMERIC = /^(TINYINT|SMALLINT|INTEGER|BIGINT|HUGEINT|UTINYINT|USMALLINT|UINTEGER|UBIGINT|FLOAT|REAL|DOUBLE|DECIMAL)/

function columnsFor(types?: string[]): string[] {
  const all = Object.keys(dataset.value?.schema || {})
  if (!types || types.includes('any')) return all
  return all.filter(col => {
    const dtype = (dataset.value?.schema[col] || '').toUpperCase()
    if (types.includes('numeric') && NUMERIC.test(dtype)) return true
    if (types.includes('date') && (/^(DATE|TIMESTAMP)/.test(dtype) || /CHAR|TEXT|STRING/.test(dtype))) return true
    return false
  })
}

function resetParams() {
  const next: Record<string, any> = {}
  for (const p of currentModule.value?.params || []) {
    if (p.kind === 'columns') next[p.name] = []
    else if (p.kind === 'number') next[p.name] = p.default ?? 0
    else if (p.kind === 'enum') next[p.name] = p.default ?? p.options?.[0] ?? ''
    else next[p.name] = p.default ?? ''
  }
  values.value = next
  result.value = null
}

async function loadDatasets() {
  const data = await api.datasets()
  datasets.value = data.datasets || []
  if (!datasets.value.some(d => d.dataset_id === datasetId.value)) {
    datasetId.value = datasets.value[0]?.dataset_id || ''
  }
}

function buildParams(): Record<string, any> {
  const out: Record<string, any> = {}
  for (const p of currentModule.value?.params || []) {
    const value = values.value[p.name]
    if (p.kind === 'columns') out[p.name] = Array.isArray(value) ? value : []
    else if (p.kind === 'number') out[p.name] = Number(value)
    else if (p.split) out[p.name] = String(value || '').split(',').map(s => s.trim()).filter(Boolean)
    else out[p.name] = value ?? ''
  }
  return out
}

async function run() {
  if (!datasetId.value || !currentModule.value) {
    show('请先选择数据集与分析模块', 'error')
    return
  }
  for (const p of currentModule.value.params) {
    if (!p.required) continue
    const value = values.value[p.name]
    const empty = p.kind === 'columns' ? !(value && value.length) : !String(value ?? '').trim()
    if (empty) { show(`请填写：${p.label}`, 'error'); return }
  }
  loading.value = true
  try {
    result.value = await api.analysisRun({
      module: moduleName.value,
      dataset_id: datasetId.value,
      params: buildParams()
    })
  } catch (e: any) {
    result.value = null
    show(e.message || '分析失败', 'error')
  } finally {
    loading.value = false
  }
}

function addToReport() {
  if (!result.value) return
  const block = {
    type: 'analysis',
    title: result.value.label,
    dataset_id: result.value.dataset.dataset_id,
    datasetName: result.value.dataset.name,
    module: result.value.module,
    params: result.value.params,
    version: result.value.dataset.version
  }
  const key = JSON.stringify(block)
  if (draft.value.some(item => item._key === key)) {
    show('该分析已在报告草稿中')
    return
  }
  draft.value.push({ ...block, _key: key })
  composed.value = null
  show('已加入报告草稿')
}

function removeDraft(index: number) {
  draft.value.splice(index, 1)
  composed.value = null
}

function clearDraft() {
  draft.value = []
  composed.value = null
}

async function composeReport() {
  if (!draft.value.length || !reportTitle.value.trim()) {
    show('请先加入分析块并填写报告标题', 'error')
    return
  }
  composing.value = true
  try {
    const blocks = draft.value.map(item => ({
      type: 'analysis',
      title: item.title,
      dataset_id: item.dataset_id,
      module: item.module,
      params: item.params,
      version: item.version
    }))
    composed.value = await api.composeReport({
      title: reportTitle.value,
      subtitle: reportSubtitle.value,
      blocks
    })
    show('报告已生成（HTML / Word / Excel）')
  } catch (e: any) {
    show(e.message || '报告生成失败', 'error')
  } finally {
    composing.value = false
  }
}

function downloadReport(format: string) {
  if (!composed.value?.report_id) return
  window.open(api.reportFileUrl(composed.value.report_id, format), '_blank')
}

async function onFile(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  uploading.value = true
  try {
    const imported = await api.uploadDataset(file)
    await loadDatasets()
    datasetId.value = imported.dataset_id
    show(`数据集已导入：${imported.name}（${imported.rows} 行）`)
  } catch (e: any) {
    show(e.message || '导入失败', 'error')
  } finally {
    uploading.value = false
    input.value = ''
  }
}

function cell(value: any): string {
  if (value === null || value === undefined || value === '') return '—'
  if (typeof value === 'number') {
    if (Number.isInteger(value)) return String(value)
    return value.toFixed(6).replace(/0+$/, '').replace(/\.$/, '')
  }
  return String(value)
}

function safeName(text: string): string {
  return text.replace(/[\\/:*?"<>|]+/g, '_')
}

function save(href: string, filename: string) {
  const link = document.createElement('a')
  link.href = href
  link.download = filename
  link.click()
}

function exportPng() {
  const url = chartRef.value?.getDataURL?.()
  if (!url) { show('图表尚未渲染完成', 'error'); return }
  save(url, safeName(`${dataset.value?.name || 'dataset'}_${moduleName.value}`) + '.png')
}

function exportHtml() {
  const r = result.value
  const table = r?.data?.table
  if (!table) { show('当前结果没有可导出的数据表', 'error'); return }
  const png = chartRef.value?.getDataURL?.() || ''
  const esc = (v: any) => String(v ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;')
  const head = table.columns.map((c: string) => `<th>${esc(c)}</th>`).join('')
  const body = table.rows.map((row: any[]) =>
    `<tr>${row.map(v => `<td>${esc(cell(v))}</td>`).join('')}</tr>`).join('')
  const warnings = (r.warnings || []).map((w: string) => `<li>${esc(w)}</li>`).join('')
  const title = `${dataset.value?.name || 'dataset'} · ${r.label}`
  const html = `<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>${esc(title)}</title>
<style>
body{font-family:"Segoe UI","Microsoft YaHei",system-ui,sans-serif;max-width:1100px;margin:32px auto;padding:0 20px;color:#24303b;background:#fff}
h1{font-size:22px;margin:0 0 6px}h2{font-size:16px;margin:28px 0 10px}
.meta{color:#5f6b76;font-size:12px;margin:4px 0}
table{border-collapse:collapse;width:100%;font-size:12px;margin-top:8px}
th,td{border:1px solid #dfe4ea;padding:6px 8px;text-align:left;white-space:nowrap}
th{background:#f4f6f8}img{max-width:100%;margin-top:12px;border:1px solid #e3e6ea;border-radius:8px}
.note{color:#5f6b76;font-size:12px;margin-top:20px}.warn{color:#a05a00}
</style></head><body>
<h1>${esc(title)}</h1>
<p class="meta">数据集 ${esc(dataset.value?.name)} v${esc(result.value?.dataset?.version)} · sha256 ${esc((result.value?.dataset?.sha256 || '').slice(0, 16))}… · 生成于 ${esc(r.generated_at)}</p>
<p class="meta">result_hash ${esc(r.result_hash)}${r.chart ? ` · chart_spec ${esc(r.chart.spec_hash)}` : ''}</p>
${png ? `<img src="${png}" alt="chart">` : ''}
<h2>数据表</h2>
<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>
${warnings ? `<h2>提示</h2><ul class="warn">${warnings}</ul>` : ''}
<p class="note">本报告由教学演示智能体生成，仅供课程学习使用，不构成任何投资建议。</p>
</body></html>`
  const blob = new Blob([html], { type: 'text/html;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  save(url, safeName(`${dataset.value?.name || 'dataset'}_${moduleName.value}`) + '.html')
  setTimeout(() => URL.revokeObjectURL(url), 2000)
}

watch(moduleName, resetParams)
watch(datasetId, () => { result.value = null })
watch([reportTitle, reportSubtitle], () => { composed.value = null })

onMounted(async () => {
  try {
    const [moduleRes] = await Promise.all([api.analysisModules(), loadDatasets()])
    modules.value = moduleRes.modules || []
    if (modules.value.length && !modules.value.some(m => m.name === moduleName.value)) {
      moduleName.value = modules.value[0].name
    }
    resetParams()
  } catch (e: any) {
    show(e.message || '数据分析初始化失败', 'error')
  } finally {
    loadingBase.value = false
  }
})
</script>

<template>
  <div class="data-view">
    <div class="intro">
      <div>
        <h2>数据分析</h2>
        <p class="muted">确定性统计 + 可复现图表；数据集快照与图表 spec 均带哈希，可回放。</p>
      </div>
      <MdButton variant="tonal" icon="plus" small :loading="uploading" @click="fileInput?.click()">
        导入数据集
      </MdButton>
      <input ref="fileInput" type="file" accept=".csv,.tsv,.xlsx,.xlsm,.parquet" hidden @change="onFile" />
    </div>

    <div v-if="loadingBase" class="skeleton" style="height: 220px" />

    <MdCard v-else variant="filled" class="control-card">
      <div class="dataset-row">
        <label class="field-label" for="dataset-select">数据集</label>
        <select id="dataset-select" v-model="datasetId" class="control dataset-select">
          <option v-if="!datasets.length" value="">暂无数据集</option>
          <option v-for="d in datasets" :key="d.dataset_id" :value="d.dataset_id">
            {{ d.name }} · {{ d.rows }} 行 × {{ d.columns }} 列 · v{{ d.version }}
          </option>
        </select>
        <MdButton variant="text" icon="refresh" small @click="loadDatasets">刷新</MdButton>
      </div>

      <div class="module-chips">
        <button v-for="m in modules" :key="m.name"
                :class="['module-chip', { 'module-chip--active': m.name === moduleName }]"
                @click="moduleName = m.name">
          {{ m.label }}
        </button>
      </div>

      <div v-if="currentModule" class="form-grid">
        <div v-for="p in currentModule.params" :key="p.name" class="field">
          <label :for="`param-${p.name}`">
            {{ p.label }}<span v-if="p.required" class="req">*</span>
          </label>
          <select v-if="p.kind === 'columns'" :id="`param-${p.name}`"
                  v-model="values[p.name]" class="control multi" multiple
                  :size="Math.min(6, Math.max(3, columnsFor(p.column_types).length))">
            <option v-for="col in columnsFor(p.column_types)" :key="col" :value="col">{{ col }}</option>
          </select>
          <select v-else-if="p.kind === 'column'" :id="`param-${p.name}`"
                  v-model="values[p.name]" class="control">
            <option value="">{{ p.required ? '请选择' : '（可选）' }}</option>
            <option v-for="col in columnsFor(p.column_types)" :key="col" :value="col">{{ col }}</option>
          </select>
          <select v-else-if="p.kind === 'enum'" :id="`param-${p.name}`"
                  v-model="values[p.name]" class="control">
            <option v-for="opt in p.options" :key="opt" :value="opt">{{ opt }}</option>
          </select>
          <input v-else-if="p.kind === 'number'" :id="`param-${p.name}`"
                 v-model.number="values[p.name]" class="control" type="number" step="any" />
          <input v-else :id="`param-${p.name}`" v-model="values[p.name]"
                 class="control" type="text" :placeholder="p.help || ''" />
          <small v-if="p.help" class="help">{{ p.help }}</small>
        </div>
      </div>

      <div class="run-row">
        <MdButton icon="analysis" :loading="loading" :disabled="!datasetId" @click="run">
          运行分析
        </MdButton>
        <span class="muted">{{ currentModule?.description }}</span>
      </div>
    </MdCard>

    <MdEmptyState v-if="!loadingBase && !datasets.length" icon="info"
                  title="还没有数据集"
                  description="导入 CSV/Excel/Parquet 后即可做描述统计、缺失、相关、趋势、分组、漏斗与 A/B 测试" />

    <template v-if="result">
      <div class="meta-row">
        <span class="chip">{{ dataset?.name }} · v{{ result.dataset.version }}</span>
        <span class="chip">{{ result.elapsed_ms }} ms</span>
        <span class="chip num">result {{ result.result_hash }}</span>
        <span v-if="result.chart" class="chip num">chart {{ result.chart.spec_hash }}</span>
        <MdButton variant="tonal" icon="plus" small @click="addToReport">加入报告</MdButton>
      </div>
      <div v-if="result.warnings?.length" class="warnings">
        <div v-for="w in result.warnings" :key="w"><MdIcon name="warning" :size="14" /> {{ w }}</div>
      </div>

      <div :class="['result-grid', { 'result-grid--single': !result.chart }]">
        <MdCard v-if="result.chart" variant="elevated" class="chart-card">
          <div class="card-head">
            <div class="card-title">
              <strong>{{ result.label }}</strong>
              <span class="muted num">spec {{ result.chart.spec_hash }}</span>
            </div>
            <div class="card-actions">
              <MdButton variant="text" icon="monitor" small @click="exportPng">PNG</MdButton>
              <MdButton variant="text" icon="info" small @click="exportHtml">HTML</MdButton>
            </div>
          </div>
          <AnalysisChart ref="chartRef" :option="result.chart.option" :height="380" />
        </MdCard>

        <MdCard variant="outlined" class="table-card">
          <div class="card-head">
            <strong>数据表</strong>
            <span class="muted">{{ result.data.table?.rows?.length ?? 0 }} 行</span>
          </div>
          <div v-if="result.data.table" class="table-wrap">
            <table>
              <thead>
                <tr><th v-for="c in result.data.table.columns" :key="c">{{ c }}</th></tr>
              </thead>
              <tbody>
                <tr v-for="(row, idx) in result.data.table.rows" :key="idx">
                  <td v-for="(v, ci) in row" :key="ci">{{ cell(v) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </MdCard>
      </div>
    </template>

    <MdCard v-if="draft.length || composed" variant="filled" class="report-card">
      <div class="card-head">
        <div class="card-title">
          <strong>报告草稿</strong>
          <span class="muted">{{ draft.length }} 个分析块 · 服务端重跑并记录哈希</span>
        </div>
        <MdButton variant="text" icon="trash" small :disabled="!draft.length" @click="clearDraft">
          清空
        </MdButton>
      </div>
      <div v-if="draft.length" class="draft-list">
        <div v-for="(item, index) in draft" :key="item._key" class="draft-item">
          <span class="draft-index num">{{ index + 1 }}</span>
          <div class="draft-main">
            <strong>{{ item.title }}</strong>
            <span class="muted">{{ item.datasetName }} · {{ item.module }}</span>
          </div>
          <button class="draft-remove" @click="removeDraft(index)">移除</button>
        </div>
      </div>
      <div v-else class="muted">先运行分析，再点“加入报告”。</div>

      <div class="report-form">
        <input v-model="reportTitle" class="control" type="text" placeholder="报告标题" />
        <input v-model="reportSubtitle" class="control" type="text" placeholder="副标题（可选）" />
        <MdButton icon="report" :loading="composing" :disabled="!draft.length" @click="composeReport">
          生成报告
        </MdButton>
      </div>

      <div v-if="composed" class="report-result">
        <div class="meta-row">
          <span class="chip num">spec {{ composed.spec_hash }}</span>
          <span v-for="f in composed.files" :key="f.format" class="chip">
            {{ f.format.toUpperCase() }} · {{ (f.bytes / 1024).toFixed(1) }} KB
          </span>
        </div>
        <div class="report-actions">
          <MdButton v-for="f in composed.files" :key="f.format" variant="outlined"
                    icon="monitor" small @click="downloadReport(f.format)">
            下载 {{ f.format.toUpperCase() }}
          </MdButton>
        </div>
      </div>
    </MdCard>
  </div>
</template>

<style scoped>
.data-view {
  flex: 1; min-height: 0; overflow-y: auto;
  padding: var(--space-4) var(--space-5) var(--space-6);
  display: flex; flex-direction: column; gap: var(--space-3);
}
.intro { display: flex; align-items: flex-start; justify-content: space-between; gap: var(--space-3); }
.intro h2 { margin: 0 0 4px; font-size: 18px; }
.intro p { margin: 0; font-size: 12px; }
.muted { color: var(--text-muted); font-size: 12px; }
.control-card { display: flex; flex-direction: column; gap: var(--space-3); }
.dataset-row { display: flex; align-items: center; gap: var(--space-2); flex-wrap: wrap; }
.field-label { font-size: 12px; color: var(--text-muted); }
.control {
  height: 36px; padding: 0 10px; border-radius: var(--md-radius-s);
  border: 1px solid var(--border-default);
  background: var(--md-surface-container-lowest); color: var(--text-primary);
  font-size: 13px; outline: none;
}
.control:focus { border-color: var(--md-primary); }
.dataset-select { min-width: 280px; flex: 1; max-width: 520px; }
.module-chips { display: flex; gap: var(--space-2); flex-wrap: wrap; }
.module-chip {
  height: 32px; padding: 0 14px; border-radius: var(--md-radius-full);
  border: 1px solid var(--border-default); background: transparent;
  color: var(--text-secondary); font-size: 12px; font-weight: 600;
  transition: background-color var(--motion-fade-in), color var(--motion-fade-in);
}
.module-chip:hover { background: var(--bg-hover); color: var(--text-primary); }
.module-chip--active { background: var(--md-secondary-container); color: var(--md-on-secondary-container); border-color: transparent; }
.form-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(210px, 1fr)); gap: var(--space-3); }
.field { display: flex; flex-direction: column; gap: 5px; min-width: 0; }
.field label { font-size: 12px; color: var(--text-secondary); font-weight: 600; }
.req { color: var(--md-error); margin-left: 3px; }
.multi { height: auto; padding: 4px; }
.help { color: var(--text-muted); font-size: 11px; }
.run-row { display: flex; align-items: center; gap: var(--space-3); flex-wrap: wrap; padding-top: var(--space-1); }
.meta-row { display: flex; gap: var(--space-2); flex-wrap: wrap; }
.chip {
  display: inline-flex; align-items: center; gap: 4px;
  padding: 3px 10px; border-radius: var(--md-radius-full);
  background: var(--md-surface-container); color: var(--text-secondary); font-size: 11px;
}
.warnings {
  display: flex; flex-direction: column; gap: 4px;
  padding: 8px 12px; border-radius: var(--md-radius-s);
  background: color-mix(in srgb, var(--warn) 8%, transparent); color: var(--warn); font-size: 12px;
}
.warnings > div { display: flex; align-items: center; gap: 6px; }
.result-grid { display: grid; grid-template-columns: minmax(0, 1.45fr) minmax(0, 1fr); gap: var(--space-4); align-items: start; }
.result-grid--single { grid-template-columns: minmax(0, 1fr); }
.chart-card, .table-card { min-width: 0; }
.card-head { display: flex; align-items: flex-start; justify-content: space-between; gap: var(--space-2); margin-bottom: var(--space-2); }
.card-title { display: flex; flex-direction: column; gap: 2px; }
.card-actions { display: flex; gap: 2px; flex-wrap: wrap; }
.table-wrap { overflow: auto; max-height: 520px; }
table { border-collapse: collapse; width: 100%; font-size: 12px; }
th, td { border-bottom: 1px solid var(--border-subtle); padding: 7px 9px; text-align: left; white-space: nowrap; }
th { color: var(--text-muted); font-weight: 600; position: sticky; top: 0; background: var(--bg-card); }
td { color: var(--text-primary); }
.skeleton { border-radius: var(--md-radius-l); background: var(--md-surface-container); animation: pulse 1.4s ease-in-out infinite; }
@keyframes pulse { 0%, 100% { opacity: 0.55; } 50% { opacity: 0.9; } }

.report-card { display: flex; flex-direction: column; gap: var(--space-3); }
.draft-list { display: flex; flex-direction: column; gap: 6px; }
.draft-item {
  display: flex; align-items: center; gap: var(--space-3);
  padding: 8px 10px; border-radius: var(--md-radius-s);
  background: var(--md-surface-container-lowest);
}
.draft-index {
  width: 24px; height: 24px; flex: 0 0 auto; display: grid; place-items: center;
  border-radius: 50%; background: var(--md-secondary-container);
  color: var(--md-on-secondary-container); font-size: 11px; font-weight: 700;
}
.draft-main { display: flex; flex-direction: column; gap: 1px; min-width: 0; flex: 1; }
.draft-main strong { font-size: 13px; }
.draft-remove {
  border: none; background: transparent; color: var(--md-error);
  font-size: 12px; padding: 4px 6px; border-radius: var(--md-radius-s);
}
.draft-remove:hover { background: var(--bg-hover); }
.report-form { display: flex; align-items: center; gap: var(--space-2); flex-wrap: wrap; }
.report-form .control { min-width: 220px; flex: 1; max-width: 360px; }
.report-result { display: flex; flex-direction: column; gap: var(--space-2); padding-top: var(--space-2); border-top: 1px solid var(--border-subtle); }
.report-actions { display: flex; gap: var(--space-2); flex-wrap: wrap; }

@media (max-width: 1100px) {
  .result-grid { grid-template-columns: minmax(0, 1fr); }
}
@media (max-width: 768px) {
  .data-view { padding: var(--space-3); }
  .dataset-select { min-width: 0; width: 100%; }
}
</style>
