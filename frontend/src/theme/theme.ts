/** 主题运行时（架构移植自 CyreneCompass theme.js：单一种子色 → 派生变量 + 亮/暗/中性三模式）。
 *  主题种子为蓝色系（替换原粉红 #ea5ec1 体系）；MD3 角色在 tokens.css 中承载完整 token 集。
 *  - 模式：light / dark / system（跟随系统）
 *  - accent：仅蓝色系预设（无粉色残留）
 *  - 持久化：localStorage（键名带命名空间）
 */

export type ThemeMode = 'light' | 'dark' | 'system'
export type AccentPreset = 'deep-sea' | 'sky' | 'azure'

const STORAGE_KEY = 'oh-ecofin.theme.v1'

/** 蓝色系 accent 预设（三个都是蓝，杜绝粉红回潮） */
export const ACCENT_PRESETS: Record<AccentPreset, { name: string; light: string; dark: string }> = {
  'deep-sea': { name: '深海蓝', light: '#3557c7', dark: '#b0c4ff' },
  'sky':      { name: '晴空蓝', light: '#2264c5', dark: '#a8c8ff' },
  'azure':    { name: '青碧蓝', light: '#1f6bb0', dark: '#9cccff' },
}

const SYSTEM_QUERY = '(prefers-color-scheme: dark)'

interface PersistedTheme {
  mode: ThemeMode
  accent: AccentPreset
}

function load(): PersistedTheme {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (raw) {
      const data = JSON.parse(raw)
      return {
        mode: (['light', 'dark', 'system'] as ThemeMode[]).includes(data.mode) ? data.mode : 'system',
        accent: (Object.keys(ACCENT_PRESETS) as AccentPreset[]).includes(data.accent) ? data.accent : 'deep-sea',
      }
    }
  } catch { /* 忽略损坏数据 */ }
  return { mode: 'system', accent: 'deep-sea' }
}

function resolveSystem(): boolean {
  return typeof window !== 'undefined' && window.matchMedia(SYSTEM_QUERY).matches
}

/** 亮/暗两态的 accent hex（全为蓝色系） */
function accentHex(accent: AccentPreset, dark: boolean): string {
  return ACCENT_PRESETS[accent][dark ? 'dark' : 'light']
}

/** accent → 对应角色的 rgba 覆写（CyreneCompass 的 rgba(accent, a) 模式） */
function hexToRgb(hex: string): [number, number, number] {
  const h = hex.replace('#', '')
  return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)]
}

function apply(accent: AccentPreset, dark: boolean): void {
  const root = document.documentElement
  root.setAttribute('data-theme', dark ? 'dark' : 'light')
  const [r, g, b] = hexToRgb(accentHex(accent, dark))
  const rs = root.style
  // 蓝色 accent 派生变量（覆盖 tokens.css 的默认深海蓝；三个预设全为蓝）
  rs.setProperty('--md-primary', accentHex(accent, dark))
  rs.setProperty('--accent', accentHex(accent, dark))
  rs.setProperty('--accent-50', `rgba(${r}, ${g}, ${b}, ${dark ? 0.14 : 0.10})`)
  rs.setProperty('--accent-200', `rgba(${r}, ${g}, ${b}, ${dark ? 0.26 : 0.22})`)
  rs.setProperty('--md-surface-tint', accentHex(accent, dark))
}

const state = {
  mode: load().mode as ThemeMode,
  accent: load().accent as AccentPreset,
  listeners: new Set<() => void>(),
}

function persist(): void {
  localStorage.setItem(STORAGE_KEY, JSON.stringify({ mode: state.mode, accent: state.accent }))
}

function render(): void {
  const dark = state.mode === 'dark' || (state.mode === 'system' && resolveSystem())
  apply(state.accent, dark)
  state.listeners.forEach(fn => fn())
}

if (typeof window !== 'undefined') {
  window.matchMedia(SYSTEM_QUERY).addEventListener('change', () => {
    if (state.mode === 'system') render()
  })
  render()
}

export const theme = {
  get mode(): ThemeMode { return state.mode },
  get accent(): AccentPreset { return state.accent },
  get isDark(): boolean {
    return document.documentElement.getAttribute('data-theme') === 'dark'
  },
  setMode(mode: ThemeMode): void {
    state.mode = mode
    persist()
    render()
  },
  setAccent(accent: AccentPreset): void {
    state.accent = accent
    persist()
    render()
  },
  subscribe(fn: () => void): () => void {
    state.listeners.add(fn)
    return () => state.listeners.delete(fn)
  },
}

/** 响应式绑定（组件内使用） */
import { ref, onUnmounted } from 'vue'

export function useTheme() {
  const mode = ref<ThemeMode>(state.mode)
  const accent = ref<AccentPreset>(state.accent)
  const isDark = ref(theme.isDark)
  const unsub = theme.subscribe(() => {
    mode.value = state.mode
    accent.value = state.accent
    isDark.value = theme.isDark
  })
  onUnmounted(unsub)
  return { mode, accent, isDark, theme }
}
