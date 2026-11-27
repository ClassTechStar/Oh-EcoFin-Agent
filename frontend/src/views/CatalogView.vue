<script setup lang="ts">
/** 标的目录（M8）：目录统计、检索、FinanceDatabase 快照导入。 */
import { onMounted, ref } from 'vue'
import { api } from '../sse'
import { useToast } from '../toast'
import MdButton from '../components/MdButton.vue'
import MdCard from '../components/MdCard.vue'
import MdEmptyState from '../components/MdEmptyState.vue'
import MdIcon from '../components/MdIcon.vue'

const { show } = useToast()
const stats = ref<any>(null)
const items = ref<any[]>([])
const total = ref(0)
const loading = ref(true)
const query = ref('')
const assetClass = ref('')
const listingStatus = ref('active')
const importPath = ref('')
const importing = ref(false)
const importResult = ref<any>(null)

async function loadStats() {
  try { stats.value = await api.catalogStats() } catch { /* ignore */ }
}

async function search() {
  loading.value = true
  try {
    const data = await api.catalogSearch({
      q: query.value,
      asset_class: assetClass.value,
      listing_status: listingStatus.value,
      limit: 50
    })
    items.value = data.items || []
    total.value = data.total || 0
  } catch (e: any) {
    show(e.message || '目录检索失败', 'error')
  } finally {
    loading.value = false
  }
}

async function importCatalog() {
  if (!importPath.value.trim()) {
    show('请填写 FinanceDatabase 根目录', 'error')
    return
  }
  importing.value = true
  importResult.value = null
  try {
    importResult.value = await api.catalogImport({ path: importPath.value.trim() })
    show(importResult.value.idempotent ? '该快照已导入（幂等）' : '目录快照导入完成')
    await Promise.all([loadStats(), search()])
  } catch (e: any) {
    show(e.message || '目录导入失败', 'error')
  } finally {
    importing.value = false
  }
}

onMounted(async () => {
  await Promise.all([loadStats(), search()])
})
</script>

<template>
  <div class="catalog-view">
    <div class="intro">
      <div>
        <h2>标的目录</h2>
        <p class="muted">版本化标的目录：代码/名称/行业检索；目录不含行情，行情请回投研对话。</p>
      </div>
      <MdButton variant="text" icon="refresh" small :loading="loading" @click="search">刷新</MdButton>
    </div>

    <div class="stats-row">
      <span class="chip">标的 {{ stats?.total ?? 0 }}</span>
      <span v-for="row in stats?.by_asset_class || []" :key="row.asset_class" class="chip">
        {{ row.asset_class }} {{ row.count }}
      </span>
      <span class="chip">快照 {{ stats?.snapshots?.length || 0 }}</span>
    </div>

    <MdCard variant="filled" class="search-card">
      <div class="search-row">
        <input v-model="query" class="control search-input" type="search"
               placeholder="代码 / 名称 / 行业，如 AAPL、600519、Banks" @keyup.enter="search" />
        <select v-model="assetClass" class="control">
          <option value="">全部资产</option>
          <option value="equity">股票</option>
          <option value="etf">ETF</option>
          <option value="fund">基金</option>
          <option value="index">指数</option>
          <option value="fx">外汇</option>
          <option value="crypto">加密货币</option>
          <option value="moneymarket">货币市场</option>
        </select>
        <select v-model="listingStatus" class="control">
          <option value="active">在市</option>
          <option value="delisted">退市</option>
          <option value="all">全部状态</option>
        </select>
        <MdButton icon="spark" small :loading="loading" @click="search">检索</MdButton>
      </div>
      <div class="import-row">
        <input v-model="importPath" class="control import-input" type="text"
               placeholder="FinanceDatabase 根目录（含 compression/ 或 database/）" />
        <MdButton variant="tonal" icon="plus" small :loading="importing" @click="importCatalog">
          导入目录快照
        </MdButton>
      </div>
      <p v-if="importResult" class="muted num">
        快照 {{ importResult.snapshot_id }} · {{ importResult.rows }} 行
        · {{ importResult.idempotent ? '幂等命中' : '新导入' }}
        · {{ importResult.source_version }}
      </p>
      <p class="muted">局域网模式禁用本地路径导入；服务端可用 `python scripts/import_catalog.py --source ...`。</p>
    </MdCard>

    <div v-if="loading && !items.length" class="skeleton" style="height: 160px" />
    <MdEmptyState v-else-if="!items.length" icon="analysis" title="没有匹配的标的"
                  description="换一个代码/名称/行业关键词；仓库已内置 10 只演示标的（贵州茅台/平安银行/宁德时代等），完整 30 万+ 目录用 FinanceDatabase 快照导入" />

    <MdCard v-else variant="outlined" class="table-card">
      <div class="card-head">
        <strong>检索结果</strong>
        <span class="muted">共 {{ total }} 条，展示前 {{ items.length }} 条</span>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr><th>instrument_id</th><th>代码</th><th>名称</th><th>资产</th>
                <th>交易所</th><th>行业</th><th>国家</th><th>状态</th></tr>
          </thead>
          <tbody>
            <tr v-for="item in items" :key="item.instrument_id">
              <td class="num">{{ item.instrument_id }}</td>
              <td class="num">{{ item.symbol }}</td>
              <td>{{ item.name }}</td>
              <td>{{ item.asset_class }}</td>
              <td>{{ item.exchange || '—' }}</td>
              <td>{{ item.industry || item.sector || '—' }}</td>
              <td>{{ item.country || '—' }}</td>
              <td><span :class="['status', item.listing_status]">{{ item.listing_status }}</span></td>
            </tr>
          </tbody>
        </table>
      </div>
    </MdCard>
  </div>
</template>

<style scoped>
.catalog-view {
  flex: 1; min-height: 0; overflow-y: auto;
  padding: var(--space-4) var(--space-5) var(--space-6);
  display: flex; flex-direction: column; gap: var(--space-3);
}
.intro { display: flex; align-items: flex-start; justify-content: space-between; gap: var(--space-3); }
.intro h2 { margin: 0 0 4px; font-size: 18px; }
.intro p { margin: 0; font-size: 12px; }
.muted { color: var(--text-muted); font-size: 12px; }
.stats-row { display: flex; flex-wrap: wrap; gap: var(--space-2); }
.chip {
  display: inline-flex; align-items: center; padding: 3px 10px;
  border-radius: var(--md-radius-full); background: var(--md-surface-container);
  color: var(--text-secondary); font-size: 11px;
}
.search-card { display: flex; flex-direction: column; gap: var(--space-2); }
.search-row, .import-row { display: flex; gap: var(--space-2); flex-wrap: wrap; align-items: center; }
.control {
  height: 36px; padding: 0 10px; border-radius: var(--md-radius-s);
  border: 1px solid var(--border-default);
  background: var(--md-surface-container-lowest); color: var(--text-primary);
  font-size: 13px; outline: none;
}
.control:focus { border-color: var(--md-primary); }
.search-input { min-width: 260px; flex: 1; max-width: 520px; }
.import-input { min-width: 320px; flex: 1; }
.table-card { min-width: 0; }
.card-head { display: flex; align-items: center; justify-content: space-between; gap: var(--space-2); margin-bottom: var(--space-2); }
.table-wrap { overflow: auto; max-height: 620px; }
table { border-collapse: collapse; width: 100%; font-size: 12px; }
th, td { border-bottom: 1px solid var(--border-subtle); padding: 7px 9px; text-align: left; white-space: nowrap; }
th { color: var(--text-muted); font-weight: 600; position: sticky; top: 0; background: var(--md-surface); }
.status { font-size: 10.5px; padding: 2px 8px; border-radius: var(--md-radius-full); background: var(--md-surface-container); }
.status.active { color: var(--down); }
.status.delisted { color: var(--md-error); }
.skeleton { border-radius: var(--md-radius-l); background: var(--md-surface-container); animation: pulse 1.4s ease-in-out infinite; }
@keyframes pulse { 0%, 100% { opacity: 0.55; } 50% { opacity: 0.9; } }
@media (max-width: 768px) {
  .catalog-view { padding: var(--space-3); }
  .search-input, .import-input { min-width: 0; width: 100%; max-width: none; }
}
</style>
