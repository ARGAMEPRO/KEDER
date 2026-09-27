import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
const t = 'http://localhost:8000'
export default defineConfig({
  plugins: [react()],
  server: { host: '0.0.0.0', port: 5173, proxy: { '/api': t, '/uploads': t, '/ws': { target: t, ws: true } } },
  build: {
    chunkSizeWarningLimit: 1200,
    rollupOptions: { output: { manualChunks: { maplibre: ['maplibre-gl'], motion: ['framer-motion'] } } },
  },
})
