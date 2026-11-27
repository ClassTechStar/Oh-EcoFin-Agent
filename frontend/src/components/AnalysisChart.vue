<script setup lang="ts">
/** 分析图表：后端返回的 ECharts option 直接渲染（按需注册，异步 chunk）。
 *  option 内不含 JS 函数，保证图表 spec 可序列化、可复现；
 *  PNG 导出走 canvas getDataURL，HTML 导出由调用方拼装自包含文档。 */
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'

const props = withDefaults(defineProps<{ option: any; height?: number }>(), {
  height: 380
})

const host = ref<HTMLElement | null>(null)
let inst: any = null
let ro: ResizeObserver | null = null

function colors() {
  const s = getComputedStyle(document.documentElement)
  return {
    text: s.getPropertyValue('--text-2').trim() || '#5f6b76',
    bg: s.getPropertyValue('--md-surface-container-lowest').trim() || '#ffffff'
  }
}

async function ensureChart() {
  if (inst || !host.value) return
  const echarts = await import('echarts/core')
  const { BarChart, LineChart, PieChart, HeatmapChart, FunnelChart } =
    await import('echarts/charts')
  const { TitleComponent, TooltipComponent, GridComponent, LegendComponent,
          VisualMapComponent } = await import('echarts/components')
  const { CanvasRenderer } = await import('echarts/renderers')
  echarts.use([BarChart, LineChart, PieChart, HeatmapChart, FunnelChart,
               TitleComponent, TooltipComponent, GridComponent, LegendComponent,
               VisualMapComponent, CanvasRenderer])
  inst = echarts.init(host.value)
  apply()
}

function apply() {
  if (!inst) return
  const c = colors()
  inst.setOption({ textStyle: { color: c.text, fontFamily: 'inherit' }, ...props.option }, true)
}

function getDataURL(): string {
  if (!inst) return ''
  return inst.getDataURL({ type: 'png', pixelRatio: 2, backgroundColor: colors().bg })
}

defineExpose({ getDataURL })

onMounted(async () => {
  await ensureChart()
  if (host.value) {
    ro = new ResizeObserver(() => inst?.resize())
    ro.observe(host.value)
  }
})

onBeforeUnmount(() => {
  ro?.disconnect()
  inst?.dispose()
  inst = null
})

watch(() => props.option, apply, { deep: true })
</script>

<template>
  <div ref="host" class="analysis-chart" :style="{ height: `${height}px` }" />
</template>

<style scoped>
.analysis-chart { width: 100%; min-height: 220px; }
</style>
