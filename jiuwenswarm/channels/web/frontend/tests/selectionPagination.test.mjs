import assert from 'node:assert/strict';
import test from 'node:test';
import { act, createElement } from 'react';
import { createRoot } from 'react-dom/client';
import { JSDOM, VirtualConsole } from 'jsdom';
import { useSelectionPagination } from '../node_modules/.cache/agent-management/SelectionPagination.js';

function installDom() {
  const dom = new JSDOM('<!doctype html><div id="root"></div>', { virtualConsole: new VirtualConsole() });
  const globals = {
    window: dom.window,
    document: dom.window.document,
    navigator: dom.window.navigator,
    Element: dom.window.Element,
    HTMLElement: dom.window.HTMLElement,
    Node: dom.window.Node,
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

function mountProbe() {
  const container = document.getElementById('root');
  const root = createRoot(container);
  const state = { current: null };

  function Probe({ items, resetKey }) {
    state.current = useSelectionPagination(items, resetKey);
    return null;
  }

  return {
    state,
    render: async (items, resetKey) => {
      await act(async () => {
        root.render(createElement(Probe, { items, resetKey }));
      });
    },
    unmount: async () => {
      await act(async () => root.unmount());
    },
  };
}

test('selection pagination slices, clamps and resets at runtime', async () => {
  const restore = installDom();
  const items = Array.from({ length: 21 }, (_, index) => `item-${index + 1}`);
  const probe = mountProbe();

  try {
    await probe.render(items, 'initial');
    assert.equal(probe.state.current.page, 1);
    assert.equal(probe.state.current.totalPages, 3);
    assert.deepEqual(probe.state.current.pageItems, items.slice(0, 10));

    await act(async () => probe.state.current.setPage(2));
    assert.equal(probe.state.current.page, 2);
    assert.deepEqual(probe.state.current.pageItems, items.slice(10, 20));

    await act(async () => probe.state.current.setPage(99));
    assert.equal(probe.state.current.page, 3);
    assert.deepEqual(probe.state.current.pageItems, items.slice(20));

    await probe.render(items, 'changed');
    assert.equal(probe.state.current.page, 1);
    assert.deepEqual(probe.state.current.pageItems, items.slice(0, 10));
  } finally {
    await probe.unmount();
    restore();
  }
});
