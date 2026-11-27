<script setup lang="ts">
/** M11 投顾合规看板：适当性测评 → 五档画像 → 组合建议 → 合规四项审查。 */
import { computed, onMounted, ref } from 'vue'
import { api } from '../sse'
import { useToast } from '../toast'
import MdCard from '../components/MdCard.vue'
import MdChip from '../components/MdChip.vue'
import MdButton from '../components/MdButton.vue'
import MdTextField from '../components/MdTextField.vue'
import MdEmptyState from '../components/MdEmptyState.vue'
import MdIcon from '../components/MdIcon.vue'

const { show } = useToast()

const questions = ref<any[]>([])
const answers = ref<Record<string, string>>({})
const assessment = ref<any>(null)
const assessing = ref(false)

const symbols = ref('600519,000001,601318,300750,510300')
const level = ref('R3')
const portfolio = ref<any>(null)
const loadingPortfolio = ref(false)

const statement = ref('')
const statementResult = ref<any>(null)
const checkingStatement = ref(false)

const RISK_LEVELS = ['R1', 'R2', 'R3', 'R4', 'R5']
const ALL_ANSWERED = computed(() =>
  questions.value.length > 0 && questions.value.every(q => !!answers.value[q.qid]))

const parsedSymbols = computed(() => {
  const list = symbols.value.split(/[,，\s]+/).map(s => s.trim()).filter(Boolean)
  return [...new Set(list)]
})
const symbolError = computed(() => {
  const list = parsedSymbols.value
  if (list.length < 3 || list.length > 10) return '需 3-10 只标的'
  if (list.some(s => !/^\d{6}$/.test(s))) return '标的须为 6 位代码'
  return ''
})

const frontierPath = computed(() => {
  const pts = portfolio.value?.frontier || []
  if (pts.length < 2) return ''
  const vols = pts.map((p: any) => Number(p.vol))
  const rets = pts.map((p: any) => Number(p.ret))
  const minV = Math.min(...vols), maxV = Math.max(...vols)
  const minR = Math.min(...rets), maxR = Math.max(...rets)
  const spanV = maxV - minV || 1
  const spanR = maxR - minR || 1
  return pts.map((p: any, i: number) => {
    const x = 4 + ((Number(p.vol) - minV) / spanV) * 92
    const y = 44 - ((Number(p.ret) - minR) / spanR) * 40
    return `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`
  }).join(' ')
})

function pct(v: any, digits = 2): string {
  const n = Number(v ?? 0)
  return `${n >= 0 ? '+' : ''}${(n * 100).toFixed(digits)}%`
}
function errText(e: any): string {
  return String(e?.message || e || '未知错误')
}
function complianceLabel(status?: string): string {
  return status === 'passed' ? '合规通过' : '需要关注'
}

async function loadQuestions() {
  try {
    const r = await api.riskQuestions()
    questions.value = r.questions || []
  } catch (e) {
    show(`题库加载失败：${errText(e)}`, 'error')
  }
}

async function submitAssessment() {
  if (!ALL_ANSWERED.value) return
  assessing.value = true
  try {
    const payload = questions.value.map(q => ({ qid: q.qid, choice: answers.value[q.qid] }))
    assessment.value = await api.advisorAssess(payload)
    level.value = assessment.value.level
    show(`测评完成：${assessment.value.level} · ${assessment.value.profile.label}`, 'success')
  } catch (e) {
    show(`测评提交失败：${errText(e)}`, 'error')
  } finally { assessing.value = false }
}

async function buildPortfolio() {
  if (symbolError.value) { show(symbolError.value, 'error'); return }
  loadingPortfolio.value = true
  try {
    portfolio.value = await api.advisorPortfolio({
      symbols: parsedSymbols.value, level: level.value
    })
    if (!portfolio.value.ok) {
      show(`组合建议：${portfolio.value.reason || '未通过前置条件'}`, 'error', 5000)
    }
  } catch (e) {
    show(`组合生成失败：${errText(e)}`, 'error')
  } finally { loadingPortfolio.value = false }
}

async function checkStatement() {
  if (!statement.value.trim()) return
  checkingStatement.value = true
  try {
    statementResult.value = await api.advisorStatement(statement.value)
    show(statementResult.value.status === 'passed' ? '表述审查通过' : '表述需要修改',
         statementResult.value.status === 'passed' ? 'success' : 'error', 4000)
  } catch (e) {
    show(`表述审查失败：${errText(e)}`, 'error')
  } finally { checkingStatement.value = false }
}

onMounted(loadQuestions)
</script>

<template>
  <div class="advisor">
    <div class="grid">
      <!-- 风险测评 -->
      <MdCard variant="outlined" class="panel panel--wide">
        <div class="panel__head">
          <div>
            <h3 class="panel__title">适当性测评</h3>
            <p class="panel__hint">5 题（经验/目标/亏损容忍/期限/财务）→ 加权评分 → 五档画像；评分口径不可变</p>
          </div>
          <MdButton icon="shield" :loading="assessing" :disabled="!ALL_ANSWERED"
                    @click="submitAssessment">提交测评</MdButton>
        </div>

        <MdEmptyState v-if="!questions.length" icon="shield" title="题库加载中"
          description="若长时间无响应，请确认后端 /api/risk-questions 可用" />
        <div v-else class="quiz">
          <div v-for="(q, qi) in questions" :key="q.qid" class="quiz__item">
            <div class="quiz__q">
              <span class="quiz__index num">{{ qi + 1 }}</span>
              <span class="quiz__dim">{{ q.dimension }}</span>
              <span class="quiz__text">{{ q.question }}</span>
            </div>
            <div class="quiz__options">
              <MdChip v-for="opt in q.options" :key="opt"
                      :selected="answers[q.qid] === opt"
                      @click="answers[q.qid] = opt">{{ opt }}</MdChip>
            </div>
          </div>
        </div>
      </MdCard>

      <!-- 五档画像 -->
      <MdCard variant="filled" class="panel">
        <div class="panel__head">
          <h3 class="panel__title">风险画像</h3>
          <MdChip v-if="assessment" icon="check">{{ assessment.level }}</MdChip>
        </div>
        <MdEmptyState v-if="!assessment" icon="info" title="尚未测评"
          description="完成左侧 5 题后展示得分、风险等级与权益/单一标的上限" />
        <template v-else>
          <div class="profile">
            <div class="profile__level">
              <span class="profile__label">{{ assessment.profile.label }}</span>
              <span class="profile__score num">{{ assessment.score }} 分</span>
            </div>
            <div class="profile__rows">
              <div><span>权益上限</span><strong class="num">{{ pct(assessment.equity_cap, 0) }}</strong></div>
              <div><span>单一标的上限</span><strong class="num">{{ pct(assessment.single_cap, 0) }}</strong></div>
              <div><span>可承受回撤</span><strong class="num">{{ assessment.profile.drawdown }}</strong></div>
              <div><span>建议期限</span><strong>{{ assessment.profile.horizon }}</strong></div>
              <div><span>对标</span><strong>{{ assessment.profile.benchmark }}</strong></div>
            </div>
          </div>
          <p class="profile__note">
            R1 不配置股票；权益与单一上限同时进入下单 G1 闸。等级不是收益承诺，市场有风险。
          </p>
        </template>
      </MdCard>
    </div>

    <div class="grid grid--advice">
      <!-- 组合建议 -->
      <MdCard variant="outlined" class="panel">
        <div class="panel__head">
          <div>
            <h3 class="panel__title">组合建议</h3>
            <p class="panel__hint">Markowitz + Ledoit-Wolf；超权益上限自动配置国债兜底仓</p>
          </div>
          <MdButton icon="dashboard" :loading="loadingPortfolio" :disabled="!!symbolError"
                    @click="buildPortfolio">生成组合</MdButton>
        </div>

        <MdTextField v-model="symbols" icon="trading" label="候选标的池"
                     :error="symbolError"
                     supporting="3-10 只 6 位代码，逗号分隔；默认含国债 ETF 510300" />

        <div class="controls">
          <span class="controls__label">风险等级</span>
          <MdChip v-for="lvl in RISK_LEVELS" :key="lvl" :selected="level === lvl"
                  @click="level = lvl">{{ lvl }}</MdChip>
        </div>

        <MdEmptyState v-if="!portfolio" icon="dashboard" title="尚未生成组合"
          description="输入标的池与风险等级后生成建议；权重、有效前沿与合规审查结果会一并返回" />
        <template v-else>
          <div class="advice-head">
            <span :class="['status', portfolio.status === 'passed' ? 'status--ok' : 'status--warn']">
              {{ complianceLabel(portfolio.status) }}
            </span>
            <span v-if="portfolio.point" class="num advice-head__stat">
              预期收益 {{ pct(portfolio.point.ret) }} · 波动 {{ pct(portfolio.point.vol) }} ·
              夏普 {{ Number(portfolio.point.sharpe).toFixed(2) }}
            </span>
          </div>

          <svg v-if="frontierPath" class="frontier" viewBox="0 0 100 48" preserveAspectRatio="none"
               role="img" aria-label="有效前沿">
            <path :d="frontierPath" fill="none" stroke="var(--md-primary)" stroke-width="1.6" />
          </svg>

          <table class="data-table num">
            <thead><tr><th>标的</th><th>权重</th><th>配置</th></tr></thead>
            <tbody>
              <tr v-for="(w, sym) in portfolio.weights" :key="sym">
                <td>{{ sym }}</td>
                <td>{{ pct(w) }}</td>
                <td class="bar-cell">
                  <span class="bar"><span class="bar__fill" :style="{ width: `${Math.min(Number(w) * 100, 100)}%` }" /></span>
                </td>
              </tr>
            </tbody>
          </table>
          <p v-if="portfolio.defensive_sleeve?.length" class="foot-note">
            防御性仓位：{{ portfolio.defensive_sleeve.join('、') }}
          </p>
        </template>
      </MdCard>

      <!-- 合规审查 -->
      <MdCard variant="outlined" class="panel">
        <div class="panel__head">
          <div>
            <h3 class="panel__title">合规审查</h3>
            <p class="panel__hint">适当性 / 行业集中度 / 收益表述 / 免责声明</p>
          </div>
          <MdChip v-if="portfolio?.compliance_report"
                  :icon="portfolio.compliance_report.status === 'passed' ? 'check' : 'warning'"
                  :selected="portfolio.compliance_report.status === 'passed'">
            {{ complianceLabel(portfolio.compliance_report.status) }}
          </MdChip>
        </div>

        <div v-if="portfolio?.compliance_report" class="violations">
          <div v-if="!portfolio.compliance_report.violations.length" class="violations__empty">
            <MdIcon name="check" :size="15" /> 未发现合规问题
          </div>
          <div v-for="(v, i) in portfolio.compliance_report.violations" :key="i" class="violation">
            <span :class="['violation__level', v.severity === 'block' ? 'pct-up' : 'warn']">
              {{ v.severity }}
            </span>
            <span class="violation__rule num">{{ v.rule }}</span>
            <span class="violation__detail">{{ v.detail }}</span>
          </div>
        </div>
        <MdEmptyState v-else icon="info" title="先生成组合"
          description="生成组合后这里展示逐项合规检查；问题会标明规则、等级与原因" />

        <div class="statement">
          <MdTextField v-model="statement" multiline :rows="2" label="收益表述自查"
                       placeholder="粘贴宣传语/建议文案，如「本产品保证收益」"
                       supporting="确定性词表扫描：承诺收益/保本/无风险等表述直接拦截" />
          <MdButton variant="tonal" icon="shield" :loading="checkingStatement"
                    :disabled="!statement.trim()" @click="checkStatement">审查表述</MdButton>
          <div v-if="statementResult" class="statement__result">
            <span :class="['status', statementResult.status === 'passed' ? 'status--ok' : 'status--warn']">
              {{ complianceLabel(statementResult.status) }}
            </span>
            <span v-for="(v, i) in statementResult.violations" :key="i" class="statement__item">
              {{ v.detail }}
            </span>
          </div>
        </div>

        <p class="disclaimer">
          本页为教学演示：测评/组合/合规结果不构成投资建议；数据可能延迟或有误，请以交易所官方信息为准。
        </p>
      </MdCard>
    </div>
  </div>
</template>

<style scoped>
.advisor {
  flex: 1; overflow-y: auto; overscroll-behavior: none;
  padding: var(--space-4);
  display: flex; flex-direction: column; gap: var(--space-4);
}
.grid { display: grid; grid-template-columns: minmax(0, 1.15fr) minmax(0, 0.85fr); gap: var(--space-4); align-items: start; }
.grid--advice { grid-template-columns: minmax(0, 1.05fr) minmax(0, 0.95fr); }

.panel { display: flex; flex-direction: column; gap: var(--space-3); min-width: 0; }
.panel__head { display: flex; align-items: flex-start; justify-content: space-between; gap: var(--space-3); flex-wrap: wrap; }
.panel__title { margin: 0; font-size: 16px; }
.panel__hint { margin: 4px 0 0; font-size: 12px; color: var(--text-muted); line-height: 1.5; }

.quiz { display: flex; flex-direction: column; gap: var(--space-3); }
.quiz__item { display: flex; flex-direction: column; gap: 6px; padding-bottom: var(--space-3); border-bottom: 1px solid var(--border-subtle); }
.quiz__item:last-child { border-bottom: 0; padding-bottom: 0; }
.quiz__q { display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; }
.quiz__index { width: 20px; height: 20px; display: inline-grid; place-items: center; border-radius: 50%; background: var(--md-secondary-container); color: var(--md-on-secondary-container); font-size: 11px; font-weight: 700; }
.quiz__dim { font-size: 11px; color: var(--text-muted); }
.quiz__text { font-size: 13px; font-weight: 600; }
.quiz__options { display: flex; flex-wrap: wrap; gap: 6px; }

.profile { display: flex; flex-direction: column; gap: var(--space-3); }
.profile__level { display: flex; align-items: baseline; justify-content: space-between; gap: var(--space-3); }
.profile__label { font-size: 24px; font-weight: 700; }
.profile__score { font-size: 14px; color: var(--text-secondary); }
.profile__rows { display: flex; flex-direction: column; gap: 6px; }
.profile__rows > div { display: flex; justify-content: space-between; gap: var(--space-3); font-size: 12px; padding: 5px 0; border-bottom: 1px solid var(--border-subtle); }
.profile__rows > div:last-child { border-bottom: 0; }
.profile__rows span { color: var(--text-muted); }
.profile__note { margin: 0; font-size: 11px; color: var(--text-muted); line-height: 1.6; }

.controls { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.controls__label { font-size: 12px; color: var(--text-muted); }

.advice-head { display: flex; align-items: center; gap: var(--space-3); flex-wrap: wrap; }
.advice-head__stat { font-size: 11px; color: var(--text-muted); }
.frontier { width: 100%; height: 64px; background: var(--md-surface-container); border-radius: var(--md-radius-m); padding: 4px; }

.data-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.data-table th { text-align: left; font-weight: 600; color: var(--text-muted); padding: 6px 8px; border-bottom: 1px solid var(--border-default); }
.data-table td { padding: 6px 8px; border-bottom: 1px solid var(--border-subtle); white-space: nowrap; }
.data-table tr:last-child td { border-bottom: 0; }
.bar-cell { width: 45%; }
.bar { display: block; height: 6px; border-radius: 999px; background: var(--md-surface-container-high); overflow: hidden; }
.bar__fill { display: block; height: 100%; background: var(--md-primary); border-radius: inherit; }

.status { display: inline-block; padding: 2px 9px; border-radius: 999px; font-size: 11px; font-weight: 600; }
.status--ok { background: var(--down-bg); color: var(--down); }
.status--warn { background: color-mix(in srgb, var(--warn) 12%, transparent); color: var(--warn); }

.violations { display: flex; flex-direction: column; gap: 6px; }
.violations__empty { display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--down); }
.violation { display: flex; align-items: baseline; gap: 8px; font-size: 12px; line-height: 1.5; }
.violation__level { font-size: 10px; font-weight: 700; text-transform: uppercase; }
.violation__level.warn { color: var(--warn); }
.violation__rule { font-size: 11px; color: var(--text-muted); min-width: 108px; }
.violation__detail { flex: 1; color: var(--text-secondary); }

.statement { display: flex; flex-direction: column; gap: var(--space-2); padding-top: var(--space-3); border-top: 1px solid var(--border-default); }
.statement__result { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-size: 11px; color: var(--text-secondary); }
.statement__item { color: var(--warn); }
.disclaimer { margin: 0; font-size: 11px; color: var(--text-muted); line-height: 1.6; }
.foot-note { margin: 0; font-size: 11px; color: var(--text-muted); }

@media (max-width: 1080px) {
  .grid, .grid--advice { grid-template-columns: minmax(0, 1fr); }
}
@media (max-width: 768px) {
  .advisor { padding: var(--space-3); }
}
</style>
