import assert from 'node:assert/strict';
import test, { before } from 'node:test';
import { mkdir } from 'node:fs/promises';
import { join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { build } from 'esbuild';
import { act, createElement, useEffect } from 'react';
import { createRoot } from 'react-dom/client';
import { JSDOM, VirtualConsole } from 'jsdom';

const root = new URL('..', import.meta.url);
let hookMod;

// usePendingConnectorFlow.tsx 只有连接状态机是纯逻辑，PendingConnectorModals（连带 ConnectTokenModal/
// CliAuthModal，后者又 import 了 Vite 专属的 '/logo.svg' 绝对资源路径）在本测试里根本不会渲染。用
// esbuild 插件把这三个 stub 掉，只留下 hook 逻辑 + connectorStore（真的 zustand store，供测试替换
// connect 实现）。
before(async () => {
  // 输出到 frontend 的 node_modules/.cache 下（不是系统临时目录），这样 --packages=external
  // 留下的 `import 'react'` 等裸导入能从 frontend 的 node_modules 解析到（其他 test:* 同款做法）。
  const cacheDir = fileURLToPath(new URL('node_modules/.cache/pending-connector-flow/', root));
  await mkdir(cacheDir, { recursive: true });
  const stubModals = {
    name: 'stub-connector-modals',
    setup(pluginBuild) {
      pluginBuild.onResolve({ filter: /(ConnectTokenModal|CliAuthModal)$/ }, () => ({
        path: 'stub-connector-modals',
        namespace: 'stub',
      }));
      pluginBuild.onLoad({ filter: /.*/, namespace: 'stub' }, () => ({
        contents: 'export const ConnectTokenModal = () => null;\nexport const CliAuthModal = () => null;',
        loader: 'js',
      }));
    },
  };
  await build({
    entryPoints: [
      { in: fileURLToPath(new URL('src/components/ConnectorMarket/usePendingConnectorFlow.tsx', root)), out: 'hook' },
      { in: fileURLToPath(new URL('src/stores/connectorStore.ts', root)), out: 'connectorStore' },
    ],
    absWorkingDir: fileURLToPath(root),
    bundle: true,
    splitting: true,
    format: 'esm',
    platform: 'node',
    packages: 'external',
    define: { 'import.meta.env.DEV': 'false' },
    outdir: cacheDir,
    plugins: [stubModals],
  });
  const hook = await import(pathToFileURL(join(cacheDir, 'hook.js')).href);
  const store = await import(pathToFileURL(join(cacheDir, 'connectorStore.js')).href);
  hookMod = { usePendingConnectorFlow: hook.usePendingConnectorFlow, useConnectorStore: store.useConnectorStore };
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

/** 挂一个只在挂载时 start() 一组名单的探针组件，把 flow 的回调计数暴露出来。 */
function mountFlow({ names, connectMock }) {
  const { usePendingConnectorFlow, useConnectorStore } = hookMod;
  useConnectorStore.setState({ connect: connectMock });
  const calls = { allConnected: 0, aborted: [] };
  const container = document.getElementById('root');
  const rootNode = createRoot(container);

  function Probe() {
    const flow = usePendingConnectorFlow(
      () => { calls.allConnected += 1; },
      (reason) => { calls.aborted.push(reason); },
    );
    useEffect(() => {
      flow.start(names);
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);
    return createElement('span', null, flow.active ? 'active' : 'idle');
  }

  return {
    calls,
    container,
    unmount: () => act(async () => rootNode.unmount()),
    render: async () => {
      await act(async () => rootNode.render(createElement(Probe)));
      await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
    },
  };
}

test('硬失败（connect 返回 null）中止续跑：触发 onAborted(failed)，不触发 onAllConnected', async () => {
  const restore = installDom();
  const seen = [];
  const flow = mountFlow({
    names: ['bad-mcp'],
    connectMock: async (name) => { seen.push(name); return null; },
  });
  try {
    await flow.render();
    assert.deepEqual(seen, ['bad-mcp']);
    assert.equal(flow.calls.allConnected, 0, 'onAllConnected 不能被调用');
    assert.deepEqual(flow.calls.aborted, ['failed'], 'onAborted 必须以 failed 触发一次');
    assert.equal(flow.container.textContent, 'idle', 'flow 应回到非 active');
  } finally {
    await flow.unmount();
    restore();
  }
});

test('第二个 connector 硬失败：整体走 onAborted 而非 onAllConnected', async () => {
  const restore = installDom();
  const flow = mountFlow({
    names: ['ok-mcp', 'bad-mcp'],
    connectMock: async (name) => (name === 'bad-mcp' ? null : { type: 'connected', name }),
  });
  try {
    await flow.render();
    assert.equal(flow.calls.allConnected, 0);
    assert.deepEqual(flow.calls.aborted, ['failed']);
  } finally {
    await flow.unmount();
    restore();
  }
});

test('全部连接成功：只触发 onAllConnected，不触发 onAborted', async () => {
  const restore = installDom();
  const flow = mountFlow({
    names: ['a', 'b'],
    connectMock: async (name) => ({ type: 'connected', name }),
  });
  try {
    await flow.render();
    assert.equal(flow.calls.allConnected, 1);
    assert.deepEqual(flow.calls.aborted, []);
  } finally {
    await flow.unmount();
    restore();
  }
});
