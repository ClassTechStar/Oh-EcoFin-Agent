<script setup lang="ts">
/** 数据集管理（M8）：上传、列表、版本、画像与 schema。 */
import { onMounted, ref } from 'vue'
import { api, uploadDataset } from '../sse'
import { useToast } from '../toast'
import MdButton from '../components/MdButton.vue'
import MdCard from '../components/MdCard.vue'
import MdEmptyState from '../components/MdEmptyState.vue'
import MdIcon from '../components/MdIcon.vue'

const { show } = useToast()
const datasets = ref<any[]>([])
const profile = ref<Record<string, any>>({})
const versions = ref<Record<string, any[]>>({})
const loading = ref(true)
const uploading = ref(false)
const seeding = ref(false)
const loadingId = ref('')
const fileInput = ref<HTMLInputElement | null>(null)

/** 一键示例数据集：前端内联生成演示 CSV（A/B 实验口径），走既有上传通道。 */
const SAMPLE_CSV = [
  'date,group,visitors,orders,revenue',
  '2026-09-01,A,1200,86,5120',
  '2026-09-01,B,1180,97,5560',
  '2026-09-02,A,1240,92,5380',
  '2026-09-02,B,1265,104,6010',
  '2026-09-03,A,1188,79,4890',
  '2026-09-03,B,1210,88,5320',
  '2026-09-04,A,1302,101,6110',
  '2026-09-04,B,1340,112,6480',
  '2026-09-05,A,1265,94,5730',
  '2026-09-05,B,1290,99,5870',
  '2026-09-06,A,1350,108,6340',
  '2026-09-06,B,1388,121,7010',
  '2026-09-07,A,1288,90,5410',
  '2026-09-07,B,1310,102,6120',
].join('\n')

async function seedSample() {
  seeding.value = true
  try {
    const file = new File([SAMPLE_CSV], 'demo_ab_test.csv', { type: 'text/csv' })
    const result = await uploadDataset(file)
    show(`示例数据集已就绪：${result.name}（${result.rows} 行）——去「数据分析」页跑 A-B 测试`)
    await refresh()
  } catch (e: any) {
    show(e.message || '示例导入失败', 'error')
  } finally {
    seeding.value = false
  }
}

async function refresh() {
  loading.value = true
  try {
    const data = await api.datasets()
    datasets.value = data.datasets || []
  } catch (e: any) {
    show(e.message || '数据集加载失败', 'error')
  } finally {
    loading.value = false
  }
}

async function onFile(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  uploading.value = true
  try {
    const result = await uploadDataset(file)
    show(`已导入 ${result.name}（${result.rows} 行 × ${result.columns} 列）`)
    await refresh()
  } catch (e: any) {
    show(e.message || '导入失败', 'error')
  } finally {
    uploading.value = false
    input.value = ''
  }
}

async function toggleProfile(datasetId: string) {
  if (profile.value[datasetId]) {
    delete profile.value[datasetId]
    return
  }
  loadingId.value = datasetId
  try {
    const [profileData, versionData] = await Promise.all([
      api.datasetProfile(datasetId),
      api.datasetVersions(datasetId)
    ])
    profile.value[datasetId] = profileData
    versions.value[datasetId] = versionData.versions || []
  } catch (e: any) {
    show(e.message || '画像加载失败', 'error')
  } finally {
    loadingId.value = ''
  }
}

function cell(value: any): string {
  if (value === null || value === undefined || value === '') return '—'
  if (typeof value === 'number') return Number.isInteger(value) ? String(value) : value.toFixed(4)
  return String(value)
}

onMounted(refresh)
</script>

<template>
  <div class="datasets-view">
    <div class="intro">
      <div>
        <h2>数据集</h2>
        <p class="muted">CSV/Excel/Parquet → 不可变 Parquet 快照；版本、sha256 与画像可查。</p>
      </div>
      <MdButton icon="plus" small :loading="uploading" @click="fileInput?.click()">导入数据集</MdButton>
      <input ref="fileInput" type="file" accept=".csv,.tsv,.xlsx,.xlsm,.parquet" hidden @change="onFile" />
    </div>

    <div v-if="loading && !datasets.length" class="skeleton" style="height: 160px" />
    <MdEmptyState v-else-if="!datasets.length" icon="dashboard" title="还没有数据集"
                  description="上传 CSV/Excel/Parquet，或先导入内置示例（转化漏斗/A-B 测试演示数据）后即可在「数据分析」页做分析与报告">
      <MdButton variant="tonal" icon="spark" small :loading="seeding" @click="seedSample">
        一键导入示例数据集
      </MdButton>
      <MdButton variant="outlined" icon="plus" small @click="fileInput?.click()">
        上传本地文件
      </MdButton>
    </MdEmptyState>

    <div v-else class="dataset-list">
      <MdCard v-for="item in datasets" :key="item.dataset_id" variant="elevated" class="dataset-item">
        <div class="dataset-head">
          <div class="dataset-title">
            <MdIcon name="dashboard" :size="20" />
            <div>
              <h3>{{ item.name }}</h3>
              <p class="muted num">表名 {{ item.table_name }} · v{{ item.version }} · {{ item.dataset_id }}</p>
            </div>
          </div>
          <div class="dataset-meta">
            <span class="chip">{{ item.rows }} 行 × {{ item.columns }} 列</span>
            <span class="chip num">sha {{ item.sha256.slice(0, 12) }}…</span>
          </div>
        </div>

        <div class="dataset-actions">
          <MdButton variant="outlined" icon="info" small
                    :loading="loadingId === item.dataset_id"
                    @click="toggleProfile(item.dataset_id)">
            {{ profile[item.dataset_id] ? '收起画像' : '查看画像 / 版本' }}
          </MdButton>
          <span class="muted">在「数据分析」页选择此数据集即可；SQL 表名 {{ item.table_name }}</span>
        </div>

        <div v-if="profile[item.dataset_id]" class="dataset-detail">
          <div class="detail-row">
            <span class="chip">画像列 {{ profile[item.dataset_id].profiled_columns }}</span>
            <span v-if="profile[item.dataset_id].truncated_columns" class="chip">
              另有 {{ profile[item.dataset_id].truncated_columns }} 列未深画像
            </span>
            <span class="chip">版本 {{ versions[item.dataset_id]?.length || 0 }}</span>
          </div>
          <div class="table-wrap">
            <table>
              <thead>
                <tr><th>字段</th><th>类型</th><th>缺失</th><th>缺失率</th><th>唯一值</th>
                    <th>最小</th><th>P50</th><th>最大</th></tr>
              </thead>
              <tbody>
                <tr v-for="col in profile[item.dataset_id].profile" :key="col.name">
                  <td>{{ col.name }}</td><td><code>{{ col.dtype }}</code></td>
                  <td class="num">{{ col.null_count }}</td>
                  <td class="num">{{ (col.null_ratio * 100).toFixed(2) }}%</td>
                  <td class="num">{{ col.distinct_count }}</td>
                  <td>{{ cell(col.min) }}</td><td>{{ cell(col.p50) }}</td><td>{{ cell(col.max) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <div v-if="versions[item.dataset_id]?.length" class="versions">
            <span v-for="v in versions[item.dataset_id]" :key="v.version" class="chip num">
              v{{ v.version }} · {{ v.rows }} 行 · {{ v.created_at }}
            </span>
          </div>
        </div>
      </MdCard>
    </div>
  </div>
</template>

<style scoped>
.datasets-view {
  flex: 1; min-height: 0; overflow-y: auto;
  padding: var(--space-4) var(--space-5) var(--space-6);
  display: flex; flex-direction: column; gap: var(--space-3);
}
.intro { display: flex; align-items: flex-start; justify-content: space-between; gap: var(--space-3); }
.intro h2 { margin: 0 0 4px; font-size: 18px; }
.intro p { margin: 0; font-size: 12px; }
.muted { color: var(--text-muted); font-size: 12px; }
.dataset-list { display: flex; flex-direction: column; gap: var(--space-3); }
.dataset-item { display: flex; flex-direction: column; gap: var(--space-3); }
.dataset-head { display: flex; align-items: flex-start; justify-content: space-between; gap: var(--space-3); }
.dataset-title { display: flex; align-items: flex-start; gap: var(--space-2); color: var(--md-primary); }
.dataset-title h3 { margin: 0 0 2px; font-size: 15px; color: var(--text-primary); }
.dataset-title p { margin: 0; }
.dataset-meta { display: flex; gap: var(--space-2); flex-wrap: wrap; justify-content: flex-end; }
.chip {
  display: inline-flex; align-items: center; padding: 3px 10px;
  border-radius: var(--md-radius-full); background: var(--md-surface-container);
  color: var(--text-secondary); font-size: 11px;
}
.dataset-actions { display: flex; align-items: center; gap: var(--space-2); flex-wrap: wrap; }
.dataset-detail { display: flex; flex-direction: column; gap: var(--space-2); border-top: 1px solid var(--border-subtle); padding-top: var(--space-2); }
.detail-row, .versions { display: flex; gap: var(--space-2); flex-wrap: wrap; }
.table-wrap { overflow: auto; max-height: 420px; }
table { border-collapse: collapse; width: 100%; font-size: 11.5px; }
th, td { border-bottom: 1px solid var(--border-subtle); padding: 6px 9px; text-align: left; white-space: nowrap; }
th { color: var(--text-muted); font-weight: 600; position: sticky; top: 0; background: var(--bg-card); }
code { font-family: var(--font-mono); color: var(--text-secondary); }
.skeleton { border-radius: var(--md-radius-l); background: var(--md-surface-container); animation: pulse 1.4s ease-in-out infinite; }
@keyframes pulse { 0%, 100% { opacity: 0.55; } 50% { opacity: 0.9; } }
@media (max-width: 768px) {
  .datasets-view { padding: var(--space-3); }
  .dataset-head { flex-direction: column; }
  .dataset-meta { justify-content: flex-start; }
}
</style>
