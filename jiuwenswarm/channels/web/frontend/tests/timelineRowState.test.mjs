import assert from 'node:assert/strict';
import test from 'node:test';
import { JSDOM } from 'jsdom';

const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'http://localhost/' });
for (const [key, value] of Object.entries({
  window: dom.window,
  document: dom.window.document,
  navigator: dom.window.navigator,
  IS_REACT_ACT_ENVIRONMENT: true,
})) {
  Object.defineProperty(globalThis, key, { configurable: true, value });
}
const { act, createElement: h, StrictMode } = await import('react');
const { createRoot } = await import('react-dom/client');
const { TimelineRowStateProvider, useTimelineRowState } =
  await import('../node_modules/.cache/timeline-row-state/timelineRowState.js');
const { useProcessTreeCollapse } = await import('../node_modules/.cache/timeline-row-state/useProcessTreeCollapse.js');

function Details() {
  const [open, setOpen] = useTimelineRowState('details', false);
  return h('button', { 'aria-expanded': open, onClick: () => setOpen((value) => !value) }, 'details');
}
function Tree({ finished, resetKey }) {
  const [collapsed, setCollapsed] = useProcessTreeCollapse(finished, resetKey);
  return h('button', { 'aria-expanded': !collapsed, onClick: () => setCollapsed((value) => !value) }, 'tree');
}
async function fixture(run) {
  const container = document.getElementById('root');
  const root = createRoot(container);
  const values = new Map();
  const render = async (scope, child) =>
    act(async () =>
      root.render(
        h(StrictMode, null, child && h(TimelineRowStateProvider, { key: scope, values, prefix: scope }, child)),
      ),
    );
  const expanded = () => container.querySelector('button').getAttribute('aria-expanded') === 'true';
  const toggle = async () => act(async () => container.querySelector('button').click());
  try {
    await run({ render, expanded, toggle });
  } finally {
    await act(async () => root.unmount());
  }
}

test('details survive virtual row unmounts and stay isolated by row and session', async () =>
  fixture(async ({ render, expanded, toggle }) => {
    await render('session-a/row-1', h(Details));
    await toggle();
    assert.equal(expanded(), true);
    await render('session-a/row-1', null);
    await render('session-a/row-1', h(Details));
    assert.equal(expanded(), true);
    await render('session-a/row-2', h(Details));
    assert.equal(expanded(), false);
    await render('session-b/row-1', h(Details));
    assert.equal(expanded(), false);
    await render('session-a/row-1', h(Details));
    assert.equal(expanded(), true);
  }));

test('completed tree keeps manual expansion across unmounts but reacts to a new completion', async () =>
  fixture(async ({ render, expanded, toggle }) => {
    await render('session/row', h(Tree, { finished: false, resetKey: 'a' }));
    assert.equal(expanded(), true);
    await render('session/row', h(Tree, { finished: true, resetKey: 'a' }));
    assert.equal(expanded(), false);
    await toggle();
    await render('session/row', null);
    await render('session/row', h(Tree, { finished: true, resetKey: 'a' }));
    assert.equal(expanded(), true);
    await render('session/row', h(Tree, { finished: true, resetKey: 'b' }));
    assert.equal(expanded(), false);
  }));
