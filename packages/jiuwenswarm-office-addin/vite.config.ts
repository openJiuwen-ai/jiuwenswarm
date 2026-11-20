import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import { defineConfig } from 'vitest/config';

/**
 * The apps' package directories (``@office/<app>/``): the tools.json and
 * profile.json of each are the contract the backend loads.
 */
const officeDir = fileURLToPath(new URL('../../jiuwenswarm/office/', import.meta.url));
/** The Office hosts, each with the manifest of the same name. */
const officeHosts = ['powerpoint', 'word', 'excel'];
/**
 * One page per host; the code they share is split into common chunks. WPS loads the shell under ``public/wps/``
 * instead of a manifest, which opens its page.
 */
const hosts = [...officeHosts, 'wps-presentation', 'wps-spreadsheet', 'wps-writer'];

/** Where `build:desktop` puts the pane: the web frontend's build, which the desktop serves at `/office/`. */
const desktopOutDir = fileURLToPath(new URL('../../jiuwenswarm/channels/web/frontend/dist/office/', import.meta.url));

/** The Gateway's web channel, which the pane reaches at ``/ws`` of its own origin. */
const gatewayTarget = process.env.JIUWENSWARM_WEB_CHANNEL_URL ?? 'ws://127.0.0.1:19000';
/** The web frontend's server, whose ``/file-api/download`` serves the files a tool call inserts. */
const fileApiTarget = process.env.JIUWENSWARM_FILE_API_URL ?? 'http://127.0.0.1:5173';

export default defineConfig(async ({ command, mode }) => ({
  base: mode === 'desktop' ? '/office/' : '/',
  plugins: mode === 'desktop' ? [{
    // The desktop renders the manifests it registers from these sources, with its own origin and Ids.
    name: 'ship-manifest',
    generateBundle() {
      for (const host of officeHosts) {
        const fileName = `manifest.${host}.xml`;
        this.emitFile({ type: 'asset', fileName, source: readFileSync(new URL(fileName, import.meta.url), 'utf8') });
      }
    },
  }] : [],
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
    outDir: mode === 'desktop' ? desktopOutDir : 'dist',
    emptyOutDir: true,
    rollupOptions: { input: Object.fromEntries(hosts.map(host => [host, `taskpane.${host}.html`])) },
  },
  test: { include: ['src/**/*.test.ts'] },
}));
