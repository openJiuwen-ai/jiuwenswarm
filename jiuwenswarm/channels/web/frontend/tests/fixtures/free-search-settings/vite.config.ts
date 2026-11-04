import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import svgr from 'vite-plugin-svgr';

export default defineConfig({
  root: fileURLToPath(new URL('../../../', import.meta.url)),
  plugins: [react(), svgr()],
  resolve: {
    alias: {
      'virtual:login-auth-simulate-provider': fileURLToPath(new URL('../../../src/auth/simulate/available.ts', import.meta.url)),
    },
  },
  optimizeDeps: { entries: ['tests/fixtures/free-search-settings/index.html'] },
  server: { host: '127.0.0.1', strictPort: true },
});
