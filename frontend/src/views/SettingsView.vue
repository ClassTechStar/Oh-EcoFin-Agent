<script setup lang="ts">
/** 设置视图（CyreneCompass settings-layout 模式：setting-card 分组 + segmented 控件 + field 行）：
 *  - 外观：主题模式（浅色/深色/跟随系统 segmented）+ 蓝色系 accent 预设（三档全为蓝）
 *  - 服务健康：快照源/截至时间/缺密钥诚实降级提示
 *  - 合规：三层免责声明
 */
import { onMounted, ref, watch } from 'vue'
import { api } from '../sse'
import { ACCENT_PRESETS, theme, useTheme, type AccentPreset, type ThemeMode } from '../theme/theme'
import MdCard from '../components/MdCard.vue'
import MdSwitch from '../components/MdSwitch.vue'
import MdChip from '../components/MdChip.vue'
import MdIcon from '../components/MdIcon.vue'
import MdButton from '../components/MdButton.vue'
import { useToast } from '../toast'

const { mode, accent } = useTheme()
const health = ref<any>(null)
const { show } = useToast()
const autoRefresh = ref(true)

const MODE_OPTIONS: { value: ThemeMode; label: string; icon: string }[] = [
  { value: 'light', label: '浅色', icon: 'sun' },
  { value: 'dark', label: '深色', icon: 'moon' },
  { value: 'system', label: '跟随系统', icon: 'monitor' },
]

onMounted(async () => {
  try { health.value = await api.health() } catch { /* ignore */ }
})

watch(autoRefresh, (v) => { if (!v) show('定时刷新已关闭（仅演示开关）', 'info') })
</script>

<template>
  <div class="settings">
    <div class="settings__col">
      <!-- 外观 -->
      <MdCard variant="filled" class="setting-card">
        <h2>外观</h2>
        <div class="field">
          <span>主题模式</span>
          <div class="segmented" role="radiogroup" aria-label="主题模式">
            <button v-for="m in MODE_OPTIONS" :key="m.value"
                    :class="{ active: mode === m.value }" role="radio"
                    :aria-checked="mode === m.value"
                    @click="theme.setMode(m.value)">
              <MdIcon :name="m.icon" :size="15" /> {{ m.label }}
            </button>
          </div>
        </div>
        <div class="field">
          <span>主题色（蓝色系）</span>
          <div class="accent-row" role="radiogroup" aria-label="主题色预设">
            <button v-for="(preset, key) in ACCENT_PRESETS" :key="key"
                    :class="['accent-swatch', { active: accent === key }]"
                    role="radio" :aria-checked="accent === key" :aria-label="preset.name"
                    :style="{ background: preset.light }"
                    @click="theme.setAccent(key as AccentPreset)">
              <MdIcon v-if="accent === key" name="check" :size="16" />
              <span>{{ preset.name }}</span>
            </button>
          </div>
        </div>
        <div class="field">
          <span>跟随快照自动刷新徽章</span>
          <MdSwitch v-model="autoRefresh" label="自动刷新" />
        </div>
        <p class="hint-line">设置保存在本地浏览器；三端（桌面/移动/浏览器）同源生效。</p>
      </MdCard>

      <!-- 服务健康 -->
      <MdCard variant="filled" class="setting-card">
        <h2>服务健康</h2>
        <div class="field"><span>运行状态</span><strong>{{ health?.status || '—' }}</strong></div>
        <div class="field"><span>快照数据源</span><strong>{{ health?.snapshot?.source || '—' }}</strong></div>
        <div class="field"><span>快照截至</span><strong class="num">{{ health?.snapshot?.as_of || '—' }}</strong></div>
        <div class="field"><span>DEMO_MODE</span>
          <strong>{{ health?.demo_mode ? '开启（冻结卡带离线演示）' : '关闭' }}</strong>
        </div>
        <div v-if="health?.missing_keys?.length" class="field"><span>缺失密钥</span>
          <strong class="pct-up">{{ health.missing_keys.join(', ') }}</strong>
        </div>
        <p class="hint-line">
          密钥三级读取：环境变量 FINAGENT_* → Windows 凭据管理器（keyring 服务名 oh-ecofin-agent）→ 本地 .env。
          绝不把"无密钥"伪装成"模型故障"。
        </p>
      </MdCard>

      <!-- 合规 -->
      <MdCard variant="outlined" class="setting-card">
        <h2>合规声明</h2>
        <p class="compliance-text">
          本系统为教学演示用途，所有输出不构成任何投资建议，不提供任何形式的投资建议服务；
          数据可能存在延迟或误差，请以交易所官方信息为准。
        </p>
        <div class="chips-row">
          <MdChip icon="check">页面 footer 常驻</MdChip>
          <MdChip icon="check">报告 risk_disclosure 字段</MdChip>
          <MdChip icon="check">API 响应体附带</MdChip>
        </div>
        <div class="chips-row">
          <MdButton variant="outlined" icon="refresh" small @click="show('免责声明三层在位（教学演示）', 'info')">
            自检
          </MdButton>
        </div>
      </MdCard>
    </div>
  </div>
</template>

<style scoped>
.settings { flex: 1; overflow-y: auto; overscroll-behavior: none; }
.settings__col { max-width: 720px; margin: 0 auto; padding: var(--space-5) var(--space-4) var(--space-8); display: flex; flex-direction: column; gap: var(--space-4); }
.setting-card h2 { margin: 0 0 var(--space-4); font-size: var(--type-title); }
.field {
  display: flex; align-items: center; justify-content: space-between; gap: var(--space-4);
  padding: var(--space-2) 0;
  color: var(--text-secondary); font-size: 13px;
}
.hint-line { margin: var(--space-2) 0 0; color: var(--text-muted); font-size: 12px; line-height: 1.6; }

.segmented {
  display: inline-flex; gap: 4px; padding: 4px;
  border-radius: var(--md-radius-full);
  background: var(--md-surface-container);
}
.segmented button {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 7px 14px; border-radius: var(--md-radius-full);
  background: transparent; color: var(--text-secondary); font-size: 13px;
  transition: background-color var(--motion-fade-in), color var(--motion-fade-in);
}
.segmented button:hover { background: var(--bg-hover); color: var(--text-primary); }
.segmented button.active { background: var(--md-primary); color: var(--md-on-primary); }

.accent-row { display: flex; gap: var(--space-2); }
.accent-swatch {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 7px 12px; border-radius: var(--md-radius-full);
  color: #fff; font-size: 12px; font-weight: 600;
  border: 2px solid transparent;
  transition: transform var(--motion-press-down), border-color var(--motion-fade-in), box-shadow var(--motion-fade-in);
}
.accent-swatch:hover { transform: translateY(-2px); box-shadow: var(--md-elev-1); }
.accent-swatch.active { border-color: var(--md-outline); }

.compliance-text { margin: 0 0 var(--space-3); font-size: 13px; line-height: 1.7; color: var(--text-secondary); }
.chips-row { display: flex; flex-wrap: wrap; gap: var(--space-2); margin-top: var(--space-2); }

@media (max-width: 768px) {
  .settings__col { padding: var(--space-3) var(--space-3) var(--space-6); }
  .field { flex-wrap: wrap; }
}
</style>
