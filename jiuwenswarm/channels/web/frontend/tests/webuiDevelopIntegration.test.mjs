import assert from 'node:assert/strict';
import test, { after } from 'node:test';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { build, transform } from 'esbuild';
import { JSDOM, VirtualConsole } from 'jsdom';

const dom = new JSDOM('<!doctype html><html><body></body></html>', {
  url: 'http://localhost/portal/chat/new', pretendToBeVisual: true, virtualConsole: new VirtualConsole(),
});
const previous = new Map();
const channels = [];
const NativeMessageChannel = globalThis.MessageChannel;
class TestMessageChannel extends NativeMessageChannel {
  constructor() { super(); channels.push(this); }
}
dom.window.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
dom.window.HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', ''); };
dom.window.HTMLDialogElement.prototype.close = function () { this.removeAttribute('open'); };
for (const [name, value] of Object.entries({
  window: dom.window, document: dom.window.document, navigator: dom.window.navigator,
  Element: dom.window.Element, HTMLElement: dom.window.HTMLElement, SVGElement: dom.window.SVGElement,
  ShadowRoot: dom.window.ShadowRoot, Node: dom.window.Node, MutationObserver: dom.window.MutationObserver,
  PopStateEvent: dom.window.PopStateEvent,
  getComputedStyle: dom.window.getComputedStyle.bind(dom.window),
  requestAnimationFrame: dom.window.requestAnimationFrame.bind(dom.window),
  cancelAnimationFrame: dom.window.cancelAnimationFrame.bind(dom.window),
  IS_REACT_ACT_ENVIRONMENT: true, MessageChannel: TestMessageChannel,
})) {
  previous.set(name, Object.getOwnPropertyDescriptor(globalThis, name));
  Object.defineProperty(globalThis, name, { configurable: true, writable: true, value });
}
after(() => {
  for (const channel of channels) { channel.port1.close(); channel.port2.close(); }
  dom.window.close();
  for (const [name, descriptor] of previous) {
    if (descriptor) Object.defineProperty(globalThis, name, descriptor);
    else delete globalThis[name];
  }
});
const { act, createElement } = await import('react');
const { createRoot } = await import('react-dom/client');

async function moduleFor(transport) {
  const outfile = `node_modules/.cache/webui-develop-integration/${transport}.mjs`;
  await build({
    absWorkingDir: fileURLToPath(new URL('..', import.meta.url)),
    stdin: { resolveDir: fileURLToPath(new URL('..', import.meta.url)), contents: `
      export { WebUIAppRoot } from './src/features/webui/react/WebUIAppRoot.tsx';
      export { ApplicationPresentationHost } from './src/features/webui/presentation/ApplicationPresentationHost.tsx';
      export { SystemPluginRecoverySettings } from './src/features/settings/modules/systemPluginRecovery/SystemPluginRecoverySettings.tsx';
      export { useOptionalWebUIAppContext } from './src/features/webui/react/WebUIProvider.tsx';
      export { GatewayImage } from './src/features/webui/client/GatewayResources.tsx';
      export { gatewayFetch } from './src/services/gatewayFetch.ts';
      export { webClient } from './src/services/webClient.ts';
    ` },
    bundle: true, packages: 'external', platform: 'node', format: 'esm', outfile,
    define: { 'import.meta.env': JSON.stringify({ BASE_URL: '/portal/', VITE_WEB_TRANSPORT: transport }) },
    loader: { '.css': 'empty', '.svg': 'dataurl', '.png': 'dataurl' },
    plugins: [{ name: 'test-svg', setup(builder) {
      builder.onResolve({ filter: /\.svg\?react$/ }, ({ path }) => ({ path, namespace: 'svg-stub' }));
      builder.onLoad({ filter: /.*/, namespace: 'svg-stub' }, () => ({ contents: "export default 'svg';", loader: 'js' }));
    } }],
  });
  return import(new URL(`../${outfile}`, import.meta.url));
}
const http = await moduleFor('http');
const ws = await moduleFor('websocket');
const presentation = {
  sessionId: 'session-1', connected: true, onSelect: async () => {},
  onSend: async () => ({ accepted: false, conversationKey: 'session-1' }),
  onStop: async () => {}, onWorkspace: async () => {},
};
async function mount(tree) {
  const container = document.createElement('div'); container.id = 'root'; document.body.append(container);
  const root = createRoot(container);
  await act(async () => root.render(tree));
  return { container, async close() { await act(async () => root.unmount()); container.remove(); } };
}

test('HTTP pages render without a WebUI runtime, retain native resources and remove the startup cover', async (t) => {
  const requests = [];
  t.mock.method(http.webClient, 'request', async (...args) => { requests.push(args); throw new Error('unexpected WebUI call'); });
  const fetches = [];
  t.mock.method(globalThis, 'fetch', async (...args) => { fetches.push(args); return new Response('native'); });
  const neutral = document.createElement('div'); neutral.id = 'workswarm-neutral'; document.body.append(neutral);
  function Probe() {
    assert.equal(http.useOptionalWebUIAppContext(), null);
    return createElement('main', { 'data-testid': 'native-page' },
      createElement(http.ApplicationPresentationHost, presentation),
      createElement(http.GatewayImage, { src: '/file-api/raw-file?path=report.png' }));
  }
  const view = await mount(createElement(http.WebUIAppRoot, null, createElement(Probe)));
  try {
    assert.ok(view.container.querySelector('[data-testid="native-page"]'));
    assert.equal(view.container.querySelector('img').getAttribute('src'), '/file-api/raw-file?path=report.png');
    assert.equal(document.getElementById('workswarm-neutral'), null);
    assert.equal(view.container.hasAttribute('inert'), false);
    await http.gatewayFetch('/file-api/raw-file?path=report.png', { credentials: 'include' });
    assert.deepEqual(fetches, [['/file-api/raw-file?path=report.png', { credentials: 'include' }]]);
    assert.deepEqual(requests, []);
  } finally { await view.close(); }
});

test('a WS Gateway without V2 methods leaves ordinary pages usable after the splash decision', async (t) => {
  const requests = [];
  t.mock.method(ws.webClient, 'getState', () => 'ready');
  t.mock.method(ws.webClient, 'request', async (method) => { requests.push(method); throw new Error('METHOD_NOT_FOUND'); });
  t.mock.method(globalThis, 'fetch', async () => Response.json({ candidates: true }));
  const view = await mount(createElement(ws.WebUIAppRoot, null,
    createElement('main', { 'data-testid': 'ordinary-chat' }, 'Chat')));
  try {
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 50)); });
    assert.ok(view.container.querySelector('[data-testid="ordinary-chat"]'));
    assert.equal(document.querySelector('[data-testid="webui-splash"]'), null);
    assert.equal(view.container.hasAttribute('inert'), false);
    assert.ok(requests.includes('webui.splash.providers'));
  } finally { await view.close(); }
});

test('the recovery settings page keeps the chat URL and uses the same-origin recovery proxy', async (t) => {
  const fetches = [];
  t.mock.method(globalThis, 'fetch', async (path) => {
    fetches.push(path);
    return Response.json({ url: './proxy' });
  });
  window.history.replaceState(null, '', '/portal/chat/new');
  const view = await mount(createElement(http.WebUIAppRoot, null, createElement(http.SystemPluginRecoverySettings)));
  try {
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 10)); });
    assert.equal(window.location.pathname, '/portal/chat/new');
    assert.deepEqual(fetches, ['/portal/system-plugin-recovery/endpoint']);
    const frame = view.container.querySelector('iframe');
    const frameUrl = new URL(frame.src);
    assert.equal(frameUrl.origin, 'http://localhost');
    assert.equal(frameUrl.pathname, '/portal/system-plugin-recovery/proxy');
    assert.deepEqual([...frameUrl.searchParams.keys()], ['locale']);
    assert.ok(view.container.querySelector('[data-testid="settings-system-plugin-recovery-restore"]'));
    assert.equal(view.container.querySelector('a[target="_blank"]'), null);
  } finally { await view.close(); }
});

test('persistent JiuwenMate is not disabled by a stale recovery URL', async () => {
  window.localStorage.setItem('jiuwenswarm:jiuwenmate-mode', 'true');
  const app = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8');
  const predicate = app.match(/if \((isJiuwenMate[^\r\n]*)\) \{\s*return \(\s*<>\s*<JiuwenMateApp/)[1];
  const compiled = await transform(`export default (isJiuwenMate, route) => ${predicate}`, { format: 'esm' });
  const { default: showJiuwenMate } = await import(`data:text/javascript;base64,${Buffer.from(compiled.code).toString('base64')}`);
  const persisted = window.localStorage.getItem('jiuwenswarm:jiuwenmate-mode') === 'true';
  for (const kind of ['chat-new', 'chat-session', 'group-chat', 'design-project', 'system-plugin-recovery']) {
    assert.equal(showJiuwenMate(persisted, { kind }), true);
  }
  assert.equal(window.localStorage.getItem('jiuwenswarm:jiuwenmate-mode'), 'true');
});

test('login and standalone export entry points clear the neutral layer without opening the WebUI gate', () => {
  const main = readFileSync(new URL('../src/main.tsx', import.meta.url), 'utf8');
  const bootstrap = main.slice(main.indexOf('const runnerQuery ='), main.indexOf('ReactDOM.createRoot('));
  const run = Function('window', 'document', 'isEnterprise', 'isUserLoginRoute', 'userWebPaths', bootstrap);
  for (const [pathname, search, route, cleared] of [
    ['/portal/auth', '', '/auth', true],
    ['/share-export-runner', '?client=1', '/share-export-runner', true],
    ['/portal/chat/new', '', '/chat/new', false],
  ]) {
    let removals = 0;
    run({ location: { pathname, search } }, { getElementById: () => ({ remove: () => { removals++; } }) },
      () => true, (value) => value === '/auth', () => ({ route }));
    assert.equal(removals > 0, cleared, pathname);
  }
});
