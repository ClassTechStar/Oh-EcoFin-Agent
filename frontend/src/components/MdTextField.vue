<script setup lang="ts">
/** MD3 文本输入框（对齐 mochi MochiTextFieldDefaults）：
 *  - 单行高度 48px，圆角 = 高度一半（胶囊）；textarea 多行变圆角矩形
 *  - 容器 = onSurface@8%（浅）/ @20%（深）fill；placeholder onSurfaceVariant@70%
 *  - 光标 primary；行首图标 inset 14px；水平内边距 16px
 *  - 错误：说明文字与光标 error，底色 error@12%；supporting 文字 onSurfaceVariant
 */
import { computed } from 'vue'
import MdIcon from './MdIcon.vue'

const props = withDefaults(defineProps<{
  modelValue: string
  label?: string
  placeholder?: string
  icon?: string
  error?: string
  supporting?: string
  disabled?: boolean
  type?: string
  rows?: number
  multiline?: boolean
  maxlength?: number
}>(), {
  placeholder: '', icon: '', error: '', supporting: '', label: '',
  disabled: false, type: 'text', rows: 3, multiline: false, maxlength: undefined,
})

const emit = defineEmits<{ (e: 'update:modelValue', v: string): void; (e: 'enter'): void }>()
const value = computed({
  get: () => props.modelValue,
  set: (v: string) => emit('update:modelValue', v),
})
</script>

<template>
  <div :class="['md-field', { 'md-field--error': !!error, 'md-field--disabled': disabled }]">
    <div v-if="multiline" class="md-field__box md-field__box--area">
      <textarea v-model="value" :placeholder="placeholder" :disabled="disabled"
                :rows="rows" :maxlength="maxlength" class="md-field__input md-field__area" />
      <button v-if="modelValue" class="md-field__clear" aria-label="清空"
              @click="emit('update:modelValue', '')">
        <MdIcon name="close" :size="16" />
      </button>
    </div>
    <div v-else class="md-field__box">
      <MdIcon v-if="icon" :name="icon" :size="20" class="md-field__leading" />
      <input v-model="value" :type="type" :placeholder="placeholder" :disabled="disabled"
             :maxlength="maxlength" class="md-field__input"
             @keydown.enter.prevent="emit('enter')" />
      <button v-if="modelValue" class="md-field__clear" aria-label="清空"
              @click="emit('update:modelValue', '')">
        <MdIcon name="close" :size="16" />
      </button>
    </div>
    <div v-if="error || supporting" class="md-field__supporting num" :class="{ 'md-field__err-text': !!error }">
      {{ error || supporting }}
    </div>
  </div>
</template>

<style scoped>
.md-field { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
.md-field--disabled { opacity: 0.55; }

.md-field__box {
  position: relative;
  display: flex; align-items: center;
  height: 48px;
  border-radius: var(--md-radius-full);
  background: var(--md-state-hover) solid;
  background-color: color-mix(in srgb, var(--md-on-surface) 8%, transparent);
  transition: background-color var(--motion-fade-in), box-shadow var(--motion-fade-in);
}
:root[data-theme="dark"] .md-field__box {
  background-color: color-mix(in srgb, var(--md-on-surface) 20%, transparent);
}
.md-field__box:focus-within { box-shadow: inset 0 0 0 2px var(--md-primary); }
.md-field--error .md-field__box {
  background-color: color-mix(in srgb, var(--md-error) 12%, transparent);
}
.md-field--error .md-field__box:focus-within { box-shadow: inset 0 0 0 2px var(--md-error); }

.md-field__leading { margin-left: 14px; color: var(--md-on-surface-variant); flex: 0 0 auto; }
.md-field__input {
  flex: 1; min-width: 0;
  border: 0; outline: none; background: transparent;
  padding: 0 16px; height: 100%;
  font-size: 14px; color: var(--md-on-surface);
  caret-color: var(--md-primary);
}
.md-field__input::placeholder { color: var(--md-on-surface-variant); opacity: 0.7; }
.md-field--disabled .md-field__input { color: var(--md-disabled-content); }

.md-field__box--area {
  height: auto; border-radius: var(--md-radius-m);
  padding: 6px 8px 6px 0; align-items: flex-end;
}
.md-field__area {
  height: auto; min-height: 60px; max-height: 200px;
  padding: 8px 8px 8px 16px; resize: none; line-height: 1.5;
}
.md-field__box--area .md-field__clear { margin-bottom: 6px; }

.md-field__clear {
  width: 28px; height: 28px; margin-right: 10px;
  display: grid; place-items: center; flex: 0 0 auto;
  border-radius: var(--md-radius-full); color: var(--md-on-surface-variant);
  transition: background-color var(--motion-fade-in);
}
.md-field__clear:hover { background: var(--bg-hover); color: var(--md-on-surface); }

.md-field__supporting { font-size: var(--type-label); color: var(--md-on-surface-variant); padding-inline: var(--space-4); }
.md-field__err-text { color: var(--md-error); }
</style>
