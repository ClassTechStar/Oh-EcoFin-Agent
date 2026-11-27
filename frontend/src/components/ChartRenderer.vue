<script setup lang="ts">
/** 图表渲染器：按 kind 路由。
 *  kline → lightweight-charts（异步 chunk ~45KB）
 *  eff_frontier / heatmap / radar / decay → ECharts（按需注册，异步 chunk）
 *  图表懒渲染：IntersectionObserver 视口外不初始化。 */
import { onMounted, onBeforeUnmount, ref } from 'vue'

const props = defineProps<{ chart: { id: string; kind: string; title: string; data: any } }>()

const el = ref<HTMLElement | null>(null)
const visible = ref(false)
let observer: IntersectionObserver | null = null
let dispose: (() => void) | null = null

// 红涨绿跌：K 线/热力图全部吃 Token
function tokenColors() {
  const style = getComputedStyle(document.documentElement)
  return {
    up: style.getPropertyValue('--up').trim() || '#e03e3e',
    down: style.getPropertyValue('--down').trim() || '#0f8b6d',
    text: style.getPropertyValue('--text-2').trim() || '#5f6b76',
    border: style.getPropertyValue('--border').trim() || '#e3e6ea'
  }
}

onMounted(() => {
  observer = new IntersectionObserver(entries => {
    if (entries[0].isIntersecting) { visible.value = true; render(); observer?.disconnect() }
  }, { rootMargin: '200px' })
  if (el.value) observer.observe(el.value)
})

onBeforeUnmount(() => { observer?.disconnect(); dispose?.() })

async function render() {
  const c = tokenColors()
  try {
    if (props.chart.kind === 'kline') {
      const lc = await import('lightweight-charts')
      const host = el.value!.querySelector('.chart-host') as HTMLElement
      host.style.height = '260px'
      const chart = lc.createChart(host, {
        layout: { background: { color: 'transparent' }, textColor: c.text },
        grid: { vertLines: { color: c.border }, horzLines: { color: c.border } },
        timeScale: { timeVisible: false }
      })
      const series = chart.addCandlestickSeries({
        upColor: c.up, downColor: c.down, borderVisible: false,
        wickUpColor: c.up, wickDownColor: c.down
      })
      const bars = (props.chart.data?.bars || []).map((b: any) => ({
        time: b.date, open: b.open, high: b.high, low: b.low, close: b.close
      }))
      series.setData(bars)
      chart.timeScale().fitContent()
      const t0 = performance.now()
      dispose = () => chart.remove()
      void t0 // 渲染耗时可用 performance.mark 埋点（红线 <100ms）
    } else {
      const echarts = await import('echarts/core')
      const { LineChart, ScatterChart, HeatmapChart, RadarChart } = await import('echarts/charts')
      const { GridComponent, TooltipComponent, VisualMapComponent, LegendComponent } =
        await import('echarts/components')
      const { CanvasRenderer } = await import('echarts/renderers')
      echarts.use([LineChart, ScatterChart, HeatmapChart, RadarChart,
                   GridComponent, TooltipComponent, VisualMapComponent, LegendComponent, CanvasRenderer])
      const host = el.value!.querySelector('.chart-host') as HTMLElement
      host.style.height = '280px'
      const inst = echarts.init(host)
      inst.setOption(optionFor(props.chart.kind, props.chart.data, c))
      dispose = () => inst.dispose()
    }
  } catch (e) {
    console.error('chart render failed', e)
  }
}

function optionFor(kind: string, data: any, c: any): any {
  if (kind === 'eff_frontier') {
    const frontier = (data?.frontier || []).map((p: any) => [p.vol, p.ret])
    const point = data?.point
    return {
      grid: { left: 48, bottom: 32, right: 16, top: 16 },
      tooltip: { trigger: 'axis' },
      xAxis: { type: 'value', name: '年化波动', axisLabel: { formatter: (v: number) => (v * 100).toFixed(0) + '%' } },
      yAxis: { type: 'value', name: '年化收益', axisLabel: { formatter: (v: number) => (v * 100).toFixed(0) + '%' } },
      series: [
        { type: 'line', data: frontier, smooth: true, showSymbol: false,
          lineStyle: { color: c.text }, name: '有效前沿' },
        ...(point ? [{ type: 'scatter', symbolSize: 14,
          data: [[point.vol, point.ret]],
          itemStyle: { color: c.up }, name: `当前配置(${point.level})` }] : [])
      ],
      legend: {}
    }
  }
  if (kind === 'heatmap') {
    const symbols: string[] = data?.symbols || []
    const values: number[][] = data?.values || []
    const pairs: any[] = []
    values.forEach((row, i) => row.forEach((v, j) => pairs.push([j, i, v])))
    return {
      grid: { left: 80, bottom: 60, right: 16, top: 8 },
      tooltip: { formatter: (p: any) => `${symbols[p.value[1]]} ↔ ${symbols[p.value[0]]}: ${p.value[2]}` },
      xAxis: { type: 'category', data: symbols, axisLabel: { rotate: 45 } },
      yAxis: { type: 'category', data: symbols },
      visualMap: { min: -1, max: 1, calculable: true, orient: 'horizontal',
        inRange: { color: [c.down, '#ffffff', c.up] } },
      series: [{ type: 'heatmap', data: pairs,
        label: { show: true, formatter: (p: any) => p.value[2].toFixed(2) } }]
    }
  }
  if (kind === 'decay') {
    const folds = data?.folds || []
    return {
      grid: { left: 48, bottom: 32, right: 16, top: 32 },
      tooltip: { trigger: 'axis' },
      legend: {},
      xAxis: { type: 'category', data: folds.map((f: any) => `第${f.fold_id + 1}折`) },
      yAxis: { type: 'value', name: '夏普' },
      series: [
        { type: 'bar', name: '样本内夏普', data: folds.map((f: any) => f.is_sharpe),
          itemStyle: { color: '#8ca6c0' } },
        { type: 'bar', name: '样本外夏普', data: folds.map((f: any) => f.oos_sharpe),
          itemStyle: { color: c.up } }
      ]
    }
  }
  if (kind === 'radar') {
    const d = data || {}
    const supported = (d.supported || []).length
    const unsupported = (d.unsupported || []).length
    return {
      radar: { indicator: [
        { name: '有数据支撑论据', max: Math.max(supported + unsupported, 6) },
        { name: '无支撑论据', max: Math.max(supported + unsupported, 6) }] },
      series: [{ type: 'radar', data: [{ value: [supported, unsupported],
        name: '辩论支撑度', areaStyle: { opacity: 0.3 } }] }]
    }
  }
  return {}
}
</script>

<template>
  <div ref="el" class="chart card">
    <div class="title">{{ chart.title }}</div>
    <div class="chart-host" />
  </div>
</template>

<style scoped>
.chart { padding: 10px 12px; margin: 4px 0; }
.title { font-size: 13px; color: var(--text-2); margin-bottom: 6px; }
.chart-host { width: 100%; min-height: 120px; }
</style>
