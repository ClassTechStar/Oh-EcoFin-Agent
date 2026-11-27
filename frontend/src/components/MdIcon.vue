<script setup lang="ts">
/** 内联 SVG 图标集（Material Symbols 风格，24px 描边/填充）。
 *  离线安全：不依赖 Iconify 运行时 API（CyreneCompass 用 Iconify，此处为保证 DEMO_MODE
 *  断网可用改为内置 path；视觉规格与其一致：currentColor + 24 视窗）。 */
const props = defineProps<{ name: string; size?: number | string }>()
</script>

<template>
  <svg :width="size || 24" :height="size || 24" viewBox="0 0 24 24" fill="none"
       xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
    <!-- chat -->
    <template v-if="name === 'chat'">
      <path d="M4 6a3 3 0 0 1 3-3h10a3 3 0 0 1 3 3v8a3 3 0 0 1-3 3H9.4l-3.7 3.1c-.7.6-1.7.1-1.7-.8V6Z"
            stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" />
      <path d="M8 8h8M8 12h5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" />
    </template>
    <!-- dashboard -->
    <template v-else-if="name === 'dashboard'">
      <rect x="3.5" y="3.5" width="7.5" height="10" rx="2" stroke="currentColor" stroke-width="1.8" />
      <rect x="13" y="3.5" width="7.5" height="6" rx="2" stroke="currentColor" stroke-width="1.8" />
      <rect x="3.5" y="16" width="7.5" height="4.5" rx="2" stroke="currentColor" stroke-width="1.8" />
      <rect x="13" y="12" width="7.5" height="8.5" rx="2" stroke="currentColor" stroke-width="1.8" />
    </template>
    <!-- analysis（柱状图） -->
    <template v-else-if="name === 'analysis'">
      <path d="M4 20V11M9.3 20V6M14.7 20v-6M20 20V3.5" stroke="currentColor"
            stroke-width="2.4" stroke-linecap="round" />
      <path d="M2.5 20h19" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" />
    </template>
    <!-- report（文档） -->
    <template v-else-if="name === 'report'">
      <path d="M6 3.5h8l4.5 4.5V20a1.5 1.5 0 0 1-1.5 1.5H6A1.5 1.5 0 0 1 4.5 20V5A1.5 1.5 0 0 1 6 3.5Z"
            stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" />
      <path d="M14 3.8V8.2h4.3M8 12h7M8 15.5h5" stroke="currentColor"
            stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" />
    </template>
    <!-- data（数据库） -->
    <template v-else-if="name === 'data'">
      <ellipse cx="12" cy="5.5" rx="7.5" ry="3" stroke="currentColor" stroke-width="1.8" />
      <path d="M4.5 5.5v6c0 1.7 3.4 3 7.5 3s7.5-1.3 7.5-3v-6"
            stroke="currentColor" stroke-width="1.8" />
      <path d="M4.5 11.5v6c0 1.7 3.4 3 7.5 3s7.5-1.3 7.5-3v-6"
            stroke="currentColor" stroke-width="1.8" />
    </template>
    <!-- audit（审计清单） -->
    <template v-else-if="name === 'audit'">
      <path d="M7 3.5h10A1.5 1.5 0 0 1 18.5 5v15.5H5.5V5A1.5 1.5 0 0 1 7 3.5Z"
            stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" />
      <path d="M9 3.5V2.8h6v.7M8.5 9h7M8.5 12.3h4.5" stroke="currentColor"
            stroke-width="1.7" stroke-linecap="round" />
      <path d="m12.2 16.2 1.5 1.5 3-3.2" stroke="currentColor"
            stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" />
    </template>
    <!-- trading（K 线烛台 + 趋势） -->
    <template v-else-if="name === 'trading'">
      <path d="M7 3.5v3M7 15v5.5M17 3.5v5M17 17v3.5"
            stroke="currentColor" stroke-width="1.7" stroke-linecap="round" />
      <rect x="4.8" y="6.5" width="4.4" height="8.5" rx="1.3" stroke="currentColor" stroke-width="1.7" />
      <rect x="14.8" y="8.5" width="4.4" height="8.5" rx="1.3" stroke="currentColor" stroke-width="1.7" />
    </template>
    <!-- shield（合规） -->
    <template v-else-if="name === 'shield'">
      <path d="M12 3 5 6v5c0 4.6 2.9 8 7 10 4.1-2 7-5.4 7-10V6l-7-3Z"
            stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" />
      <path d="m8.8 11.8 2.2 2.2 4.2-4.4" stroke="currentColor" stroke-width="1.9"
            stroke-linecap="round" stroke-linejoin="round" />
    </template>
    <!-- history -->
    <template v-else-if="name === 'history'">
      <circle cx="12" cy="12" r="8.5" stroke="currentColor" stroke-width="1.8" />
      <path d="M12 7.5V12l3.2 2.4" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" />
    </template>
    <!-- settings -->
    <template v-else-if="name === 'settings'">
      <circle cx="12" cy="12" r="3.2" stroke="currentColor" stroke-width="1.8" />
      <path d="M12 2.8v2.4M12 18.8v2.4M21.2 12h-2.4M5.2 12H2.8M18.5 5.5l-1.7 1.7M7.2 16.8l-1.7 1.7M18.5 18.5l-1.7-1.7M7.2 7.2 5.5 5.5"
            stroke="currentColor" stroke-width="1.8" stroke-linecap="round" />
    </template>
    <!-- send -->
    <template v-else-if="name === 'send'">
      <path d="M4.3 11.3 19 5.2c.8-.3 1.6.5 1.3 1.3l-6.1 14.7c-.3.8-1.5.8-1.8-.1l-2-5.4-5.4-2c-.9-.3-.9-1.5 0-1.8Z"
            fill="currentColor" />
    </template>
    <!-- sun / moon / monitor -->
    <template v-else-if="name === 'sun'">
      <circle cx="12" cy="12" r="4.2" stroke="currentColor" stroke-width="1.8" />
      <path d="M12 3v2M12 19v2M21 12h-2M5 12H3M18.4 5.6 17 7M7 17l-1.4 1.4M18.4 18.4 17 17M7 7 5.6 5.6"
            stroke="currentColor" stroke-width="1.8" stroke-linecap="round" />
    </template>
    <template v-else-if="name === 'moon'">
      <path d="M20 14.5A8.5 8.5 0 0 1 9.5 4a8.5 8.5 0 1 0 10.5 10.5Z" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" />
    </template>
    <template v-else-if="name === 'monitor'">
      <rect x="3" y="4.5" width="18" height="12" rx="2" stroke="currentColor" stroke-width="1.8" />
      <path d="M9 20h6M12 16.5V20" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" />
    </template>
    <!-- status / feedback -->
    <template v-else-if="name === 'check'">
      <path d="M4.5 12.5 10 18 19.5 6.5" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" />
    </template>
    <template v-else-if="name === 'close'">
      <path d="M6 6l12 12M18 6 6 18" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" />
    </template>
    <template v-else-if="name === 'warning'">
      <path d="M12 3.6 21.5 20H2.5L12 3.6Z" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" />
      <path d="M12 9.5v4.2" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" />
      <circle cx="12" cy="16.6" r="1.15" fill="currentColor" />
    </template>
    <template v-else-if="name === 'info'">
      <circle cx="12" cy="12" r="8.5" stroke="currentColor" stroke-width="1.8" />
      <path d="M12 11v5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" />
      <circle cx="12" cy="8" r="1.15" fill="currentColor" />
    </template>
    <!-- refresh -->
    <template v-else-if="name === 'refresh'">
      <path d="M19.5 12a7.5 7.5 0 1 1-2.2-5.3" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" />
      <path d="M19.8 3.6v3.9h-3.9" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" />
    </template>
    <!-- spark (brand) -->
    <template v-else-if="name === 'spark'">
      <path d="M12 2.5l2.2 6.3 6.3 2.2-6.3 2.2L12 19.5l-2.2-6.3-6.3-2.2 6.3-2.2L12 2.5Z" fill="currentColor" />
    </template>
    <!-- up/down arrows (trend) -->
    <template v-else-if="name === 'trend-up'">
      <path d="M4 17.5 10 11l3.5 3.5L20 8" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" />
      <path d="M20 12.5V8h-4.5" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" />
    </template>
    <template v-else-if="name === 'trend-down'">
      <path d="M4 6.5 10 13l3.5-3.5L20 16" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" />
      <path d="M20 11.5V16h-4.5" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" />
    </template>
    <!-- menu / more -->
    <template v-else-if="name === 'plus'">
      <path d="M12 5v14M5 12h14" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" />
    </template>
    <template v-else-if="name === 'trash'">
      <path d="M5 7h14M10 4.5h4M6.5 7l.8 12a1.6 1.6 0 0 0 1.6 1.5h6.2a1.6 1.6 0 0 0 1.6-1.5L17.5 7"
            stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" />
    </template>
    <template v-else-if="name === 'mic'">
      <rect x="9" y="3" width="6" height="11" rx="3" stroke="currentColor" stroke-width="1.8" />
      <path d="M5.5 11.5a6.5 6.5 0 0 0 13 0M12 18v3M9 21h6"
            stroke="currentColor" stroke-width="1.8" stroke-linecap="round" />
    </template>
    <template v-else>
      <circle cx="12" cy="12" r="7" stroke="currentColor" stroke-width="1.8" />
    </template>
  </svg>
</template>
