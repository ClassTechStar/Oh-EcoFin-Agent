<script setup lang="ts">
/** 投研对话视图（业务逻辑与原 App.vue 完全一致，UI 层迁移到 MD3 组件体系）：
 *  - 模式选择 → MD3 Chip 行（assist chips）
 *  - 输入器 → MD3 48px 胶囊输入框 + 36px 行尾发送按钮（mochi TrailingButtonSize 公式）
 *  - 消息流 / 工具时间线 / 图表 / 报告 / ask_user 挂起 / 成本摘要（done 事件）
 */
import { ref, nextTick, onMounted, watch } from 'vue'
import { api, chatStream, resumeStream } from '../sse'
import MdButton from '../components/MdButton.vue'
import MdChip from '../components/MdChip.vue'
import MdTextField from '../components/MdTextField.vue'
import MdEmptyState from '../components/MdEmptyState.vue'
import MdIcon from '../components/MdIcon.vue'
import MdProgress from '../components/MdProgress.vue'
import ToolTimeline from '../components/ToolTimeline.vue'
import ChartRenderer from '../components/ChartRenderer.vue'
import AskUserCard from '../components/AskUserCard.vue'
import ReportView from '../components/ReportView.vue'
import DisclaimerFooter from '../components/DisclaimerFooter.vue'

interface ToolCallItem {
  id: string; name: string; status: string; args: string
  result_digest?: string; ms?: number; cached?: boolean
  error?: string; hint?: string
  requires_confirmation?: boolean; side_effect?: string
}
interface ChartItem { id: string; kind: string; title: string; data: any }
interface ChatMessage {
  role: 'user' | 'assistant' | 'system'
  text: string
  thinking?: string
  tools?: ToolCallItem[]
  charts?: ChartItem[]
  ask?: { qid: string; question: string; options: string[] }
  report?: any
  meta?: string
  state?: string
  budget?: any
}

const STATE_LABEL: Record<string, string> = {
  planning: '规划中',
  acting: '执行工具',
  observing: '读取结果',
  waiting_user: '等待选择',
  synthesizing: '整理答案',
  done: '已完成',
  failed: '已中止'
}

function stateLabel(state?: string): string {
  return state ? (STATE_LABEL[state] || state) : ''
}

const messages = ref<ChatMessage[]>([])
const input = ref('')
const running = ref(false)
const sessionId = ref('')
const mode = ref('auto')
const streamRef = ref<ChatMessage | null>(null)
const listEl = ref<HTMLElement | null>(null)

const MODES = [
  { value: 'auto', label: '自动路由', icon: 'spark' },
  { value: 'chat', label: '问答', icon: 'chat' },
  { value: 'debate', label: '多空辩论', icon: 'trend-up' },
  { value: 'advisor', label: '智能投顾', icon: 'dashboard' },
]

onMounted(async () => {
  try {
    const s = await api.createSession()
    sessionId.value = s.id
  } catch { /* ignore */ }
})

watch(messages, () => {
  nextTick(() => { listEl.value?.scrollTo({ top: listEl.value.scrollHeight, behavior: 'smooth' }) })
}, { deep: true })

function ensureStream(): ChatMessage {
  if (!streamRef.value) {
    streamRef.value = { role: 'assistant', text: '', thinking: '', tools: [], charts: [] }
    messages.value.push(streamRef.value)
  }
  return streamRef.value
}

function handleEvent(stream: ChatMessage, event: string, payload: any) {
  switch (event) {
    case 'thinking_delta':
      stream.thinking = (stream.thinking || '') + payload.text
      break
    case 'message_delta': {
      try {
        const parsed = JSON.parse(payload.text)
        if (parsed && parsed.type === 'report') { stream.report = parsed.report; break }
      } catch { /* 普通增量文本 */ }
      stream.text += payload.text
      break
    }
    case 'tool_call':
      if (payload.status === 'start') {
        stream.tools = [...(stream.tools || []), { ...payload, result_digest: '' }]
      } else {
        const arr = stream.tools || []
        const hit = arr.find(t => t.id === payload.id)
        if (hit) Object.assign(hit, payload)
        else stream.tools = [...arr, payload]
      }
      break
    case 'chart':
      stream.charts = [...(stream.charts || []), payload]
      break
    case 'ask_user':
      stream.ask = { qid: payload.qid, question: payload.question, options: payload.options }
      break
    case 'run_state':
      stream.state = payload.state
      break
    case 'budget':
      stream.budget = payload
      break
    case 'done': {
      stream.state = payload.state
      const stop = payload.stop_reason && payload.stop_reason !== 'answer'
        ? ` · 停止 ${payload.stop_reason}` : ''
      stream.meta = `轮次 ${payload.rounds} · 工具 ${payload.tool_stats?.calls ?? 0} 次 · 成本 ¥${payload.cost_breakdown?.total_cny ?? 0}${stop}`
      if (payload.stop_reason === 'context_budget') {
        stream.text += '\n\n（上下文预算已用尽，已按已有信息收尾。）'
      }
      running.value = false
      streamRef.value = null
      break
    }
    case 'error':
      // M6：error 是过程告警，终态由 done 收口
      stream.text += `\n\n⚠️ ${payload.message}`
      break
    case 'ping':
      break
  }
}

async function recoverStream(stream: ChatMessage, getLastId: () => number,
                             onEvent: (event: string, payload: any) => void) {
  let attempts = 0
  while (running.value && sessionId.value && attempts < 3) {
    attempts++
    await resumeStream(sessionId.value, getLastId(), onEvent, 45)
    if (running.value) {
      await new Promise(resolve => setTimeout(resolve, 700 * attempts))
    }
  }
  if (running.value) {
    stream.text += '\n\n⚠️ 多次重连仍未收到终态事件，请刷新后重试。'
  }
}

async function send() {
  const text = input.value.trim()
  if (!text || running.value) return
  input.value = ''
  running.value = true
  streamRef.value = null
  messages.value.push({ role: 'user', text })
  const stream = ensureStream()
  let lastEventId = 0
  const onEvent = (event: string, payload: any) => {
    if (typeof payload?.id === 'number' && payload.id > lastEventId) {
      lastEventId = payload.id
    }
    handleEvent(stream, event, payload)
  }

  try {
    await chatStream(text, sessionId.value, mode.value, onEvent)
    // POST 流结束但没有终态：尝试用 Last-Event-ID 重放/续传
    if (running.value) await recoverStream(stream, () => lastEventId, onEvent)
  } catch (e: any) {
    stream.text += `\n\n⚠️ 连接中断: ${e?.message || e}`
    await recoverStream(stream, () => lastEventId, onEvent)
  } finally {
    running.value = false
    streamRef.value = null
  }
}

async function answerAsk(qid: string, choice: string) {
  const m = messages.value[messages.value.length - 1]
  if (m?.ask) m.ask = undefined
  try { await api.resume(sessionId.value, qid, choice) } catch (e) {
    console.warn('resume failed', e)
  }
}
</script>

<template>
  <div class="chat">
    <div ref="listEl" class="chat__list">
      <MdEmptyState v-if="!messages.length" icon="spark" title="开始你的第一次投研对话"
        description="试试：600519 值得买吗（触发多空辩论）· 帮我做风险测评（ask_user 挂起）· 000001 双均线回测">
        <MdChip v-for="hint in ['600519 值得买吗', '帮我做风险测评', '000001 双均线回测']" :key="hint"
                icon="trend-up" @click="input = hint; send()">{{ hint }}</MdChip>
      </MdEmptyState>

      <template v-for="(m, i) in messages" :key="i">
        <!-- 用户消息（MD3：primaryContainer 气泡） -->
        <div v-if="m.role === 'user'" class="msg msg--user">
          <div class="bubble-user">{{ m.text }}</div>
        </div>

        <div v-else class="msg msg--assistant">
          <div v-if="m.state" class="run-state">
            <MdIcon name="spark" :size="13" />
            <span>{{ stateLabel(m.state) }}</span>
            <span v-if="m.budget?.action === 'compressed'" class="muted">· 上下文已压缩</span>
            <span v-else-if="m.budget?.action === 'tool_budget'" class="muted">· 工具结果预算</span>
          </div>
          <details v-if="m.thinking" class="thinking md-card--outlined" :open="true">
            <summary><MdIcon name="spark" :size="14" /> 思考过程（{{ m.thinking.length }} 字）</summary>
            <div class="raw">{{ m.thinking }}</div>
          </details>

          <ToolTimeline v-if="m.tools?.length" :tools="m.tools" />
          <AskUserCard v-if="m.ask" :ask="m.ask" @choose="answerAsk" />
          <ChartRenderer v-for="c in m.charts" :key="c.id" :chart="c" />
          <ReportView v-if="m.report" :report="m.report" />
          <div v-if="m.text" class="bubble-assistant md-card--outlined">{{ m.text }}</div>
          <div v-if="m.meta" class="msg-meta num">{{ m.meta }}</div>
        </div>
      </template>

      <div v-if="running && !streamRef?.text" class="msg skeleton" style="height: 64px; width: 70%" />
    </div>

    <footer class="composer-zone">
      <div class="mode-row" role="tablist" aria-label="对话模式">
        <MdChip v-for="m in MODES" :key="m.value" :icon="m.icon" :selected="mode === m.value"
                @click="mode = m.value">{{ m.label }}</MdChip>
      </div>

      <div class="composer md-card--elevated">
        <div class="composer__field">
          <MdTextField v-model="input" multiline :rows="2" :disabled="running"
                       placeholder="输入问题，如：600519 值得买吗？/ 帮我做风险测评 / 000001 双均线回测"
                       @enter="send" />
        </div>
        <button class="send-btn" :class="{ 'send-btn--running': running }"
                :disabled="running || !input.trim()" aria-label="发送" @click="send">
          <MdProgress v-if="running" type="ring" :size="20" />
          <MdIcon v-else name="send" :size="22" />
        </button>
      </div>
      <DisclaimerFooter />
    </footer>
  </div>
</template>

<style scoped>
.chat { flex: 1; min-height: 0; display: flex; flex-direction: column; }
.chat__list {
  flex: 1; overflow-y: auto; overscroll-behavior: none;
  max-width: 920px; width: 100%; margin: 0 auto;
  padding: var(--space-4);
  display: flex; flex-direction: column; gap: var(--space-3);
}
.msg { max-width: 92%; }
.msg--user { align-self: flex-end; }
.bubble-user {
  padding: 10px 16px;
  background: var(--md-primary-container); color: var(--md-on-primary-container);
  border-radius: var(--md-radius-l) var(--md-radius-l) var(--md-radius-s) var(--md-radius-l);
  white-space: pre-wrap; word-break: break-word; line-height: 1.6;
}
.msg--assistant { align-self: stretch; display: flex; flex-direction: column; gap: var(--space-2); }
.bubble-assistant { padding: 12px 16px; white-space: pre-wrap; word-break: break-word; line-height: 1.7; }
.msg-meta { font-size: var(--type-label); color: var(--text-muted); }
.run-state {
  display: flex; align-items: center; gap: 5px;
  font-size: 11px; color: var(--text-muted);
}
.run-state .muted { color: var(--text-muted); }

.thinking { border-radius: var(--md-radius-m); overflow: hidden; }
.thinking summary {
  cursor: pointer; padding: 8px 12px; font-size: 13px; color: var(--text-secondary);
  display: flex; align-items: center; gap: 6px; list-style: none;
}
.thinking summary::-webkit-details-marker { display: none; }
.thinking summary:hover { background: var(--bg-hover); }
.thinking .raw {
  padding: 8px 12px; font-size: 12px; color: var(--text-muted);
  white-space: pre-wrap; max-height: 200px; overflow: auto;
  border-top: 1px dashed var(--border-subtle);
  font-family: var(--font-mono);
}

.composer-zone { flex: 0 0 auto; padding: var(--space-2) var(--space-4) var(--space-1); }
.mode-row { display: flex; gap: var(--space-2); max-width: 920px; margin: 0 auto var(--space-2); flex-wrap: wrap; }
.composer {
  max-width: 920px; margin: 0 auto;
  display: flex; align-items: flex-end; gap: var(--space-2);
  padding: var(--space-2); border-radius: var(--md-radius-xl);
}
.composer__field { flex: 1; min-width: 0; }
.composer__field :deep(.md-field__box--area) { background: transparent; }
.composer__field :deep(.md-field__area) { padding-right: 0; }
.send-btn {
  width: 48px; height: 48px; flex: 0 0 auto;
  display: grid; place-items: center;
  border-radius: var(--md-radius-full);
  background: var(--md-primary); color: var(--md-on-primary);
  transition: transform var(--motion-press-down), opacity var(--motion-fade-in),
              background-color var(--motion-fade-in);
}
.send-btn:hover:not(:disabled) { box-shadow: var(--md-elev-1); }
.send-btn:active:not(:disabled) { transform: scale(1.03); }
.send-btn:disabled { background: var(--md-disabled-container); color: var(--md-disabled-content); }

@media (max-width: 768px) {
  .chat__list { padding: var(--space-3); }
  .msg { max-width: 100%; }
  .composer-zone { padding: var(--space-2) var(--space-3) 0; }
}
</style>
