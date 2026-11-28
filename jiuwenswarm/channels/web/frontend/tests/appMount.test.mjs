import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import { build } from 'esbuild';
import { JSDOM, VirtualConsole } from 'jsdom';
import { act, createElement } from 'react';

test('the production App mounts with all startup effects inside its error boundary', async () => {
  const errors = [];
  const virtualConsole = new VirtualConsole();
  virtualConsole.on('jsdomError', (error) => {
    // jsdom 26 cannot parse the CSS layers injected by the A2UI provider.
    if (error.type !== 'css parsing') errors.push(String(error));
  });
  const dom = new JSDOM('<!doctype html><div id="root"></div>', {
    url: 'http://localhost/', pretendToBeVisual: true, virtualConsole,
  });
  const previous = new Map();
  const originalError = console.error;
  let renderer;
  let client;
  let appServices;
  for (const [key, value] of Object.entries({
    window: dom.window, document: dom.window.document, navigator: dom.window.navigator,
    HTMLElement: dom.window.HTMLElement, Element: dom.window.Element,
    Node: dom.window.Node, self: dom.window, localStorage: dom.window.localStorage,
    sessionStorage: dom.window.sessionStorage,
    SVGElement: dom.window.SVGElement, CustomEvent: dom.window.CustomEvent,
    getComputedStyle: dom.window.getComputedStyle.bind(dom.window),
    MutationObserver: dom.window.MutationObserver, IS_REACT_ACT_ENVIRONMENT: true,
    ResizeObserver: class { observe() {} unobserve() {} disconnect() {} },
    WebSocket: class {
      static CONNECTING = 0;
      static OPEN = 1;
      static CLOSED = 3;
      readyState = 0;
      addEventListener() {}
      removeEventListener() {}
      close() { this.readyState = 3; }
    },
    fetch: async () => new Response('{}', { headers: { 'Content-Type': 'application/json' } }),
    requestAnimationFrame: dom.window.requestAnimationFrame.bind(dom.window),
    cancelAnimationFrame: dom.window.cancelAnimationFrame.bind(dom.window),
  })) {
    previous.set(key, Object.getOwnPropertyDescriptor(globalThis, key));
    Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
  }
  dom.window.matchMedia = (query) => ({
    matches: false, media: query, addEventListener() {}, removeEventListener() {},
    addListener() {}, removeListener() {},
  });
  dom.window.ResizeObserver = globalThis.ResizeObserver;
  dom.window.WebSocket = globalThis.WebSocket;
  dom.window.HTMLCanvasElement.prototype.getContext = () => null;
  const require = createRequire(import.meta.url);
  try {
    const built = await build({
      stdin: {
        contents: `
          export { default as App } from './src/App';
          export { WebUIProvider } from './src/features/webui/react/WebUIProvider';
          export { createWebUIAppRuntime } from './src/features/webui/assembly/createWebUIAppRuntime';
          export { componentCatalog } from './src/features/webui/catalog/componentCatalog';
          export { createComponentRendererRegistry } from './src/features/webui/renderer/adapters/componentRegistry';
          export { browserClientCapabilities } from './src/features/webui/client/browserClient';
          export { A2UIProvider } from '@a2ui/react';
          export { openSourceSettingsPageDefinition } from './src/features/settings/registry/openSourceDefinition';
          export { useAuthStore } from './src/stores/authStore';
          export { webClient } from './src/services/webClient';
        `,
        loader: 'tsx', resolveDir: fileURLToPath(new URL('../', import.meta.url)),
      },
      bundle: true, write: false, platform: 'node', format: 'cjs', jsx: 'automatic',
      packages: 'external',
      outdir: 'node_modules/.cache/app-mount',
      external: ['react', 'react/*', 'react-dom', 'react-dom/*', 'i18next', 'react-i18next'],
      loader: {
        '.css': 'empty', '.svg': 'dataurl', '.png': 'dataurl', '.jpg': 'dataurl',
        '.gif': 'dataurl', '.webp': 'dataurl', '.woff': 'dataurl', '.woff2': 'dataurl',
      },
      plugins: [{
        name: 'decorative-svg-components',
        setup(builder) {
          builder.onResolve({ filter: /\.svg\?react$/ }, ({ path }) => ({ path, namespace: 'decorative-svg' }));
          builder.onLoad({ filter: /.*/, namespace: 'decorative-svg' }, () => ({
            contents: 'export default () => null;', loader: 'js',
          }));
          builder.onResolve({ filter: /^\/.*\.(svg|png|jpg)$/ }, ({ path }) => ({ path, namespace: 'public-asset' }));
          builder.onLoad({ filter: /.*/, namespace: 'public-asset' }, ({ path }) => ({
            contents: `export default ${JSON.stringify(path)};`, loader: 'js',
          }));
        },
      }],
      define: { 'import.meta.glob': 'testAssetGlob', 'import.meta.env': '{}' },
      banner: { js: 'const testAssetGlob = () => ({});' },
      logLevel: 'silent',
    });
    const compiled = { exports: {} };
    const requireWithoutStyles = (name) => name.endsWith('.css') ? {} : require(name);
    new Function('require', 'module', 'exports', built.outputFiles[0].text)(requireWithoutStyles, compiled, compiled.exports);
    const { App, WebUIProvider, createWebUIAppRuntime, componentCatalog, createComponentRendererRegistry,
      browserClientCapabilities, A2UIProvider, openSourceSettingsPageDefinition, useAuthStore, webClient } = compiled.exports;
    client = webClient;
    appServices = createWebUIAppRuntime({
      client, clientCapabilities: browserClientCapabilities, catalog: componentCatalog,
      registry: createComponentRendererRegistry(),
    });
    useAuthStore.setState({ initialized: true, enabled: false, campaignState: 'off' });
    const { createRoot } = await import('react-dom/client');
    console.error = (...values) => errors.push(values.map(String).join(' '));
    renderer = createRoot(dom.window.document.getElementById('root'));
    await act(async () => {
      renderer.render(createElement(A2UIProvider, { onAction() {} },
        createElement(WebUIProvider, { runtime: appServices.runtime, registry: appServices.registry }, createElement(App, {
          settingsPageDefinition: openSourceSettingsPageDefinition,
          resolveSettingsRequest: (request) => request,
        })),
      ));
    });
    assert.equal(Boolean(dom.window.document.querySelector('[data-testid="app-error-fallback"]')), false,
      errors.join('\n'));
    assert.deepEqual(errors.filter((message) => /ReferenceError|not defined|The above error occurred/.test(message)), []);
    assert.ok(dom.window.document.querySelector('[data-testid="app-shell"]'), 'App shell remains mounted');
  } catch (error) {
    process.stderr.write(`${error.stack || error}\n${errors.join('\n')}\n`);
    throw error;
  } finally {
    if (renderer) await act(async () => renderer.unmount());
    client?.disconnect();
    appServices?.dispose();
    console.error = originalError;
    dom.window.close();
    for (const [name, descriptor] of previous) {
      if (descriptor) Object.defineProperty(globalThis, name, descriptor);
      else delete globalThis[name];
    }
  }
});
