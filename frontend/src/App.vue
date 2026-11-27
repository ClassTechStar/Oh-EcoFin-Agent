<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { api, chatStream } from './sse'
import ToolTimeline from './components/ToolTimeline.vue'
import ChartRenderer from './components/ChartRenderer.vue'
import AskUserCard from './components/AskUserCard.vue'
import ReportView from './components/ReportView.vue'
import DisclaimerFooter from './components/DisclaimerFooter.vue'

interface ToolCallItem { id: string; name: string; status: string; args: string; result_digest?: string; ms?: number; cached?: boolean }
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
}

const messages = ref<ChatMessage[]>([])
const input = ref('')
const running = ref(false)
const sessionId = ref('')
const mode = ref('auto')
const health = ref<any>(null)
const thinkingOpen = ref(true)
const streamRef = ref<ChatMessage | null>(null)

const MODES = [
  { value: 'auto', label: '自动' },
  { value: 'chat', label: '问答' },
  { value: 'debate', label: '多空辩论' },
  { value: 'advisor', label: '智能投顾' }
]

onMounted(async () => {
  try { health.value = await api.health() } catch { /* 后端未启动 */ }
  try {
    const s = await api.createSession()
    sessionId.value = s.id
  } catch { /* ignore */ }
})

function ensureStream(): ChatMessage {
  if (!streamRef.value) {
    streamRef.value = {
      role: 'assistant', text: '', thinking: '', tools: [], charts: []
    }
    messages.value.push(streamRef.value)
  }
  return streamRef.value
}

function pctClass(v?: number | string): string {
  const n = typeof v === 'string' ? parseFloat(v) : v
  if (n == null || isNaN(n)) return ''
  // 红涨绿跌：涨红跌绿
  return n >= 0 ? 'pct-up' : 'pct-down'
}

async function send() {
  const text = input.value.trim()
  if (!text || running.value) return
  input.value = ''
  running.value = true
  streamRef.value = null
  messages.value.push({ role: 'user', text })
  const stream = ensureStream()

  try {
    await chatStream(text, sessionId.value, mode.value, (event, payload) => {
      switch (event) {
        case 'thinking_delta':
          stream.thinking = (stream.thinking || '') + payload.text
          break
        case 'message_delta': {
          // 辩论模式 message_delta 是整份 JSON 报告
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
        case 'done':
          stream.meta = `轮次 ${payload.rounds} · 工具 ${payload.tool_stats?.calls ?? 0} 次 · 成本 ¥${payload.cost_breakdown?.total_cny ?? 0}`
          running.value = false
          streamRef.value = null
          break
        case 'error':
          stream.text += `\n\n⚠️ ${payload.message}`
          if (!payload.recoverable) { running.value = false; streamRef.value = null }
          break
        case 'ping':
          break
      }
    })
  } catch (e: any) {
    stream.text += `\n\n⚠️ 连接中断: ${e?.message || e}`
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

function formatArgs(args: string): string {
  try { return JSON.stringify(JSON.parse(args), null, 2) } catch { return args }
}
</script>

<template>
  <header class="topbar">
    <div class="brand">
      <strong>哦经金智能体</strong>
      <span class="sub">Oh EcoFin Agent · 教学演示</span>
    </div>
    <div class="mode-switch">
      <button v-for="m in MODES" :key="m.value" :class="{ primary: mode === m.value }"
              @click="mode = m.value">{{ m.label }}</button>
    </div>
    <div class="health desktop-only">
      <template v-if="health">
        <span :class="`badge-fresh-${health.snapshot?.freshness}`">
          {{ health.snapshot?.freshness === 'green' ? '🟢' : health.snapshot?.freshness === 'yellow' ? '🟡' : '🔴' }}
          快照 {{ health.snapshot?.as_of || '—' }}
        </span>
        <span v-if="health.demo_mode" class="demo-badge">DEMO_MODE</span>
      </template>
      <span v-else class="badge-fresh-red">后端未连接</span>
    </div>
  </header>

  <main class="chat-main">
    <div class="messages">
      <template v-for="(m, i) in messages" :key="i">
        <div v-if="m.role === 'user'" class="msg user card">
          <div class="bubble">{{ m.text }}</div>
        </div>

        <div v-else class="msg assistant">
          <!-- 思考过程（可折叠，答辩演示素材） -->
          <details v-if="m.thinking" class="thinking card" :open="thinkingOpen">
            <summary>💭 思考过程（{{ m.thinking.length }} 字）</summary>
            <div class="raw">{{ m.thinking }}</div>
          </details>

          <!-- 工具调用时间线（折叠面板：名称/参数/耗时/结果摘要） -->
          <ToolTimeline v-if="m.tools?.length" :tools="m.tools" />

          <!-- ask_user 选项卡片组 -->
          <AskUserCard v-if="m.ask" :ask="m.ask" @choose="answerAsk" />

          <!-- 富组件图表（按 kind 路由渲染） -->
          <ChartRenderer v-for="c in m.charts" :key="c.id" :chart="c" />

          <!-- 结构化投研报告（溯源闭环到 UI） -->
          <ReportView v-if="m.report" :report="m.report" />

          <!-- 正文 -->
          <div v-if="m.text" class="bubble card">{{ m.text }}</div>
          <div v-if="m.meta" class="meta num">{{ m.meta }}</div>
        </div>
      </template>

      <div v-if="running && !streamRef?.text" class="skeleton msg" style="height: 64px; width: 70%" />
    </div>
  </main>

  <footer class="inputbar">
    <div class="composer card">
      <textarea v-model="input" :disabled="running"
                :placeholder="running ? '智能体运行中…' : '输入问题，如：600519 值得买吗？/ 帮我做风险测评 / 000001 双均线回测'"
                rows="2" @keydown.enter.exact.prevent="send" />
      <button class="primary" :disabled="running || !input.trim()" @click="send">
        {{ running ? '运行中' : '发送' }}
      </button>
    </div>
    <DisclaimerFooter />
  </footer>
</template>

<style scoped>
.topbar {
  display: flex; align-items: center; gap: 16px;
  padding: 10px 16px; background: var(--surface);
  border-bottom: 1px solid var(--border);
}
.brand strong { font-size: 16px; }
.brand .sub { color: var(--text-3); font-size: 12px; margin-left: 8px; }
.mode-switch { display: flex; gap: 6px; }
.mode-switch button { padding: 4px 12px; font-size: 13px; }
.health { margin-left: auto; font-size: 12px; color: var(--text-2); display: flex; gap: 10px; }
.demo-badge {
  background: var(--accent); color: #fff; border-radius: 4px; padding: 1px 6px; font-size: 11px;
}
.chat-main { flex: 1; overflow-y: auto; }
.messages { max-width: 900px; margin: 0 auto; padding: 16px; display: flex; flex-direction: column; gap: 12px; }
.msg.user { align-self: flex-end; max-width: 80%; }
.msg.user .bubble { background: var(--primary-light); }
.bubble { padding: 10px 14px; white-space: pre-wrap; word-break: break-word; line-height: 1.6; }
.msg.assistant { display: flex; flex-direction: column; gap: 8px; max-width: 92%; }
.thinking summary { cursor: pointer; color: var(--text-2); font-size: 13px; padding: 6px 10px; }
.thinking .raw { padding: 8px 12px; font-size: 12px; color: var(--text-2); white-space: pre-wrap; max-height: 200px; overflow: auto; }
.meta { font-size: 12px; color: var(--text-3); }
.inputbar { padding: 8px 16px 4px; background: var(--surface); border-top: 1px solid var(--border); }
.composer { display: flex; gap: 8px; padding: 8px; max-width: 900px; margin: 0 auto; }
.composer textarea {
  flex: 1; border: none; resize: none; outline: none;
  font-family: inherit; font-size: 14px; background: transparent;
}
@media (max-width: 768px) {
  .messages { padding: 10px; }
  .msg.user, .msg.assistant { max-width: 100%; }
}
</style>
