<script setup lang="ts">
/** Agent 运行记录（M8）：run_id / 状态 / 停止原因 / 工具错误与成本。 */
import { onMounted, ref } from 'vue'
import { api } from '../sse'
import { useToast } from '../toast'
import MdButton from '../components/MdButton.vue'
import MdCard from '../components/MdCard.vue'
import MdEmptyState from '../components/MdEmptyState.vue'

const { show } = useToast()
const runs = ref<any[]>([])
const loading = ref(true)

async function refresh() {
  loading.value = true
  try {
    const data = await api.runs()
    runs.value = data.runs || []
  } catch (e: any) {
    show(e.message || '运行记录加载失败', 'error')
  } finally {
    loading.value = false
  }
}

function stateLabel(state: string): string {
  const map: Record<string, string> = {
    planning: '规划中', acting: '执行中', observing: '观察中',
    waiting_user: '等待选择', synthesizing: '整理中',
    done: '完成', failed: '失败'
  }
  return map[state] || state
}

onMounted(refresh)
</script>

<template>
  <div class="runs-panel">
    <div class="panel-head">
      <div>
        <h2>运行记录</h2>
        <p class="muted">每次 Agent run 的状态、停止原因、工具失败与 token/成本；run_id 可用于 M7 审计回溯。</p>
      </div>
      <MdButton variant="text" icon="refresh" small :loading="loading" @click="refresh">刷新</MdButton>
    </div>

    <div v-if="loading && !runs.length" class="skeleton" style="height: 150px" />
    <MdEmptyState v-else-if="!runs.length" icon="history" title="还没有 Agent 运行记录"
                  description="在投研对话中发起一次问答后，这里会出现 run 状态与工具统计" />

    <MdCard v-else variant="outlined" class="table-card">
      <div class="table-wrap">
        <table>
          <thead>
            <tr><th>run_id</th><th>会话</th><th>模式</th><th>状态</th><th>停止原因</th>
                <th>轮次</th><th>工具</th><th>token in/out</th><th>成本</th><th>开始时间</th></tr>
          </thead>
          <tbody>
            <tr v-for="run in runs" :key="run.id">
              <td class="num">{{ run.id }}</td>
              <td class="num">{{ run.session_id }}</td>
              <td>{{ run.mode }}</td>
              <td><span :class="['state', run.state]">{{ stateLabel(run.state) }}</span></td>
              <td>{{ run.stop_reason || '—' }}</td>
              <td class="num">{{ run.rounds }}</td>
              <td class="num">
                {{ run.tool_calls }} 次
                <span v-if="run.tool_errors" class="bad">失败 {{ run.tool_errors }}</span>
                <span v-if="run.tool_blocked" class="warn">拦截 {{ run.tool_blocked }}</span>
              </td>
              <td class="num">{{ run.tokens_in }} / {{ run.tokens_out }}</td>
              <td class="num">¥{{ ((run.cost_cents || 0) / 100).toFixed(3) }}</td>
              <td class="num">{{ run.started_at }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </MdCard>
  </div>
</template>

<style scoped>
.runs-panel { display: flex; flex-direction: column; gap: var(--space-3); }
.panel-head { display: flex; align-items: flex-start; justify-content: space-between; gap: var(--space-3); }
.panel-head h2 { margin: 0 0 4px; font-size: 18px; }
.panel-head p { margin: 0; }
.muted { color: var(--text-muted); font-size: 12px; }
.table-card { min-width: 0; }
.table-wrap { overflow: auto; max-height: 620px; }
table { border-collapse: collapse; width: 100%; font-size: 11.5px; }
th, td { border-bottom: 1px solid var(--border-subtle); padding: 7px 9px; text-align: left; white-space: nowrap; }
th { color: var(--text-muted); font-weight: 600; position: sticky; top: 0; background: var(--md-surface); }
.state { font-size: 10.5px; padding: 2px 8px; border-radius: var(--md-radius-full); background: var(--md-surface-container); }
.state.done { color: var(--down); }
.state.failed { color: var(--md-error); }
.state.acting, .state.planning { color: var(--md-primary); }
.bad { color: var(--md-error); margin-left: 4px; }
.warn { color: var(--warn); margin-left: 4px; }
.skeleton { border-radius: var(--md-radius-l); background: var(--md-surface-container); animation: pulse 1.4s ease-in-out infinite; }
@keyframes pulse { 0%, 100% { opacity: 0.55; } 50% { opacity: 0.9; } }
</style>
