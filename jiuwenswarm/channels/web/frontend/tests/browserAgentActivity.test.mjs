import assert from 'node:assert/strict';
import { after, test } from 'node:test';
import { JSDOM } from 'jsdom';

const dom = new JSDOM('<!doctype html><html><body></body></html>', { url: 'http://localhost/' });
for (const [key, value] of Object.entries({
  window: dom.window,
  document: dom.window.document,
  navigator: dom.window.navigator,
  localStorage: dom.window.localStorage,
  IS_REACT_ACT_ENVIRONMENT: true,
})) {
  Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
}
after(() => dom.window.close());

const { act, createElement } = await import('react');
const { createRoot } = await import('react-dom/client');
const { useBrowserAgentActivity } =
  await import('../node_modules/.cache/browser-agent-activity/features/browserAgentActivity.js');
const { useSessionStore } = await import('../node_modules/.cache/browser-agent-activity/stores/sessionStore.js');
const { useSubagentStore } = await import('../node_modules/.cache/browser-agent-activity/stores/subagentStore.js');

function Probe({ sessionId }) {
  return createElement('output', null, String(useBrowserAgentActivity(sessionId)));
}

function seed(sessionId, mode = 'team') {
  useSessionStore.setState({
    runtimes: {
      ...useSessionStore.getState().runtimes,
      [sessionId]: { mode, teamMemberExecutionEvents: [{ tool_name: 'browser_navigate' }] },
    },
  });
  useSubagentStore.setState({ runtimes: {} });
}

async function mount({ mode = 'team', electron = true, listPanels = async () => [] } = {}) {
  seed('one', mode);
  seed('two', mode);
  const listeners = new Set();
  window.jiuwenDesktop = electron
    ? {
        isElectron: true,
        browser: {
          listPanels,
          onPanelsChanged(callback) {
            listeners.add(callback);
            return () => listeners.delete(callback);
          },
        },
      }
    : undefined;
  const container = document.createElement('div');
  document.body.appendChild(container);
  const root = createRoot(container);
  await act(async () => root.render(createElement(Probe, { sessionId: 'one' })));
  return {
    container,
    listeners,
    emit: (panels) =>
      act(async () => {
        for (const listener of listeners) listener(panels);
      }),
    switchTo: (sessionId) => act(async () => root.render(createElement(Probe, { sessionId }))),
    async close() {
      await act(async () => root.unmount());
      container.remove();
      assert.equal(listeners.size, 0);
    },
  };
}

test('external Swarm browser events do not create an embedded panel', async () => {
  const view = await mount();
  try {
    assert.equal(view.container.textContent, 'false');
    await view.emit([
      { sessionId: 'one', memberId: '' },
      { sessionId: 'two', memberId: 'alice' },
    ]);
    assert.equal(view.container.textContent, 'false');
    await view.emit([{ sessionId: 'one', memberId: 'alice' }]);
    assert.equal(view.container.textContent, 'true');
    await view.emit([]);
    assert.equal(view.container.textContent, 'false');
  } finally {
    await view.close();
  }
});

test('panel state does not leak when changing conversations', async () => {
  const view = await mount({
    listPanels: async (sessionId) => [{ sessionId, memberId: sessionId === 'one' ? 'alice' : '' }],
  });
  try {
    assert.equal(view.container.textContent, 'true');
    await view.switchTo('two');
    assert.equal(view.container.textContent, 'false');
    await view.emit([{ sessionId: 'one', memberId: 'alice' }]);
    assert.equal(view.container.textContent, 'false');
  } finally {
    await view.close();
  }
});

test('an old list response cannot overwrite a newer panel notification', async () => {
  let resolveList;
  const view = await mount({
    listPanels: () =>
      new Promise((resolve) => {
        resolveList = resolve;
      }),
  });
  try {
    await view.emit([{ sessionId: 'one', memberId: 'alice' }]);
    await act(async () => resolveList([]));
    assert.equal(view.container.textContent, 'true');
  } finally {
    await view.close();
  }
});

test('single-agent and ordinary web browser activity keep the original behavior', async () => {
  for (const options of [{ mode: 'agent' }, { electron: false }]) {
    const view = await mount(options);
    try {
      assert.equal(view.container.textContent, 'true');
      assert.equal(view.listeners.size, 0);
    } finally {
      await view.close();
    }
  }
});
