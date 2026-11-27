<script setup lang="ts">
/** MD3 对话框（对齐 mochi MochiDialogDefaults + CyreneCompass modal-layer）：
 *  - 最大宽度 320（mochi 默认）/ wide 560；按钮区高度 48 的胶囊按钮
 *  - scrim 遮罩 + backdrop blur（CyreneCompass modal-layer 交互）
 *  - Pop 档位动效（360ms 回弹，从 0.92 缩放弹到位）
 *  - Esc 关闭 / 点遮罩关闭（closable 时）
 */
import { onMounted, onUnmounted } from 'vue'

const props = withDefaults(defineProps<{
  open: boolean
  title?: string
  wide?: boolean
  closable?: boolean
}>(), { title: '', wide: false, closable: true })

const emit = defineEmits<{ (e: 'close'): void }>()

function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape' && props.open && props.closable) emit('close')
}
onMounted(() => window.addEventListener('keydown', onKeydown))
onUnmounted(() => window.removeEventListener('keydown', onKeydown))
</script>

<template>
  <Teleport to="body">
    <Transition name="md-dialog">
      <div v-if="open" class="md-dialog-layer" @click.self="closable && emit('close')">
        <div :class="['md-dialog', { 'md-dialog--wide': wide }]" role="dialog" aria-modal="true"
             :aria-label="title || '对话框'">
          <div v-if="title || closable" class="md-dialog__head">
            <h2 class="md-dialog__title">{{ title }}</h2>
            <button v-if="closable" class="md-dialog__close" aria-label="关闭" @click="emit('close')">
              <svg width="20" height="20" viewBox="0 0 24 24"><path d="M6 6l12 12M18 6 6 18" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/></svg>
            </button>
          </div>
          <div class="md-dialog__body">
            <slot />
          </div>
          <div v-if="$slots.actions" class="md-dialog__actions">
            <slot name="actions" />
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.md-dialog-layer {
  position: fixed; inset: 0; z-index: 100;
  display: grid; place-items: center; padding: var(--space-4);
  background: var(--md-scrim);
  backdrop-filter: blur(6px);
  overscroll-behavior: none;
}
.md-dialog {
  width: min(320px, 92vw);
  max-height: min(620px, 88vh);
  display: flex; flex-direction: column; gap: var(--space-4);
  padding: var(--space-5);
  border-radius: var(--md-radius-xl);
  background: var(--md-surface-container-high);
  box-shadow: var(--md-elev-3);
}
.md-dialog--wide { width: min(560px, 92vw); }
.md-dialog__head { display: flex; align-items: center; justify-content: space-between; gap: var(--space-3); }
.md-dialog__title { margin: 0; font-size: 18px; line-height: 24px; }
.md-dialog__close {
  width: 32px; height: 32px; display: grid; place-items: center;
  border-radius: var(--md-radius-full); color: var(--text-secondary);
  transition: background-color var(--motion-fade-in);
}
.md-dialog__close:hover { background: var(--bg-hover); color: var(--text-primary); }
.md-dialog__body { overflow: auto; overscroll-behavior: none; font-size: var(--type-body); color: var(--text-secondary); }
.md-dialog__actions { display: flex; justify-content: flex-end; gap: var(--space-2); }

.md-dialog-enter-active { transition: opacity var(--motion-fade-in); }
.md-dialog-leave-active { transition: opacity var(--motion-fade-out); }
.md-dialog-enter-active .md-dialog { transition: transform var(--motion-pop); }
.md-dialog-enter-from { opacity: 0; }
.md-dialog-leave-to { opacity: 0; }
.md-dialog-enter-from .md-dialog { transform: scale(0.92); }
</style>
