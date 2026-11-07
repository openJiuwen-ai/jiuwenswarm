import assert from 'node:assert/strict';
import test, { before } from 'node:test';
import { mkdir } from 'node:fs/promises';
import { join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { build } from 'esbuild';
import { act, createElement } from 'react';
import { createRoot } from 'react-dom/client';
import { JSDOM, VirtualConsole } from 'jsdom';

const root = new URL('..', import.meta.url);
let sourceText;
let CliAuthModal;
let useConnectorStore;

before(async () => {
  const cacheDir = fileURLToPath(new URL('node_modules/.cache/host-oauth-connector-modal/', root));
  await mkdir(cacheDir, { recursive: true });
  await build({
    entryPoints: [
      { in: fileURLToPath(new URL('src/components/ConnectorMarket/CliAuthModal.tsx', root)), out: 'CliAuthModal' },
      { in: fileURLToPath(new URL('src/stores/connectorStore.ts', root)), out: 'connectorStore' },
    ],
    outExtension: { '.js': '.mjs' },
    plugins: [{
      name: 'stub-logo',
      setup(pluginBuild) {
        pluginBuild.onResolve({ filter: /logo\.svg$/ }, () => ({
          path: 'stub-logo',
          namespace: 'stub',
        }));
        pluginBuild.onLoad({ filter: /.*/, namespace: 'stub' }, () => ({
          contents: 'export default "";\n',
          loader: 'js',
        }));
      },
    }],
    absWorkingDir: fileURLToPath(root),
    bundle: true,
    splitting: true,
    platform: 'node',
    format: 'esm',
    packages: 'external',
    define: { 'import.meta.env.DEV': 'false' },
    outdir: cacheDir,
  });
  sourceText = await (await import('node:fs/promises')).readFile(join(cacheDir, 'CliAuthModal.mjs'), 'utf8');
  ({ CliAuthModal } = await import(pathToFileURL(join(cacheDir, 'CliAuthModal.mjs')).href));
  ({ useConnectorStore } = await import(pathToFileURL(join(cacheDir, 'connectorStore.mjs')).href));
});

test('host OAuth 分支只认后端 hostOAuth 信号', () => {
  assert.match(sourceText, /initial\.hostOAuth === true/);
});

test('拿到授权 URL 后会立即打开浏览器窗口', () => {
  assert.match(sourceText, /window\.open\(response\.authUrl, ["']_blank["'], ["']noopener,noreferrer["']\)/);
});

test('host OAuth 分支不会把 startOAuth 的响应写回 step 触发二次开窗', () => {
  const hostBranch = sourceText.match(/if \(isHostOAuth\) \{[\s\S]*?\n    \}\n    if \(step\.authUrl\)/);
  assert.ok(hostBranch);
  assert.doesNotMatch(hostBranch[0], /setStep\(response\);/);
});

test('host OAuth 分支有启动守卫，同一弹窗不会重复发起 mcp.oauth.start', () => {
  assert.match(sourceText, /hostOAuthStartedRef\.current/);
});

test('startOAuth 只把 loopback origin 传给后端，不带路径', async () => {
  const apiSource = (await import('node:fs/promises')).readFile(
    fileURLToPath(new URL('src/services/connectorApi.ts', root)),
    'utf8',
  );
  assert.match(await apiSource, /const callbackUrl = window\.location\.origin;/);
});

function installDom() {
  const dom = new JSDOM('<!doctype html><div id="root"></div>', { virtualConsole: new VirtualConsole() });
  const globals = {
    window: dom.window,
    document: dom.window.document,
    navigator: dom.window.navigator,
    Element: dom.window.Element,
    HTMLElement: dom.window.HTMLElement,
    Node: dom.window.Node,
    requestAnimationFrame: (cb) => setTimeout(cb, 0),
    cancelAnimationFrame: (id) => clearTimeout(id),
    IS_REACT_ACT_ENVIRONMENT: true,
  };
  const previous = new Map();
  for (const [name, value] of Object.entries(globals)) {
    previous.set(name, Object.getOwnPropertyDescriptor(globalThis, name));
    Object.defineProperty(globalThis, name, { configurable: true, writable: true, value });
  }
  return () => {
    for (const [name, descriptor] of previous) {
      if (descriptor) Object.defineProperty(globalThis, name, descriptor);
      else delete globalThis[name];
    }
    dom.window.close();
  };
}

test('host OAuth 会先 mcp.oauth.start，拿到 authUrl 后立即开窗并进入 waitAuth', async () => {
  const restore = installDom();
  const calls = { startOAuth: 0, waitAuth: 0, opened: 0 };
  const openedUrls = [];
  const container = document.getElementById('root');
  const rootNode = createRoot(container);
  let resolveStart;
  const startOAuthPromise = new Promise((resolve) => { resolveStart = resolve; });

  useConnectorStore.setState({
    startOAuth: () => {
      calls.startOAuth += 1;
      return startOAuthPromise;
    },
    waitAuth: async () => {
      calls.waitAuth += 1;
      return { type: 'connected' };
    },
    cancelConnectAction: async () => {},
    error: null,
    busyMap: {},
    detailCache: {},
    connectors: [],
    builtinConnectors: [],
    myConnectors: [],
  });

  const previousOpen = window.open;
  window.open = (url) => {
    calls.opened += 1;
    openedUrls.push(url);
    return null;
  };

  function Probe() {
    return createElement(CliAuthModal, {
      name: 'canva',
      initial: { type: 'auth_required', hostOAuth: true, authPending: true, stepIndex: 0 },
      onCancel: () => {},
      onConnected: () => {},
    });
  }

  try {
    await act(async () => rootNode.render(createElement(Probe)));
    await act(async () => { resolveStart({ type: 'auth_required', flow: 'flow-1', authUrl: 'https://example.com/auth', stepIndex: 0 }); });
    await act(async () => { await new Promise((r) => setTimeout(r, 10)); });
    assert.ok(calls.startOAuth >= 1);
    assert.deepEqual(openedUrls, ['https://example.com/auth']);
    assert.ok(calls.waitAuth >= 1);
  } finally {
    window.open = previousOpen;
    await act(async () => rootNode.unmount());
    restore();
  }
});
