<script setup lang="ts">
/** 应用根：AppShell + 四视图（mode 切换 + Page 档位过渡，无路由依赖）。
 *  聊天业务逻辑全部在 ChatView（SSE 协议不变），本文件只负责壳与导航。 */
import { ref } from 'vue'
import AppShell from './layouts/AppShell.vue'
import ChatView from './views/ChatView.vue'
import DataView from './views/DataView.vue'
import DataHubView from './views/DataHubView.vue'
import ReportsView from './views/ReportsView.vue'
import AuditView from './views/AuditView.vue'
import TradingView from './views/TradingView.vue'
import AdvisorView from './views/AdvisorView.vue'
import CostView from './views/CostView.vue'
import DecisionsView from './views/DecisionsView.vue'
import SettingsView from './views/SettingsView.vue'
import MdSnackbar from './components/MdSnackbar.vue'

const active = ref('chat')

const VIEWS: Record<string, unknown> = {
  chat: ChatView,
  analysis: DataView,
  data: DataHubView,
  reports: ReportsView,
  audit: AuditView,
  trading: TradingView,
  advisor: AdvisorView,
  cost: CostView,
  decisions: DecisionsView,
  settings: SettingsView,
}
</script>

<template>
  <AppShell :active="active" @navigate="active = $event">
    <component :is="VIEWS[active]" />
  </AppShell>
  <MdSnackbar />
</template>

<style scoped>
/* 壳层样式在 AppShell 内管理 */
</style>
