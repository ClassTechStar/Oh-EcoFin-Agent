/** SSE v2 客户端：POST /api/chat 主通道 + GET /api/chat/stream 断线重连重放。
 *  事件 id 严格递增；Last-Event-ID 重放由服务端处理（环形缓冲 + SQLite 兜底）。 */

export type SSEHandler = (event: string, payload: any) => void

const BASE = ''  // 同源（dev 走 vite proxy，生产同源 /static 托管）

function qsToken(): string {
  const t = new URLSearchParams(location.search).get('t')
  return t ? `?t=${encodeURIComponent(t)}` : ''
}

/** 主对话通道：POST /api/chat → SSE 流（含 done/error 终态）。 */
export async function chatStream(
  message: string,
  sessionId: string,
  mode: string,
  onEvent: SSEHandler,
  signal?: AbortSignal
): Promise<void> {
  const resp = await fetch(`${BASE}/api/chat${qsToken()}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message, session_id: sessionId, mode }),
    signal
  })
  if (!resp.ok || !resp.body) {
    let detail = `${resp.status}`
    try { detail = (await resp.json()).detail ?? detail } catch { /* ignore */ }
    onEvent('error', { code: 'HTTP', message: `请求失败: ${detail}`, recoverable: true })
    return
  }
  await consume(resp.body, onEvent)
}

/** 断线重连通道：GET /api/chat/stream?sid=...（Last-Event-ID 重放）。 */
export async function resumeStream(
  sessionId: string,
  lastEventId: number,
  onEvent: SSEHandler,
  maxSeconds = 60
): Promise<void> {
  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), maxSeconds * 1000)
  try {
    const resp = await fetch(
      `${BASE}/api/chat/stream?sid=${encodeURIComponent(sessionId)}&last_event_id=${lastEventId}${qsToken()}`,
      { headers: { 'Last-Event-ID': String(lastEventId) }, signal: ctrl.signal }
    )
    if (!resp.ok || !resp.body) return
    await consume(resp.body, onEvent)
  } catch { /* 超时即退出 */ }
  finally { clearTimeout(timer) }
}

async function consume(body: ReadableStream<Uint8Array>, onEvent: SSEHandler) {
  const reader = body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    const frames = buf.split('\n\n')
    buf = frames.pop() ?? ''
    for (const frame of frames) parseFrame(frame, onEvent)
  }
  if (buf.trim()) parseFrame(buf, onEvent)
}

function parseFrame(frame: string, onEvent: SSEHandler) {
  let id = 0, event = 'message', data = ''
  for (const line of frame.split('\n')) {
    if (line.startsWith('id:')) id = parseInt(line.slice(3).trim(), 10)
    else if (line.startsWith('event:')) event = line.slice(6).trim()
    else if (line.startsWith('data:')) data += line.slice(5).trim()
  }
  if (!data) return
  try { onEvent(event, { id, ...JSON.parse(data) }) } catch { onEvent('error', { message: data }) }
}

/** 网络切换（WiFi↔蜂窝）自动重连：指数退避 + Last-Event-ID 重放。 */
export function autoReconnect(
  sessionId: string,
  state: { lastEventId: number; running: boolean },
  onEvent: SSEHandler
): void {
  let attempt = 0
  const loop = () => {
    if (!state.running) return
    resumeStream(sessionId, state.lastEventId, (ev, payload) => {
      attempt = 0
      onEvent(ev, payload)
      if (ev === 'done' || ev === 'error') state.running = false
    }, 30).finally(() => {
      if (state.running) {
        attempt = Math.min(attempt + 1, 5)
        setTimeout(loop, 500 * Math.pow(2, attempt))
      }
    })
  }
  loop()
}

// ---------- REST ----------

async function rest<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(`${BASE}/api${path}${qsToken()}`, init)
  if (!resp.ok) throw new Error(`${path}: ${resp.status}`)
  return resp.json()
}

export const api = {
  health: () => rest<any>('/health'),
  createSession: () => rest<any>('/sessions', { method: 'POST' }),
  listSessions: () => rest<any>('/sessions'),
  messages: (sid: string) => rest<any>(`/sessions/${sid}/messages`),
  costSummary: () => rest<any>('/cost/summary'),
  riskQuestions: () => rest<any>('/risk-questions'),
  decisions: (symbol: string) => rest<any>(`/decisions/${symbol}`),
  resume: (sessionId: string, qid: string, choice: string) =>
    rest<any>('/chat/resume', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId, qid, choice })
    })
}
