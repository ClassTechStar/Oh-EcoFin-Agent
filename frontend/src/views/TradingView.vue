<script setup lang="ts">
/** 交易看板：M7 信号合成 + M10 仿真执行（同一标的、同一风险等级口径）。
 *  约束：下单价格由后端按快照决定；前端只做展示与参数输入，不参与成交定价。 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { api } from '../sse'
import { useToast } from '../toast'
import MdCard from '../components/MdCard.vue'
import MdChip from '../components/MdChip.vue'
import MdButton from '../components/MdButton.vue'
import MdTextField from '../components/MdTextField.vue'
import MdEmptyState from '../components/MdEmptyState.vue'
import MdIcon from '../components/MdIcon.vue'

const { show } = useToast()

const symbol = ref('600519')
const riskLevel = ref('R3')
const mode = ref<'long_only' | 'long_short' | 'dollar_neutral'>('long_only')
const gross = ref(0.8)
const side = ref<'buy' | 'sell'>('buy')
const qty = ref('100')

const account = ref<any>(null)
const orders = ref<any[]>([])
const events = ref<any[]>([])
const quote = ref<any>(null)
const signals = ref<any>(null)

const loadingAccount = ref(false)
const runningSignals = ref(false)
const submitting = ref(false)
const quoteError = ref('')

const RISK_LEVELS = ['R1', 'R2', 'R3', 'R4', 'R5']
const MODES: Array<{ key: 'long_only' | 'long_short' | 'dollar_neutral'; label: string }> = [
  { key: 'long_only', label: '纯多头' },
  { key: 'long_short', label: '多空' },
  { key: 'dollar_neutral', label: '多空中性' }
]
const FLAT_TEXT: Record<string, string> = {
  no_eligible_positions: '无可用观点（模型全部弃权/中性）',
  missing_long_side: '缺少多头一侧，拒绝半仓',
  missing_short_side: '缺少空头一侧，拒绝半仓'
}

const validSymbol = computed(() => /^\d{6}$/.test(symbol.value))
const qtyNum = computed(() => parseInt(qty.value, 10))
const canSubmit = computed(() =>
  validSymbol.value && Number.isFinite(qtyNum.value) && qtyNum.value > 0 && !submitting.value)

function money(v: any): string {
  const n = Number(v ?? 0)
  return `¥${n.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}
function pct(v: any, digits = 2): string {
  const n = Number(v ?? 0)
  return `${n >= 0 ? '+' : ''}${(n * 100).toFixed(digits)}%`
}
function signed(v: any, digits = 2): string {
  const n = Number(v ?? 0)
  return `${n >= 0 ? '+' : ''}${n.toFixed(digits)}`
}
function tone(v: any): string {
  const n = Number(v ?? 0)
  if (n > 1e-9) return 'pct-up'
  if (n < -1e-9) return 'pct-down'
  return ''
}
function flatText(reason?: string | null): string {
  return reason ? (FLAT_TEXT[reason] || reason) : ''
}
function errText(e: any): string {
  return String(e?.message || e || '未知错误')
}

async function refreshAccount() {
  loadingAccount.value = true
  try { account.value = await api.paperAccount(riskLevel.value) }
  catch (e) { show(`账户加载失败：${errText(e)}`, 'error') }
  finally { loadingAccount.value = false }
}

async function refreshLedger() {
  try {
    const [o, ev] = await Promise.all([api.paperOrders(20), api.paperRiskEvents(50)])
    orders.value = o.orders || []
    events.value = ev.events || []
  } catch { /* 静默：主账户已提示 */ }
}

async function refreshAll() {
  await Promise.all([refreshAccount(), refreshLedger()])
}

async function refreshQuote() {
  quoteError.value = ''
  if (!validSymbol.value) { quote.value = null; return }
  try { quote.value = (await api.paperQuote(symbol.value)).quote }
  catch (e) { quote.value = null; quoteError.value = errText(e) }
}

async function runSignals() {
  if (!validSymbol.value) return
  runningSignals.value = true
  try {
    signals.value = await api.signals(symbol.value, mode.value, gross.value)
    show(`信号合成完成：${signals.value.as_of}`, 'success')
  } catch (e) {
    signals.value = null
    show(`信号运行失败：${errText(e)}`, 'error')
  } finally { runningSignals.value = false }
}

async function submitOrder() {
  if (!canSubmit.value) return
  submitting.value = true
  try {
    const r = await api.paperOrder({
      symbol: symbol.value,
      side: side.value,
      qty: qtyNum.value,
      risk_level: riskLevel.value,
      strategy_id: 'manual'
    })
    account.value = r.account
    quote.value = r.quote
    if (r.ok) {
      show(`${side.value === 'buy' ? '买入' : '卖出'}成交 ${r.order?.fill?.qty ?? qtyNum.value} 股`, 'success')
    } else {
      show(`委托被拒：${r.order?.reason || '风控/撮合拒绝'}`, 'error', 5000)
    }
    await refreshLedger()
  } catch (e) {
    show(`下单失败：${errText(e)}`, 'error')
  } finally { submitting.value = false }
}

async function rollDay() {
  try {
    const r = await api.paperRollDay(riskLevel.value)
    account.value = r.account
    show('已模拟交易日切换：T+1 持仓解冻，日初权益重置', 'success')
    await refreshLedger()
  } catch (e) { show(`切换失败：${errText(e)}`, 'error') }
}

watch(riskLevel, () => { refreshAccount() })
let symbolTimer: ReturnType<typeof setTimeout> | null = null
watch(symbol, () => {
  signals.value = null
  quote.value = null
  if (symbolTimer) clearTimeout(symbolTimer)
  symbolTimer = setTimeout(refreshQuote, 400)
})

onMounted(async () => {
  await refreshAll()
  await refreshQuote()
})
onUnmounted(() => { if (symbolTimer) clearTimeout(symbolTimer) })
</script>

<template>
  <div class="trading">
    <!-- 标的与风险等级（信号/下单共用口径） -->
    <MdCard variant="filled" class="toolbar">
      <div class="toolbar__main">
        <div class="toolbar__field">
          <MdTextField v-model="symbol" icon="trading" label="标的代码"
                       placeholder="6 位代码，如 600519"
                       supporting="信号与仿真下单共用；代码变化会自动刷新报价"
                       @enter="refreshQuote" />
        </div>
        <div class="toolbar__risk">
          <span class="toolbar__label">风险等级</span>
          <div class="toolbar__chips">
            <MdChip v-for="lvl in RISK_LEVELS" :key="lvl" :selected="riskLevel === lvl"
                    @click="riskLevel = lvl">{{ lvl }}</MdChip>
          </div>
        </div>
        <MdButton icon="refresh" :loading="loadingAccount" @click="refreshAll">刷新账户</MdButton>
      </div>
    </MdCard>

    <!-- 账户概览 -->
    <div v-if="account" class="stats">
      <MdCard variant="elevated" class="stat">
        <div class="stat__label"><MdIcon name="dashboard" :size="16" /> 总权益</div>
        <div class="stat__value num">{{ money(account.equity) }}</div>
        <div class="stat__sub">初始 {{ money(account.initial_cash) }} · 成交 {{ account.fills }} 笔</div>
      </MdCard>
      <MdCard variant="elevated" class="stat">
        <div class="stat__label"><MdIcon name="info" :size="16" /> 可用资金</div>
        <div class="stat__value num">{{ money(account.cash) }}</div>
        <div class="stat__sub">持仓市值 {{ money(account.market_value) }}</div>
      </MdCard>
      <MdCard variant="elevated" class="stat">
        <div class="stat__label"><MdIcon name="trading" :size="16" /> 仓位</div>
        <div class="stat__value num">{{ pct(account.position_ratio, 1) }}</div>
        <div class="stat__sub">单标的上限 {{ pct(account.caps?.single?.[riskLevel] ?? 0, 0) }} · 股票上限 {{ pct(account.caps?.equity?.[riskLevel] ?? 0, 0) }}</div>
      </MdCard>
      <MdCard variant="elevated" class="stat">
        <div class="stat__label"><MdIcon name="trend-up" :size="16" /> 当日盈亏</div>
        <div class="stat__value num" :class="tone(account.daily_pnl)">{{ signed(account.daily_pnl) }}</div>
        <div class="stat__sub">
          <span :class="account.reconcile?.ok ? 'pct-down' : 'pct-up'">
            {{ account.reconcile?.ok ? '每日对账通过' : `对账不平 ${signed(account.reconcile?.diff, 4)}` }}
          </span>
        </div>
      </MdCard>
    </div>

    <div class="grid">
      <!-- M7 信号合成 -->
      <MdCard variant="outlined" class="panel">
        <div class="panel__head">
          <div>
            <h3 class="panel__title">M7 信号合成</h3>
            <p class="panel__hint">内置模型并行取信号 → 确定性加权合成 → 约束层（单标的上限 25%）</p>
          </div>
          <MdButton icon="spark" :loading="runningSignals" :disabled="!validSymbol" @click="runSignals">
            运行信号
          </MdButton>
        </div>

        <div class="controls">
          <span class="controls__label">组合模式</span>
          <MdChip v-for="m in MODES" :key="m.key" :selected="mode === m.key"
                  @click="mode = m.key">{{ m.label }}</MdChip>
          <span class="controls__label controls__label--gap">目标仓位</span>
          <MdChip v-for="g in [0.5, 0.8, 1.0]" :key="g" :selected="gross === g"
                  @click="gross = g">{{ Math.round(g * 100) }}%</MdChip>
        </div>

        <MdEmptyState v-if="!signals" icon="spark" title="尚未运行信号"
          description="选择标的与组合模式后点击「运行信号」：三个 Alpha 模型分别给出点位观点，弃权会显式标注，空头证据仅多空模式生效" />

        <template v-else>
          <div class="signal-meta">
            <span class="signal-meta__item num">数据截至 {{ signals.as_of }}</span>
            <MdChip icon="check">落库 {{ signals.saved_signals }} 条</MdChip>
            <span v-if="signals.flat_reason" class="signal-meta__flat">
              <MdIcon name="warning" :size="14" /> {{ flatText(signals.flat_reason) }}
            </span>
          </div>

          <div class="model-list">
            <div v-for="s in signals.signals" :key="s.model_name" class="model-row">
              <div class="model-row__head">
                <span class="model-row__name">{{ s.model_name }}</span>
                <span class="model-row__value num" :class="tone(s.value)">{{ signed(s.value) }}</span>
                <span class="model-row__meta num">置信 {{ s.confidence.toFixed(2) }} · T+{{ s.horizon_days }}</span>
              </div>
              <div class="model-row__bar">
                <span class="model-row__fill" :class="tone(s.value)"
                      :style="{ width: `${Math.min(Math.abs(s.value) * 100, 100)}%` }" />
              </div>
              <div v-if="s.abstained" class="model-row__abstain">
                <MdIcon name="info" :size="13" /> 弃权：{{ s.reason }}
              </div>
              <div v-else-if="s.evidence?.[0]?.excerpt" class="model-row__evidence num">
                {{ s.evidence[0].excerpt }}
              </div>
            </div>
          </div>

          <div class="weights">
            <div class="weights__title">目标权重</div>
            <table class="data-table num">
              <thead><tr><th>标的</th><th>合成权重</th><th>约束后</th><th>观点</th></tr></thead>
              <tbody>
                <tr v-for="(w, sym) in signals.weights" :key="sym">
                  <td>{{ sym }}</td>
                  <td>{{ pct(signals.raw_weights?.[sym] ?? 0) }}</td>
                  <td><strong>{{ pct(w) }}</strong></td>
                  <td :class="tone(signals.convictions?.[sym])">{{ signed(signals.convictions?.[sym]) }}</td>
                </tr>
              </tbody>
            </table>
          </div>

          <div v-if="signals.failures?.length" class="failures">
            <MdIcon name="warning" :size="14" />
            <span v-for="f in signals.failures" :key="f.model + f.symbol">
              {{ f.model }} / {{ f.symbol }}：{{ f.error }}
            </span>
          </div>
        </template>
      </MdCard>

      <!-- M10 仿真下单 -->
      <MdCard variant="outlined" class="panel">
        <div class="panel__head">
          <div>
            <h3 class="panel__title">M10 仿真执行</h3>
            <p class="panel__hint">下单前经 G1 适当性 / G2 资金仓位闸；成交价由服务端快照决定</p>
          </div>
          <MdButton variant="text" icon="refresh" small @click="refreshQuote">报价</MdButton>
        </div>

        <div v-if="quote" class="quote">
          <div class="quote__name">{{ quote.name || quote.symbol }} <span class="num">{{ quote.symbol }}</span></div>
          <div class="quote__price num">{{ quote.price.toFixed(2) }}</div>
          <div class="quote__pct num" :class="tone(quote.pct_chg)">{{ signed(quote.pct_chg) }}%</div>
          <div class="quote__limits num">涨停 {{ quote.limit_up.toFixed(2) }} / 跌停 {{ quote.limit_down.toFixed(2) }}</div>
          <MdChip v-if="quote.suspended" icon="warning">停牌</MdChip>
          <span class="quote__asof num">{{ (quote.data_as_of || '').slice(0, 16) }}</span>
        </div>
        <div v-else-if="quoteError" class="quote-error">
          <MdIcon name="warning" :size="14" /> {{ quoteError }}
        </div>
        <div v-else class="quote quote--empty">输入 6 位代码后刷新报价</div>

        <div class="ticket">
          <div class="ticket__side">
            <MdChip :selected="side === 'buy'" icon="trend-up" @click="side = 'buy'">买入</MdChip>
            <MdChip :selected="side === 'sell'" icon="trend-down" @click="side = 'sell'">卖出</MdChip>
          </div>
          <MdTextField v-model="qty" type="number" label="数量（股）"
                       supporting="买入必须整手（100 股）；卖出受 T+1 可用持仓限制" />
          <MdButton block :loading="submitting" :disabled="!canSubmit" icon="send" @click="submitOrder">
            {{ side === 'buy' ? '提交买入' : '提交卖出' }}
          </MdButton>
          <p class="ticket__hint">
            市价单以最新快照价成交；涨停不可买、跌停不可卖、停牌不成交；
            超过风险等级/资金/仓位上限时自动减量或拒绝，决策全量落库。
          </p>
        </div>

        <div class="demo-actions">
          <MdButton variant="tonal" icon="history" small @click="rollDay">模拟 T+1 结算</MdButton>
          <span class="demo-actions__hint">教学演示：解冻当日买入并重置日初权益</span>
        </div>
      </MdCard>
    </div>

    <!-- 持仓 -->
    <MdCard variant="outlined" class="panel">
      <div class="panel__head">
        <h3 class="panel__title">当前持仓</h3>
        <span class="panel__hint num">现金 + 市值 = 期初 + 已实现 + 浮动 − 费用</span>
      </div>
      <MdEmptyState v-if="!account?.positions?.length" icon="dashboard" title="暂无持仓"
        description="提交一笔买入委托后，这里会显示持仓数量、可卖数量（T+1）、成本与浮动盈亏" />
      <table v-else class="data-table num">
        <thead>
          <tr><th>标的</th><th>持仓</th><th>可卖</th><th>成本价</th><th>现价</th><th>市值</th><th>浮动盈亏</th></tr>
        </thead>
        <tbody>
          <tr v-for="p in account.positions" :key="p.symbol">
            <td>{{ p.symbol }}</td>
            <td>{{ p.qty }}</td>
            <td>{{ p.available_qty }}</td>
            <td>{{ p.avg_cost.toFixed(2) }}</td>
            <td>{{ p.price.toFixed(2) }}</td>
            <td>{{ money(p.market_value) }}</td>
            <td :class="tone(p.pnl)">
              {{ signed(p.pnl) }} <span class="muted">({{ pct(p.pnl_pct) }})</span>
            </td>
          </tr>
        </tbody>
      </table>
    </MdCard>

    <div class="grid grid--bottom">
      <!-- 委托流水 -->
      <MdCard variant="outlined" class="panel">
        <div class="panel__head">
          <h3 class="panel__title">委托流水</h3>
          <MdButton variant="text" icon="refresh" small @click="refreshLedger">刷新</MdButton>
        </div>
        <MdEmptyState v-if="!orders.length" icon="history" title="暂无委托"
          description="每次下单（含被风控拒绝）都会写入 orders 表，可回溯风控决策与成交价" />
        <div v-else class="scroll-y">
          <table class="data-table num">
            <thead>
              <tr><th>时间</th><th>标的</th><th>方向</th><th>数量</th><th>价格</th><th>状态</th><th>风控</th></tr>
            </thead>
            <tbody>
              <tr v-for="o in orders" :key="o.id">
                <td class="muted">{{ (o.created_at || '').slice(5, 16) }}</td>
                <td>{{ o.symbol }}</td>
                <td :class="o.side === 'buy' ? 'pct-up' : 'pct-down'">
                  {{ o.side === 'buy' ? '买入' : '卖出' }}
                </td>
                <td>{{ o.qty }}</td>
                <td>{{ Number(o.price).toFixed(2) }}</td>
                <td>
                  <span :class="['status', o.status === 'filled' ? 'status--ok' : 'status--reject']">
                    {{ o.status === 'filled' ? '已成交' : '已拒绝' }}
                  </span>
                </td>
                <td class="muted risk-cell">
                  {{ o.risk_decision?.severity || '—' }}
                  <span v-if="o.risk_decision?.reasons?.length">：{{ o.risk_decision.reasons[0] }}</span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </MdCard>

      <!-- 风控事件 -->
      <MdCard variant="outlined" class="panel">
        <div class="panel__head">
          <h3 class="panel__title">风控事件</h3>
          <span class="panel__hint">pass / warn / block 全量落库</span>
        </div>
        <MdEmptyState v-if="!events.length" icon="check" title="暂无风控事件"
          description="G1/G2 每次评估都会写入 risk_events：等级 / 闸门 / 规则 / 详情" />
        <div v-else class="event-list">
          <div v-for="e in events" :key="e.id" class="event">
            <span :class="['event__level', e.level === 'block' ? 'pct-up' : e.level === 'warn' ? 'warn' : 'pct-down']">
              {{ e.level }}
            </span>
            <span class="event__gate num">{{ e.gate }}</span>
            <span class="event__detail">{{ e.detail }}</span>
            <span class="event__time num">{{ (e.created_at || '').slice(5, 16) }}</span>
          </div>
        </div>
      </MdCard>
    </div>

    <p class="foot-note">
      本页为教学演示：仿真账户初始资金 100 万元，进程重启即重置；所有输出不构成投资建议。
    </p>
  </div>
</template>

<style scoped>
.trading {
  flex: 1; overflow-y: auto; overscroll-behavior: none;
  padding: var(--space-4);
  display: flex; flex-direction: column; gap: var(--space-4);
}
.toolbar__main { display: flex; align-items: flex-start; gap: var(--space-4); flex-wrap: wrap; }
.toolbar__field { flex: 1; min-width: 240px; }
.toolbar__risk { display: flex; flex-direction: column; gap: 8px; padding-top: 4px; }
.toolbar__label { font-size: var(--type-label); color: var(--text-secondary); font-weight: 600; }
.toolbar__chips { display: flex; gap: 6px; flex-wrap: wrap; }

.stats { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: var(--space-3); }
.stat { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
.stat__label { display: flex; align-items: center; gap: 6px; font-size: var(--type-label); color: var(--text-secondary); font-weight: 600; }
.stat__value { font-size: 22px; font-weight: 700; letter-spacing: -0.2px; }
.stat__sub { font-size: 11px; color: var(--text-muted); line-height: 1.45; }

.grid { display: grid; grid-template-columns: minmax(0, 1.1fr) minmax(0, 0.9fr); gap: var(--space-4); align-items: start; }
.grid--bottom { grid-template-columns: minmax(0, 1.2fr) minmax(0, 0.8fr); }

.panel { display: flex; flex-direction: column; gap: var(--space-3); min-width: 0; }
.panel__head { display: flex; align-items: flex-start; justify-content: space-between; gap: var(--space-3); flex-wrap: wrap; }
.panel__title { margin: 0; font-size: 16px; }
.panel__hint { margin: 4px 0 0; font-size: 12px; color: var(--text-muted); line-height: 1.5; }

.controls { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.controls__label { font-size: 12px; color: var(--text-muted); }
.controls__label--gap { margin-left: var(--space-3); }

.signal-meta { display: flex; align-items: center; gap: var(--space-3); flex-wrap: wrap; font-size: 12px; color: var(--text-secondary); }
.signal-meta__flat { display: inline-flex; align-items: center; gap: 4px; color: var(--warn); }

.model-list { display: flex; flex-direction: column; gap: var(--space-2); }
.model-row { padding: 8px 0; border-bottom: 1px solid var(--border-subtle); display: flex; flex-direction: column; gap: 5px; }
.model-row:last-child { border-bottom: 0; }
.model-row__head { display: flex; align-items: baseline; gap: var(--space-3); }
.model-row__name { font-size: 13px; font-weight: 600; }
.model-row__value { margin-left: auto; font-weight: 700; font-size: 15px; }
.model-row__meta { font-size: 11px; color: var(--text-muted); }
.model-row__bar { height: 5px; border-radius: 999px; background: var(--md-surface-container-high); overflow: hidden; }
.model-row__fill { display: block; height: 100%; border-radius: inherit; background: currentColor; opacity: 0.75; }
.model-row__abstain { display: flex; align-items: center; gap: 5px; font-size: 11px; color: var(--warn); }
.model-row__evidence { font-size: 11px; color: var(--text-muted); }

.weights { display: flex; flex-direction: column; gap: 6px; }
.weights__title { font-size: 13px; font-weight: 600; }

.failures {
  display: flex; align-items: flex-start; gap: 6px; flex-wrap: wrap;
  font-size: 12px; color: var(--md-error);
  padding: 8px 10px; border-radius: var(--md-radius-m);
  background: color-mix(in srgb, var(--md-error) 8%, transparent);
}

.quote {
  display: flex; align-items: baseline; gap: var(--space-3); flex-wrap: wrap;
  padding: 10px 12px; border-radius: var(--md-radius-m);
  background: var(--md-surface-container);
}
.quote--empty { color: var(--text-muted); font-size: 12px; }
.quote__name { font-size: 14px; font-weight: 600; }
.quote__price { font-size: 26px; font-weight: 700; }
.quote__pct { font-weight: 700; }
.quote__limits { font-size: 11px; color: var(--text-muted); }
.quote__asof { margin-left: auto; font-size: 11px; color: var(--text-muted); }
.quote-error { display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--md-error); }

.ticket { display: flex; flex-direction: column; gap: var(--space-3); }
.ticket__side { display: flex; gap: 8px; }
.ticket__hint { margin: 0; font-size: 11px; color: var(--text-muted); line-height: 1.6; }

.demo-actions { display: flex; align-items: center; gap: var(--space-2); flex-wrap: wrap; }
.demo-actions__hint { font-size: 11px; color: var(--text-muted); }

.data-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.data-table th {
  text-align: left; font-weight: 600; color: var(--text-muted);
  padding: 7px 8px; border-bottom: 1px solid var(--border-default); white-space: nowrap;
}
.data-table td { padding: 7px 8px; border-bottom: 1px solid var(--border-subtle); white-space: nowrap; }
.data-table tr:last-child td { border-bottom: 0; }
.data-table .muted, .muted { color: var(--text-muted); }
.scroll-y { max-height: 280px; overflow-y: auto; }
.risk-cell { max-width: 260px; overflow: hidden; text-overflow: ellipsis; }

.status { display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 11px; font-weight: 600; }
.status--ok { background: var(--down-bg); color: var(--down); }
.status--reject { background: var(--up-bg); color: var(--up); }

.event-list { display: flex; flex-direction: column; max-height: 280px; overflow-y: auto; }
.event { display: flex; align-items: baseline; gap: 8px; padding: 6px 0; border-bottom: 1px solid var(--border-subtle); font-size: 12px; }
.event:last-child { border-bottom: 0; }
.event__level { font-weight: 700; font-size: 11px; text-transform: uppercase; min-width: 38px; }
.event__level.warn { color: var(--warn); }
.event__gate { font-size: 11px; color: var(--text-muted); min-width: 88px; }
.event__detail { flex: 1; color: var(--text-secondary); line-height: 1.5; }
.event__time { font-size: 10px; color: var(--text-muted); }

.foot-note { margin: 0; font-size: 11px; color: var(--text-muted); text-align: center; padding-bottom: var(--space-2); }

@media (max-width: 1080px) {
  .stats { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .grid, .grid--bottom { grid-template-columns: minmax(0, 1fr); }
}
@media (max-width: 768px) {
  .trading { padding: var(--space-3); }
  .stats { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); }
  .data-table { font-size: 11px; }
}
</style>
