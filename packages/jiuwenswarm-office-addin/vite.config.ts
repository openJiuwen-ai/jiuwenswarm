import { fileURLToPath } from 'node:url';

import { defineConfig } from 'vitest/config';

/**
 * The apps' package directories (``@office/<app>/``): the tools.json and
 * profile.json of each are the contract the backend loads.
 */
const officeDir = fileURLToPath(new URL('../../jiuwenswarm/office/', import.meta.url));
/** One page per host, each with the manifest of the same name; the code they share is split into common chunks. */
const hosts = ['powerpoint', 'word', 'excel'];

/** The Gateway's web channel, which the pane reaches at ``/ws`` of its own origin. */
const gatewayTarget = process.env.JIUWENSWARM_WEB_CHANNEL_URL ?? 'ws://127.0.0.1:19000';
/** The web frontend's server, whose ``/file-api/download`` serves the files a tool call inserts. */
const fileApiTarget = process.env.JIUWENSWARM_FILE_API_URL ?? 'http://127.0.0.1:5173';

export default defineConfig(async ({ command }) => ({
  resolve: { alias: { '@office': officeDir } },
  server: {
    port: 3001,
    strictPort: true,
    // Office loads a task pane only over HTTPS.
    https: command === 'serve' && process.env.VITEST !== 'true'
      ? await (await import('office-addin-dev-certs')).getHttpsServerOptions() : undefined,
    proxy: { '/ws': { target: gatewayTarget, ws: true }, '/file-api/download': { target: fileApiTarget } },
    fs: { allow: ['.', officeDir] },
  },
  build: {
    outDir: 'dist',
    rollupOptions: { input: Object.fromEntries(hosts.map(host => [host, `taskpane.${host}.html`])) },
  },
  test: { include: ['src/**/*.test.ts'] },
}));
