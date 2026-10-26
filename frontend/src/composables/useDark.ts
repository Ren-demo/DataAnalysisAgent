import { ref } from 'vue'

export type ThemePreference = 'system' | 'light' | 'dark'

const STORAGE_KEY = 'app-theme'

function readPreference(): ThemePreference {
  const v = localStorage.getItem(STORAGE_KEY)
  return v === 'light' || v === 'dark' ? v : 'system'
}

// 模块级单例：所有组件（含 ChartCard）拿到同一份响应式状态
const preference = ref<ThemePreference>(readPreference())
const isDark = ref(false)

function apply() {
  isDark.value =
    preference.value === 'dark' ||
    (preference.value === 'system' &&
      window.matchMedia('(prefers-color-scheme: dark)').matches)
  document.documentElement.classList.toggle('dark', isDark.value)
}

// 跟随系统时，监听系统主题变化。
//
// 这是**刻意**的模块级单例：主题属于整个应用，不随任何组件的挂载/卸载变化，
// 所以没有「组件卸载时该 dispose」的东西（`useDark()` 本身不注册任何监听）。
// 唯一真实的重复注册来源是 Vite HMR —— 改本文件会重新执行模块，多挂一个监听，
// 故用 import.meta.hot?.dispose 显式摘掉旧的（生产构建下这段会被摇掉）。
const darkQuery = window.matchMedia('(prefers-color-scheme: dark)')
function onSystemThemeChange() {
  if (preference.value === 'system') apply()
}
darkQuery.addEventListener('change', onSystemThemeChange)

import.meta.hot?.dispose(() => {
  darkQuery.removeEventListener('change', onSystemThemeChange)
})

apply() // 初始化，与 index.html 内联脚本结果一致（双保险）

export function useDark() {
  function setPreference(p: ThemePreference) {
    preference.value = p
    localStorage.setItem(STORAGE_KEY, p)
    apply()
  }

  return { isDark, preference, setPreference }
}
