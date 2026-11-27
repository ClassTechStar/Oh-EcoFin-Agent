/** SSE v2 客户端：POST /api/chat 主通道 + GET /api/chat/stream 断线重连重放。
 *  事件 id 严格递增；Last-Event-ID 重放由服务端处理（环形缓冲 + SQLite 兜底）。 */

export type SSEHandler = (event: string, payload: any) => void

const BASE = ''  // 同源（dev 走 vite proxy，生产同源 /static 托管）

/** 给任意路径追加局域网令牌（已有 query 时用 &）。 */
function withToken(path: string): string {
  const t = new URLSearchParams(location.search).get('t')
  if (!t) return path
  return `${path}${path.includes('?') ? '&' : '?'}t=${encodeURIComponent(t)}`
}

/** 主对话通道：POST /api/chat → SSE 流（含 done/error 终态）。 */
export async function chatStream(
  message: string,
  sessionId: string,
  mode: string,
  onEvent: SSEHandler,
  signal?: AbortSignal
): Promise<void> {
  const resp = await fetch(withToken(`${BASE}/api/chat`), {
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
    const path = `/api/chat/stream?sid=${encodeURIComponent(sessionId)}&last_event_id=${lastEventId}`
    const resp = await fetch(withToken(`${BASE}${path}`), {
      headers: { 'Last-Event-ID': String(lastEventId) }, signal: ctrl.signal
    })
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

async function errorMessage(resp: Response, path: string): Promise<string> {
  let detail: any = `${resp.status}`
  try { detail = (await resp.json()).detail ?? detail } catch { /* ignore */ }
  const message = typeof detail === 'string'
    ? detail
    : (detail?.message ?? JSON.stringify(detail))
  const hint = typeof detail === 'object' && detail?.hint ? `（${detail.hint}）` : ''
  return `${path}: ${message}${hint}`
}

async function rest<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(withToken(`${BASE}/api${path}`), init)
  if (!resp.ok) throw new Error(await errorMessage(resp, path))
  return resp.json()
}

export async function uploadDataset(file: File): Promise<any> {
  const form = new FormData()
  form.append('file', file)
  const resp = await fetch(withToken(`${BASE}/api/datasets`), {
    method: 'POST',
    body: form
  })
  if (!resp.ok) throw new Error(await errorMessage(resp, '/datasets'))
  return resp.json()
}

export const api = {
  health: () => rest<any>('/health'),
  createSession: () => rest<any>('/sessions', { method: 'POST' }),
  listSessions: () => rest<any>('/sessions'),
  messages: (sid: string) => rest<any>(`/sessions/${sid}/messages`),
  costSummary: () => rest<any>('/cost/summary'),
  sendFeedback: (body: { session_id: string; run_id?: string; rating: 'up' | 'down'; comment?: string }) =>
    rest<any>('/feedback', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }),
  riskQuestions: () => rest<any>('/risk-questions'),
  decisions: (symbol: string) => rest<any>(`/decisions/${symbol}`),
  signals: (symbol: string, mode = 'long_only', gross = 0.8) =>
    rest<any>(`/signals/${symbol}?mode=${encodeURIComponent(mode)}&gross=${gross}`),
  signalHistory: (symbol = '', limit = 50) =>
    rest<any>(`/signals/history?symbol=${encodeURIComponent(symbol)}&limit=${limit}`),
  paperQuote: (symbol: string) => rest<any>(`/paper/quote/${symbol}`),
  paperAccount: (riskLevel = 'R3') =>
    rest<any>(`/paper/account?risk_level=${encodeURIComponent(riskLevel)}`),
  paperOrder: (body: any) =>
    rest<any>('/paper/orders', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }),
  paperOrders: (limit = 20) => rest<any>(`/paper/orders?limit=${limit}`),
  paperRiskEvents: (limit = 50) => rest<any>(`/paper/risk-events?limit=${limit}`),
  paperRollDay: (riskLevel = 'R3') =>
    rest<any>(`/paper/roll-day?risk_level=${encodeURIComponent(riskLevel)}`, { method: 'POST' }),
  advisorAssess: (answers: any[]) =>
    rest<any>('/advisor/assess', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ answers })
    }),
  advisorPortfolio: (body: any) =>
    rest<any>('/advisor/portfolio', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }),
  advisorStatement: (text: string) =>
    rest<any>('/advisor/statement', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text })
    }),
  liveStatus: () => rest<any>('/live/status'),
  liveProbe: () => rest<any>('/live/probe', { method: 'POST' }),
  datasets: () => rest<any>('/datasets'),
  datasetProfile: (datasetId: string) =>
    rest<any>(`/datasets/${encodeURIComponent(datasetId)}/profile`),
  datasetVersions: (datasetId: string) =>
    rest<any>(`/datasets/${encodeURIComponent(datasetId)}/versions`),
  catalogStats: () => rest<any>('/catalog/stats'),
  catalogSearch: (params: Record<string, string | number>) => {
    const qs = new URLSearchParams(
      Object.entries(params).map(([key, value]) => [key, String(value)])).toString()
    return rest<any>(`/catalog/search?${qs}`)
  },
  catalogImport: (body: any) =>
    rest<any>('/catalog/import', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }),
  runs: (limit = 50) => rest<any>(`/runs?limit=${limit}`),
  runDetail: (runId: string) => rest<any>(`/runs/${encodeURIComponent(runId)}`),
  analysisModules: () => rest<any>('/analysis/modules'),
  analysisRun: (body: any) =>
    rest<any>('/analysis/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }),
  reports: (limit = 50) => rest<any>(`/reports?limit=${limit}`),
  report: (reportId: string) => rest<any>(`/reports/${encodeURIComponent(reportId)}`),
  composeReport: (body: any) =>
    rest<any>('/reports', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }),
  reportFileUrl: (reportId: string, format: string) =>
    withToken(`${BASE}/api/reports/${encodeURIComponent(reportId)}/export/${format}`),
  rerunReport: (reportId: string) =>
    rest<any>(`/reports/${encodeURIComponent(reportId)}/rerun`, { method: 'POST' }),
  auditReports: (limit = 50) => rest<any>(`/audit/reports?limit=${limit}`),
  auditReport: (reportId: string) =>
    rest<any>(`/audit/reports/${encodeURIComponent(reportId)}`),
  uploadDataset,
  resume: (sessionId: string, qid: string, choice: string) =>
    rest<any>('/chat/resume', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId, qid, choice })
    })
}
