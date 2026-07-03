import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// MASHUP_API lets a second dev stack proxy to a sandboxed backend
// (e.g. one started with MASHUP_DATA_DIR pointing at a scratch copy).
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': process.env.MASHUP_API ?? 'http://127.0.0.1:8000',
    },
  },
})
