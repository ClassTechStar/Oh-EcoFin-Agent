<script setup lang="ts">
/** MD3 Snackbar/Toast 宿主：inverseSurface 底 + inverseOnSurface 文字，
 *  底部居中，Present 档位动效。全局唯一实例挂载于 App.vue。 */
import { useToast } from '../toast'
import MdIcon from './MdIcon.vue'

const { state, dismiss } = useToast()

const ICONS = { info: 'info', success: 'check', error: 'warning' } as const
</script>

<template>
  <Teleport to="body">
    <Transition name="md-toast">
      <div v-if="state.open" :class="['md-toast', `md-toast--${state.kind}`]" role="status" @click="dismiss">
        <MdIcon :name="ICONS[state.kind]" :size="18" />
        <span class="md-toast__text">{{ state.message }}</span>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.md-toast {
  position: fixed; left: 50%; bottom: calc(24px + env(safe-area-inset-bottom, 0px));
  transform: translateX(-50%);
  display: inline-flex; align-items: center; gap: var(--space-2);
  max-width: min(480px, 90vw);
  padding: 10px 16px;
  border-radius: var(--md-radius-s);
  background: var(--md-inverse-surface); color: var(--md-inverse-on-surface);
  font-size: 13px;
  box-shadow: var(--md-elev-3);
  z-index: 200;
  cursor: pointer;
}
.md-toast--success svg { color: #9cf0f2; }
.md-toast--error svg { color: #ffb4ab; }
.md-toast__text { min-width: 0; overflow: hidden; text-overflow: ellipsis; }

.md-toast-enter-active { transition: opacity var(--motion-fade-in), transform var(--motion-present); }
.md-toast-leave-active { transition: opacity var(--motion-fade-out), transform var(--motion-settle); }
.md-toast-enter-from, .md-toast-leave-to { opacity: 0; transform: translateX(-50%) translateY(12px); }
</style>
