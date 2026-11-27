<script setup lang="ts">
/** 结构化投研报告视图：sections 渲染 + [n] 引用可点弹出工具调用详情（溯源闭环到 UI）。 */
import { ref, computed } from 'vue'

const props = defineProps<{ report: any }>()
const popup = ref<{ n: number; text: string } | null>(null)

const verdictMap: Record<string, { label: string; cls: string }> = {
  bullish: { label: '看多', cls: 'pct-up' },
  bearish: { label: '看空', cls: 'pct-down' },
  neutral: { label: '中性', cls: '' },
  watch: { label: '观望', cls: '' }
}

const citationMap = computed<Record<number, any>>(() => {
  const map: Record<number, any> = {}
  for (const c of props.report?.citations || []) map[c.n] = c
  return map
})

function escapeHtml(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

/** 转义后把 [n] 包成可点引用（先转义再注入，无 XSS 面）。 */
function renderHtml(text: string): string {
  if (!text) return ''
  return escapeHtml(text).replace(/\[(\d+)\]/g, (m, n) => `<span class="cite" data-n="${n}">[${n}]</span>`)
}

function onClickCite(n: number) {
  const c = citationMap.value[n]
  if (!c) return
  popup.value = {
    n,
    text: `工具: ${c.tool}\n参数: ${c.args || '—'}\n时间: ${c.ts}\n摘录: ${c.excerpt || '—'}`
  }
}

function onSectionClick(e: MouseEvent) {
  const target = e.target as HTMLElement
  const n = target?.dataset?.n
  if (n) onClickCite(parseInt(n, 10))
}
</script>

<template>
  <div class="report card">
    <div class="head">
      <strong>{{ report.meta?.name || report.meta?.symbol }}（{{ report.meta?.symbol }}）投研报告</strong>
      <span class="num" :class="verdictMap[report.verdict]?.cls">
        {{ verdictMap[report.verdict]?.label || report.verdict }}
      </span>
      <span class="asof num">数据截至 {{ report.meta?.data_as_of }}</span>
    </div>

    <div v-for="(s, i) in report.sections" :key="i" class="section">
      <div v-if="s.title" class="sec-title">{{ s.title }}</div>
      <table v-if="s.type === 'table' && s.rows?.length" class="num">
        <tr v-for="(row, ri) in s.rows" :key="ri">
          <td v-for="(cell, ci) in row" :key="ci"
              :class="{ num: ci > 0, 'pct-up': String(cell).startsWith('+') && ci > 0, 'pct-down': String(cell).startsWith('-') && ci > 0 }">
            {{ cell }}
          </td>
        </tr>
      </table>
      <p v-else-if="s.text" class="text" v-html="renderHtml(s.text)" @click="onSectionClick" />
    </div>

    <details class="citations">
      <summary>引用出处（{{ report.citations?.length || 0 }} 条）</summary>
      <div v-for="c in report.citations" :key="c.n" class="cite-item num">
        [{{ c.n }}] {{ c.tool }} @{{ c.ts }} — {{ c.excerpt?.slice(0, 80) }}
      </div>
    </details>

    <div class="risk">⚠️ {{ report.risk_disclosure }}</div>

    <div v-if="popup" class="popup card" @click="popup = null">
      <div class="popup-title">引用 [{{ popup.n }}]（点击关闭）</div>
      <pre class="raw">{{ popup.text }}</pre>
    </div>
  </div>
</template>

<style scoped>
.report { padding: 14px 16px; border-left: 3px solid var(--primary); }
.head { display: flex; gap: 10px; align-items: baseline; flex-wrap: wrap; margin-bottom: 8px; }
.asof { margin-left: auto; font-size: 12px; color: var(--text-3); }
.section { margin: 10px 0; }
.sec-title { font-weight: 600; color: var(--text-1); margin-bottom: 4px; }
.text { line-height: 1.7; margin: 0; white-space: pre-wrap; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
td { border-bottom: 1px solid var(--border); padding: 5px 8px; text-align: right; }
td:first-child { text-align: left; }
.citations summary { cursor: pointer; color: var(--primary); font-size: 13px; }
.cite-item { font-size: 12px; color: var(--text-2); padding: 3px 0; }
.risk { margin-top: 10px; font-size: 12px; color: var(--warn); border-top: 1px dashed var(--border); padding-top: 8px; }
.popup { position: fixed; right: 16px; bottom: 100px; max-width: 420px; padding: 12px; z-index: 50; }
.popup-title { font-weight: 600; margin-bottom: 6px; font-size: 13px; }
.popup .raw { font-size: 12px; white-space: pre-wrap; color: var(--text-2); max-height: 240px; overflow: auto; }
</style>
