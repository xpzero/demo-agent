import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import sassDts from 'vite-plugin-sass-dts'
import { fileURLToPath } from 'node:url'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss(), sassDts({ enabledMode: ["development", "production"] })],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  css: {
    modules: {
      // 保留原始连字符 key，同时生成驼峰别名，两种写法都能导入
      localsConvention: 'camelCase',
    },
  },
})
