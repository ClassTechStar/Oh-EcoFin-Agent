<script setup lang="ts">
/** 应用壳（布局移植自 CyreneCompass：titlebar + dock 侧栏 + 内容区）：
 *  - 桌面 ≥768px：左侧 NavigationRail（dock 模式，188px 带标签 / 64px 收窄）
 *  - 移动 <768px：底部 NavigationBar（MD3，64px）
 *  - 顶栏 56px：品牌 + 快照新鲜度徽章 + 主题切换（MD3 top app bar 交互）
 *  - 无路由依赖：mode 切换 + Page 档位过渡（CyreneCompass mode-surface 模式）
 */
import { computed, onMounted, ref } from 'vue'
import { api } from '../sse'
import { useTheme } from '../theme/theme'
import MdIcon from '../components/MdIcon.vue'
import MdIconButton from '../components/MdIconButton.vue'
import MdProgress from '../components/MdProgress.vue'

const props = defineProps<{ active: string }>()
const emit = defineEmits<{ (e: 'navigate', id: string): void }>()

const { mode, isDark, theme } = useTheme()
const health = ref<any>(null)
const railOpen = ref(false)

const NAV = [
  { id: 'chat', label: '投研对话', icon: 'chat' },
  { id: 'trading', label: '交易看板', icon: 'trading' },
  { id: 'advisor', label: '投顾合规', icon: 'shield' },
  { id: 'cost', label: '成本看板', icon: 'dashboard' },
  { id: 'decisions', label: '决策记忆', icon: 'history' },
  { id: 'settings', label: '设置', icon: 'settings' },
]

const activeLabel = computed(() => NAV.find(n => n.id === props.active)?.label ?? '')

const freshnessMeta = computed(() => {
  const f = health.value?.snapshot?.freshness
  if (f === 'green') return { icon: 'check', cls: 'fresh-green', text: '盘中' }
  if (f === 'yellow') return { icon: 'info', cls: 'fresh-yellow', text: '盘后' }
  return { icon: 'warning', cls: 'fresh-red', text: '离线' }
})

const demoMode = computed(() => !!health.value?.demo_mode)

function cycleTheme() {
  theme.setMode(isDark.value ? 'light' : 'dark')
}

onMounted(async () => {
  try { health.value = await api.health() } catch { /* 后端未启动 */ }
})
</script>

<template>
  <div class="shell">
    <!-- 顶栏（CyreneCompass titlebar 模式 → MD3 top app bar） -->
    <header class="topbar">
      <div class="brand">
        <div class="brand__logo"><MdIcon name="spark" :size="18" /></div>
        <strong>哦经金智能体</strong>
        <span class="brand__sub desktop-only">Oh EcoFin Agent · 教学演示</span>
      </div>

      <div class="topbar__badges">
        <span v-if="demoMode" class="badge-demo">DEMO_MODE</span>
        <span v-if="health" :class="['badge-fresh', freshnessMeta.cls]" :title="`快照截至 ${health.snapshot?.as_of || '—'}`">
          <MdIcon :name="freshnessMeta.icon" :size="13" />
          {{ freshnessMeta.text }}<span class="desktop-only num"> · {{ (health.snapshot?.as_of || '').slice(11, 16) }}</span>
        </span>
        <span v-else class="badge-fresh fresh-red"><MdIcon name="warning" :size="13" /> 未连接</span>
        <MdIconButton class="desktop-only" :icon="isDark ? 'sun' : 'moon'" label="切换主题" variant="standard"
                      :size="20" @click="cycleTheme" />
      </div>
    </header>

    <div class="shell__body">
      <!-- 桌面侧栏（dock 模式） -->
      <nav :class="['rail', { 'rail--expanded': railOpen }]" aria-label="主导航">
        <button v-for="item in NAV" :key="item.id"
                :class="['rail__item', { 'rail__item--active': active === item.id }]"
                :aria-current="active === item.id ? 'page' : undefined"
                @click="emit('navigate', item.id)">
          <span class="rail__indicator">
            <MdIcon :name="item.icon" :size="22" />
          </span>
          <span v-if="railOpen" class="rail__label">{{ item.label }}</span>
          <span v-else class="rail__fallback">{{ item.label }}</span>
        </button>
        <button class="rail__expand desktop-only" :aria-label="railOpen ? '收起导航' : '展开导航'"
                @click="railOpen = !railOpen">
          <MdIcon :name="railOpen ? 'close' : 'plus'" :size="18" />
        </button>
      </nav>

      <!-- 内容区（Page 档位过渡） -->
      <main class="content">
        <div class="content__header mobile-only">
          <h1 class="content__title">{{ activeLabel }}</h1>
          <MdIconButton :icon="isDark ? 'sun' : 'moon'" label="切换主题" :size="20" @click="cycleTheme" />
        </div>
        <Transition name="page" mode="out-in">
          <div :key="active" class="content__page">
            <slot />
          </div>
        </Transition>
      </main>
    </div>

    <!-- 移动端底部导航（MD3 NavigationBar） -->
    <nav class="navbar" aria-label="底部导航">
      <button v-for="item in NAV" :key="item.id"
              :class="['navbar__item', { 'navbar__item--active': active === item.id }]"
              :aria-current="active === item.id ? 'page' : undefined"
              @click="emit('navigate', item.id)">
        <span class="navbar__indicator"><MdIcon :name="item.icon" :size="22" /></span>
        <span class="navbar__label">{{ item.label }}</span>
      </button>
    </nav>

    <!-- 全局加载指示（快照刷新） -->
    <div v-if="health && health.snapshot?.is_stale" class="stale-strip">
      <MdProgress type="ring" :size="14" />
      <span>快照为历史缓存（诚实降级）</span>
    </div>
  </div>
</template>

<style scoped>
.shell { height: 100vh; display: flex; flex-direction: column; overflow: hidden; }

/* 顶栏 */
.topbar {
  height: 56px; flex: 0 0 auto;
  display: flex; align-items: center; justify-content: space-between;
  padding: 0 var(--space-4);
  background: var(--md-surface-container-lowest);
  border-bottom: 1px solid var(--border-subtle);
}
.brand { display: flex; align-items: center; gap: 9px; min-width: 0; }
.brand__logo {
  width: 30px; height: 30px; border-radius: var(--md-radius-s);
  display: grid; place-items: center;
  background: var(--md-primary); color: var(--md-on-primary);
  flex: 0 0 auto;
}
.brand strong { font-size: 15px; letter-spacing: 0.2px; white-space: nowrap; }
.brand__sub { color: var(--text-muted); font-size: var(--type-label); white-space: nowrap; }
.topbar__badges { display: flex; align-items: center; gap: var(--space-2); }
.badge-demo {
  background: var(--md-tertiary); color: var(--md-on-tertiary);
  border-radius: var(--md-radius-full); padding: 2px 8px; font-size: 11px; font-weight: 600;
}
.badge-fresh {
  display: inline-flex; align-items: center; gap: 4px;
  border-radius: var(--md-radius-full); padding: 3px 10px;
  font-size: 12px; font-weight: 500;
  background: var(--md-surface-container); color: var(--text-secondary);
}
.fresh-green { color: var(--down); }
.fresh-yellow { color: var(--warn); }
.fresh-red { color: var(--md-error); }

/* 主体 */
.shell__body { flex: 1; display: flex; min-height: 0; }

/* 桌面 NavigationRail（dock 模式） */
.rail {
  width: 64px; flex: 0 0 auto;
  display: flex; flex-direction: column; align-items: center; gap: var(--space-1);
  padding: var(--space-3) var(--space-2);
  background: var(--md-surface-container-lowest);
  border-right: 1px solid var(--border-subtle);
  transition: width var(--motion-settle);
  overflow: hidden;
}
.rail--expanded { width: 188px; align-items: stretch; }
.rail__item {
  display: flex; align-items: center; gap: var(--space-3);
  width: 100%; min-height: 44px;
  border-radius: var(--md-radius-m);
  color: var(--text-secondary);
  overflow: hidden;
  transition: background-color var(--motion-fade-in), color var(--motion-fade-in);
}
.rail__item:hover { background: var(--bg-hover); color: var(--text-primary); }
.rail__item--active { color: var(--md-on-secondary-container); }
.rail__item--active .rail__indicator { background: var(--md-secondary-container); }
.rail__indicator {
  width: 44px; height: 32px; margin-inline: 0 auto;
  display: grid; place-items: center; flex: 0 0 auto;
  border-radius: var(--md-radius-full);
  transition: background-color var(--motion-fade-in), width var(--motion-settle);
}
.rail__label { font-size: 13px; font-weight: 600; white-space: nowrap; }
.rail__fallback { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); }
.rail__item { position: relative; }
.rail__expand {
  margin-top: auto; width: 44px; height: 44px;
  display: grid; place-items: center;
  border-radius: var(--md-radius-full); color: var(--text-muted);
  transition: background-color var(--motion-fade-in), color var(--motion-fade-in);
}
.rail__expand:hover { background: var(--bg-hover); color: var(--text-primary); }
.rail--expanded .rail__expand { width: 100%; justify-content: flex-end; display: flex; }

/* 内容区 */
.content { flex: 1; min-width: 0; display: flex; flex-direction: column; }
.content__header {
  display: flex; align-items: center; justify-content: space-between;
  padding: var(--space-3) var(--space-4) 0;
}
.content__title { margin: 0; font-size: 18px; }
.content__page { flex: 1; min-height: 0; display: flex; flex-direction: column; overflow: hidden; }

/* 移动端 NavigationBar */
.navbar {
  display: none;
  height: 64px; flex: 0 0 auto;
  background: var(--md-surface-container);
  border-top: 1px solid var(--border-subtle);
}
.navbar__item {
  flex: 1; display: flex; flex-direction: column; align-items: center;
  gap: 2px; padding-top: 8px;
  color: var(--text-secondary);
  font-size: 11px; font-weight: 500;
}
.navbar__indicator {
  width: 56px; height: 30px;
  display: grid; place-items: center;
  border-radius: var(--md-radius-full);
  transition: background-color var(--motion-fade-in);
}
.navbar__item--active { color: var(--md-on-secondary-container); }
.navbar__item--active .navbar__indicator { background: var(--md-secondary-container); }

/* 降级提示条 */
.stale-strip {
  display: flex; align-items: center; gap: var(--space-2);
  padding: 4px var(--space-4);
  font-size: 11px; color: var(--warn);
  background: color-mix(in srgb, var(--warn) 8%, var(--md-surface-container-lowest));
}

@media (max-width: 768px) {
  .rail { display: none; }
  .navbar { display: flex; }
  .topbar { padding: 0 var(--space-3); }
}
@media (min-width: 769px) {
  .mobile-only { display: none !important; }
}
</style>
