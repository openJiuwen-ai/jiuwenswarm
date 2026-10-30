import assert from 'node:assert/strict';
import test from 'node:test';
import { StrictMode, act, createElement, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { JSDOM, VirtualConsole } from 'jsdom';

import { createRendererRegistry } from '../node_modules/.cache/webui-renderer/renderer/index.js';
import { WebUIHost } from '../node_modules/.cache/webui-renderer/react/WebUIHost.js';
import { WebUIInstanceView } from '../node_modules/.cache/webui-renderer/react/WebUIInstanceView.js';
import { WebUIProvider } from '../node_modules/.cache/webui-renderer/react/WebUIProvider.js';
import { createWebUIRuntime } from '../node_modules/.cache/webui-renderer/runtime/index.js';
import { createMockTransport } from '../node_modules/.cache/webui-renderer/transport/index.js';
import { createWebUIAppRuntime } from '../node_modules/.cache/webui-renderer/assembly/index.js';
import { webUIInstanceKey } from '../node_modules/.cache/webui-renderer/contracts/index.js';
import { DEMO_SLOT, DEMO_SLOT_SINGLE, demoCatalog } from '../node_modules/.cache/webui-renderer/testing/demoCatalog.js';
import { demoClearFrame, demoRenderFrame } from '../node_modules/.cache/webui-renderer/testing/fixtures.js';

function installDom() {
  const dom = new JSDOM('<!doctype html><div id="root"></div>', { virtualConsole: new VirtualConsole() });
  const globals = {
    window: dom.window,
    document: dom.window.document,
    navigator: dom.window.navigator,
    Element: dom.window.Element,
    HTMLElement: dom.window.HTMLElement,
    Node: dom.window.Node,
    MutationObserver: dom.window.MutationObserver,
    getComputedStyle: dom.window.getComputedStyle.bind(dom.window),
    requestAnimationFrame: (callback) => setTimeout(callback, 0),
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

function createProbeAdapter() {
  let renders = 0;
  function Adapter({ node, children, context, dispatch }) {
    renders += 1;
    const [local, setLocal] = useState(0);
    const [status, setStatus] = useState('');
    if (node.props.title === 'crash') {
      throw new Error('adapter crash');
    }
    return createElement(
      'section',
      { 'data-testid': 'probe', 'data-instance': context.instanceKey, 'data-tone': String(node.props.tone ?? '') },
      createElement('span', { 'data-testid': 'probe-title' }, String(node.props.title ?? '')),
      createElement('span', { 'data-testid': 'probe-local' }, String(local)),
      createElement('button', { 'data-testid': 'probe-local-button', onClick: () => setLocal((value) => value + 1) }),
      node.action
        ? createElement('button', {
            'data-testid': 'probe-action',
            onClick: async () => {
              const result = await dispatch();
              setStatus(result.status);
            },
          })
        : null,
      status ? createElement('span', { 'data-testid': 'probe-status' }, status) : null,
      children,
    );
  }
  return { Adapter, renders: () => renders, reset: () => { renders = 0; } };
}

const INSTANCE_KEY = 'sess-1|conversation.panel.right|pkg.demo|main';

function setup() {
  const transport = createMockTransport();
  const runtime = createWebUIRuntime({ transport, catalog: demoCatalog });
  runtime.start();
  return { transport, runtime };
}

async function flush() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

function renderHost(root, runtime, registry, options = {}) {
  return root.render(
    createElement(
      WebUIProvider,
      { runtime, registry },
      createElement(WebUIHost, { slotId: DEMO_SLOT, sessionId: 'sess-1', ...options }),
    ),
  );
}

test('single slots show the latest accepted instance and fall back after clear', async () => {
  const restoreDom = installDom();
  const { transport, runtime } = setup();
  const probe = createProbeAdapter();
  const registry = createRendererRegistry({ demo: probe.Adapter });
  const root = createRoot(document.getElementById('root'));
  const target = (instanceKey) => ({ slot_id: DEMO_SLOT_SINGLE, instance_key: instanceKey });
  const send = (instanceKey, revision) => transport.injectRenderFrame(demoRenderFrame({
    target: target(instanceKey), revision, root: { component: 'demo', props: { title: instanceKey } },
  }));
  const visibleTitles = () => [...document.querySelectorAll('[data-testid="probe-title"]')]
    .map((node) => node.textContent);
  try {
    await act(async () => renderHost(root, runtime, registry, { slotId: DEMO_SLOT_SINGLE }));
    await act(async () => {
      send('first', 100);
      send('second', 1);
    });
    assert.deepEqual(visibleTitles(), ['second'], 'revisions from different instances are not comparable');

    await act(async () => send('first', 101));
    assert.deepEqual(visibleTitles(), ['first'], 'updating an existing instance changes the selected instance');

    await act(async () => {
      send('second', 1);
      transport.injectRenderFrame(demoRenderFrame({
        target: target('second'), revision: 2, root: { component: 'unregistered' },
      }));
    });
    assert.deepEqual(visibleTitles(), ['first'], 'stale or invalid frames do not take over the slot');

    await act(async () => send('second', 2));
    assert.deepEqual(visibleTitles(), ['second']);
    await act(async () => transport.injectClearFrame({
      target: { session_id: 'sess-1', package_id: 'pkg.demo', ...target('second') }, revision: 3,
    }));
    assert.deepEqual(visibleTitles(), ['first']);
  } finally {
    await act(async () => root.unmount());
    runtime.dispose();
    restoreDom();
  }
});

test('single slots filter an explicitly requested instance before selecting the latest frame', async () => {
  const restoreDom = installDom();
  const { transport, runtime } = setup();
  const probe = createProbeAdapter();
  const registry = createRendererRegistry({ demo: probe.Adapter });
  const root = createRoot(document.getElementById('root'));
  try {
    await act(async () => renderHost(root, runtime, registry, { slotId: DEMO_SLOT_SINGLE, instanceKey: 'first' }));
    await act(async () => {
      for (const instanceKey of ['first', 'second']) {
        transport.injectRenderFrame(demoRenderFrame({
          target: { slot_id: DEMO_SLOT_SINGLE, instance_key: instanceKey },
          root: { component: 'demo', props: { title: instanceKey } },
        }));
      }
    });
    const titles = [...document.querySelectorAll('[data-testid="probe-title"]')].map((node) => node.textContent);
    assert.deepEqual(titles, ['first']);
  } finally {
    await act(async () => root.unmount());
    runtime.dispose();
    restoreDom();
  }
});

test('host renders frames, confirms display and exposes the adapter context', async () => {
  const restoreDom = installDom();
  const { transport, runtime } = setup();
  const probe = createProbeAdapter();
  const registry = createRendererRegistry({ demo: probe.Adapter });
  const root = createRoot(document.getElementById('root'));
  try {
    await act(async () => {
      renderHost(root, runtime, registry);
    });
    await act(async () => {
      transport.injectRenderFrame(demoRenderFrame({ revision: 1 }));
    });
    assert.equal(document.querySelector('[data-testid="probe-title"]').textContent, 'hello');
    assert.equal(runtime.getInstance(INSTANCE_KEY).displayedRevision, 1);
    assert.equal(document.querySelector('[data-testid="probe"]').getAttribute('data-instance'), INSTANCE_KEY);
  } finally {
    await act(async () => root.unmount());
    runtime.dispose();
    restoreDom();
  }
});

test('updating one instance does not execute adapters of unrelated instances', async () => {
  const restoreDom = installDom();
  const { transport, runtime } = setup();
  const probe = createProbeAdapter();
  const registry = createRendererRegistry({ demo: probe.Adapter });
  const root = createRoot(document.getElementById('root'));
  try {
    await act(async () => {
      renderHost(root, runtime, registry);
    });
    await act(async () => {
      transport.injectRenderFrame(demoRenderFrame({ revision: 1 }));
    });
    await act(async () => {
      transport.injectRenderFrame(demoRenderFrame({ revision: 1, target: { instance_key: 'other' } }));
    });
    const baseline = probe.renders();

    await act(async () => {
      transport.injectRenderFrame(demoRenderFrame({ revision: 2 }));
    });
    assert.equal(probe.renders(), baseline + 2, 'only the updated instance tree re-renders');
  } finally {
    await act(async () => root.unmount());
    runtime.dispose();
    restoreDom();
  }
});

test('each legal replace rebuilds node local state, so reorder cannot inherit values', async () => {
  const restoreDom = installDom();
  const { transport, runtime } = setup();
  const probe = createProbeAdapter();
  const registry = createRendererRegistry({ demo: probe.Adapter });
  const root = createRoot(document.getElementById('root'));
  const tree = {
    component: 'demo',
    props: { title: 'root' },
    children: [
      { component: 'demo', props: { title: 'a' } },
      { component: 'demo', props: { title: 'b' } },
    ],
  };
  try {
    await act(async () => {
      renderHost(root, runtime, registry);
    });
    await act(async () => {
      transport.injectRenderFrame(demoRenderFrame({ revision: 1, root: tree }));
    });
    const localButtons = () => document.querySelectorAll('[data-testid="probe-local-button"]');
    await act(async () => {
      localButtons()[2].dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
    });
    const localsBefore = [...document.querySelectorAll('[data-testid="probe-local"]')].map((node) => node.textContent);
    assert.ok(localsBefore.includes('1'));

    await act(async () => {
      transport.injectRenderFrame(
        demoRenderFrame({
          revision: 2,
          root: {
            component: 'demo',
            props: { title: 'root' },
            children: [
              { component: 'demo', props: { title: 'b' } },
              { component: 'demo', props: { title: 'a' } },
            ],
          },
        }),
      );
    });
    const localsAfter = [...document.querySelectorAll('[data-testid="probe-local"]')].map((node) => node.textContent);
    assert.deepEqual(localsAfter, ['0', '0', '0']);
  } finally {
    await act(async () => root.unmount());
    runtime.dispose();
    restoreDom();
  }
});

test('a missing adapter fails the whole instance tree instead of showing partial content', async () => {
  const restoreDom = installDom();
  const { transport, runtime } = setup();
  const registry = createRendererRegistry({});
  const root = createRoot(document.getElementById('root'));
  try {
    await act(async () => {
      root.render(
        createElement(WebUIInstanceView, { runtime, registry, instanceKey: INSTANCE_KEY }),
      );
    });
    await act(async () => {
      transport.injectRenderFrame(demoRenderFrame({ revision: 1 }));
    });
    const error = document.querySelector('[data-testid="webui-instance-error"]');
    assert.notEqual(error, null);
    assert.match(error.textContent, /adapter is missing/);
    assert.equal(document.querySelector('[data-testid="probe"]'), null);
  } finally {
    await act(async () => root.unmount());
    runtime.dispose();
    restoreDom();
  }
});

test('a crashing instance is isolated and recovers on the next frame', async () => {
  const restoreDom = installDom();
  const { transport, runtime } = setup();
  const probe = createProbeAdapter();
  const registry = createRendererRegistry({ demo: probe.Adapter });
  const root = createRoot(document.getElementById('root'));
  try {
    await act(async () => {
      renderHost(root, runtime, registry);
    });
    await act(async () => {
      transport.injectRenderFrame(
        demoRenderFrame({ revision: 1, root: { component: 'demo', props: { title: 'crash' } } }),
      );
    });
    await act(async () => {
      transport.injectRenderFrame(
        demoRenderFrame({ revision: 1, target: { instance_key: 'healthy' }, root: { component: 'demo', props: { title: 'ok' } } }),
      );
    });
    assert.notEqual(document.querySelector('[data-testid="webui-instance-error"]'), null);
    assert.ok([...document.querySelectorAll('[data-testid="probe-title"]')].some((node) => node.textContent === 'ok'));

    await act(async () => {
      transport.injectRenderFrame(demoRenderFrame({ revision: 2, root: { component: 'demo', props: { title: 'recovered' } } }));
    });
    assert.equal(document.querySelector('[data-testid="webui-instance-error"]'), null);
    assert.ok([...document.querySelectorAll('[data-testid="probe-title"]')].some((node) => node.textContent === 'recovered'));
  } finally {
    await act(async () => root.unmount());
    runtime.dispose();
    restoreDom();
  }
});

test('interaction results are observable, including transport failures', async () => {
  const restoreDom = installDom();
  const { transport, runtime } = setup();
  const probe = createProbeAdapter();
  const registry = createRendererRegistry({ demo: probe.Adapter });
  const root = createRoot(document.getElementById('root'));
  try {
    await act(async () => {
      renderHost(root, runtime, registry);
    });
    await act(async () => {
      transport.injectRenderFrame(demoRenderFrame({ revision: 1 }));
    });
    await act(async () => {
      document
        .querySelector('[data-testid="probe-action"]')
        .dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
    });
    await flush();
    assert.equal(document.querySelector('[data-testid="probe-status"]').textContent, 'sent');

    transport.dispatch = async () => {
      throw new Error('offline');
    };
    await act(async () => {
      transport.injectRenderFrame(demoRenderFrame({ revision: 2 }));
    });
    await act(async () => {
      document
        .querySelector('[data-testid="probe-action"]')
        .dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
    });
    await flush();
    assert.equal(document.querySelector('[data-testid="probe-status"]').textContent, 'failed');
  } finally {
    await act(async () => root.unmount());
    runtime.dispose();
    restoreDom();
  }
});

test('multiple hosts own the same scope independently and release only their handle', async () => {
  const restoreDom = installDom();
  const { transport, runtime } = setup();
  const probe = createProbeAdapter();
  const registry = createRendererRegistry({ demo: probe.Adapter });
  const firstContainer = document.createElement('div');
  const secondContainer = document.createElement('div');
  document.body.append(firstContainer, secondContainer);
  const firstRoot = createRoot(firstContainer);
  const secondRoot = createRoot(secondContainer);
  try {
    await act(async () => {
      renderHost(firstRoot, runtime, registry);
      renderHost(secondRoot, runtime, registry);
    });
    await act(async () => {
      transport.injectRenderFrame(demoRenderFrame({ revision: 1 }));
    });
    assert.equal(runtime.getScopeSnapshot('sess-1', DEMO_SLOT).mounted, true);

    await act(async () => firstRoot.unmount());
    assert.equal(runtime.getScopeSnapshot('sess-1', DEMO_SLOT).mounted, true);
    assert.notEqual(secondContainer.querySelector('[data-testid="probe"]'), null);

    await act(async () => secondRoot.unmount());
    assert.equal(runtime.getScopeSnapshot('sess-1', DEMO_SLOT).mounted, false);
    assert.equal(runtime.getInstance(INSTANCE_KEY), undefined);
  } finally {
    runtime.dispose();
    restoreDom();
  }
});

test('StrictMode mount and cleanup do not leak scope ownership', async () => {
  const restoreDom = installDom();
  const { runtime } = setup();
  const probe = createProbeAdapter();
  const registry = createRendererRegistry({ demo: probe.Adapter });
  const root = createRoot(document.getElementById('root'));
  try {
    await act(async () => {
      root.render(
        createElement(
          StrictMode,
          null,
          createElement(
            WebUIProvider,
            { runtime, registry },
            createElement(WebUIHost, { slotId: DEMO_SLOT, sessionId: 'sess-1' }),
          ),
        ),
      );
    });
    assert.equal(runtime.getScopeSnapshot('sess-1', DEMO_SLOT).mounted, true);
    await act(async () => root.unmount());
    assert.equal(runtime.getScopeSnapshot('sess-1', DEMO_SLOT).mounted, false);
  } finally {
    runtime.dispose();
    restoreDom();
  }
});

test('clear and rebuild with the same revision remounts the instance tree', async () => {
  const restoreDom = installDom();
  const { transport, runtime } = setup();
  const probe = createProbeAdapter();
  const registry = createRendererRegistry({ demo: probe.Adapter });
  const root = createRoot(document.getElementById('root'));
  const tree = (title) => ({ component: 'demo', props: { title }, children: [] });
  try {
    await act(async () => {
      renderHost(root, runtime, registry);
    });
    await act(async () => {
      transport.injectRenderFrame(demoRenderFrame({ revision: 1, root: tree('first') }));
    });
    await act(async () => {
      document
        .querySelector('[data-testid="probe-local-button"]')
        .dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
    });
    assert.equal(document.querySelector('[data-testid="probe-local"]').textContent, '1');

    await act(async () => {
      transport.injectClearFrame(demoClearFrame({ revision: 2 }));
      transport.injectRenderFrame(demoRenderFrame({ revision: 1, root: tree('rebuilt') }));
    });
    assert.equal(document.querySelector('[data-testid="probe-title"]').textContent, 'rebuilt');
    assert.equal(
      document.querySelector('[data-testid="probe-local"]').textContent,
      '0',
      'local state of the cleared tree must not leak into the rebuilt tree',
    );
    assert.equal(
      runtime.getInstance(INSTANCE_KEY).displayedRevision,
      1,
      'rebuilt frame is confirmed even though its revision equals the cleared frame revision',
    );
  } finally {
    await act(async () => root.unmount());
    runtime.dispose();
    restoreDom();
  }
});

test('clear and rebuild with the same revision drops the previous render failure', async () => {
  const restoreDom = installDom();
  const { transport, runtime } = setup();
  const probe = createProbeAdapter();
  const registry = createRendererRegistry({ demo: probe.Adapter });
  const root = createRoot(document.getElementById('root'));
  try {
    await act(async () => {
      renderHost(root, runtime, registry);
    });
    await act(async () => {
      transport.injectRenderFrame(
        demoRenderFrame({ revision: 1, root: { component: 'demo', props: { title: 'crash' }, children: [] } }),
      );
    });
    assert.notEqual(document.querySelector('[data-testid="webui-instance-error"]'), null);

    await act(async () => {
      transport.injectClearFrame(demoClearFrame({ revision: 2 }));
      transport.injectRenderFrame(
        demoRenderFrame({ revision: 1, root: { component: 'demo', props: { title: 'healthy' }, children: [] } }),
      );
    });
    assert.equal(document.querySelector('[data-testid="webui-instance-error"]'), null, 'old failure must not survive the rebuild');
    assert.equal(document.querySelector('[data-testid="probe-title"]').textContent, 'healthy');
    assert.equal(runtime.getInstance(INSTANCE_KEY).displayedRevision, 1);
  } finally {
    await act(async () => root.unmount());
    runtime.dispose();
    restoreDom();
  }
});

test('assembly rejects a catalog and registry that do not match', () => {
  const client = {
    on: () => () => undefined,
    onStateChange: () => () => undefined,
    getState: () => 'ready',
    request: async () => ({}),
  };
  assert.throws(
    () => createWebUIAppRuntime({ client, catalog: demoCatalog, registry: createRendererRegistry({}) }),
    /adapter is missing/,
  );
});
