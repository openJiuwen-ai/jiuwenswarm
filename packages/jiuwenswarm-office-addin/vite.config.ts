import { fileURLToPath } from 'node:url';

import { defineConfig } from 'vitest/config';

/** The PowerPoint app's package directory: its tools.json and profile.json are the contract the backend loads. */
export const contractDir = fileURLToPath(
  new URL('../../jiuwenswarm/office/powerpoint/', import.meta.url),
);

/** The Gateway's web channel, which the pane reaches at ``/ws`` of its own origin. */
const gatewayTarget = process.env.JIUWENSWARM_WEB_CHANNEL_URL ?? 'ws://127.0.0.1:19000';

export default defineConfig(async ({ command }) => ({
  resolve: { alias: { '@contract': contractDir } },
  server: {
    port: 3001,
    strictPort: true,
    // Office loads a task pane only over HTTPS.
    https: command === 'serve' && process.env.VITEST !== 'true'
      ? await (await import('office-addin-dev-certs')).getHttpsServerOptions() : undefined,
    proxy: { '/ws': { target: gatewayTarget, ws: true } },
    fs: { allow: ['.', contractDir] },
  },
  build: { outDir: 'dist', rollupOptions: { input: 'taskpane.html' } },
  test: { include: ['src/**/*.test.ts'] },
}));
