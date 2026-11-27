/** 轻量 Toast/Snackbar 状态（MD3：inverseSurface 底 + inverseOnSurface 文字，
 *  底部居中（CyreneCompass toast 位置），Present 档位出现，3s 自动消失） */
import { reactive } from 'vue'

interface ToastState {
  open: boolean
  message: string
  kind: 'info' | 'success' | 'error'
}

const state = reactive<ToastState>({ open: false, message: '', kind: 'info' })
let timer: ReturnType<typeof setTimeout> | null = null

export function useToast() {
  function show(message: string, kind: ToastState['kind'] = 'info', duration = 3000) {
    state.message = message
    state.kind = kind
    state.open = true
    if (timer) clearTimeout(timer)
    timer = setTimeout(() => { state.open = false }, duration)
  }
  function dismiss() {
    state.open = false
    if (timer) clearTimeout(timer)
  }
  return { state, show, dismiss }
}
