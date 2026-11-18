import { fileURLToPath } from 'node:url';

import { defineConfig } from 'vitest/config';

/**
 * The apps' package directories (``@office/<app>/``): the tools.json and
 * profile.json of each are the contract the backend loads.
 */
const officeDir = fileURLToPath(new URL('../../jiuwenswarm/office/', import.meta.url));
/**
 * One page per host, each Office host with the manifest of the same name; the code they share is split into common
 * chunks. WPS loads the shell under ``public/wps/`` instead, which opens its page.
 */
const hosts = ['powerpoint', 'word', 'excel', 'wps-presentation'];

/** The Gateway's web channel, which the pane reaches at ``/ws`` of its own origin. */
const gatewayTarget = process.env.JIUWENSWARM_WEB_CHANNEL_URL ?? 'ws://127.0.0.1:19000';
/** The web frontend's server, whose ``/file-api/download`` serves the files a tool call inserts. */
const fileApiTarget = process.env.JIUWENSWARM_FILE_API_URL ?? 'http://127.0.0.1:5173';

export default defineConfig(async ({ command }) => ({
  resolve: { alias: { '@office': officeDir } },
  server: {
    port: 3001,
    strictPort: true,
    // Office loads a task pane only over HTTPS; WPS loads one over HTTP from the loopback address.
    https: command === 'serve' && process.env.VITEST !== 'true' && process.env.JIUWENSWARM_PANE_HTTP !== '1'
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
