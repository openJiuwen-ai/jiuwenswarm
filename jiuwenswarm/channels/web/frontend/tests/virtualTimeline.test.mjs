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

class ResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}
globalThis.ResizeObserver = dom.window.ResizeObserver = ResizeObserver;

const { act, createElement: h } = await import('react');
const { createRoot } = await import('react-dom/client');
const { VirtualTimeline } = await import('../node_modules/.cache/virtual-timeline/VirtualTimeline.mjs');

async function fixture({ heights, viewportHeight, initialState }, run) {
  const keys = heights.map((_, index) => `row-${index}`);
  const container = document.getElementById('root');
  const scrollRoot = document.createElement('div');
  scrollRoot.setAttribute('data-timeline-scroll-root', '');
  container.append(scrollRoot);
  let offset = 0;
  Object.defineProperties(scrollRoot, {
    clientHeight: { get: () => viewportHeight },
    offsetHeight: { get: () => viewportHeight },
    offsetWidth: { get: () => 800 },
    scrollHeight: {
      get: () => Math.max(viewportHeight, Number.parseFloat(scrollRoot.firstChild?.style.height) || 0),
    },
    scrollTop: {
      get: () => offset,
      set: (value) => {
        offset = Math.max(0, Math.min(value, scrollRoot.scrollHeight - viewportHeight));
      },
    },
  });
  scrollRoot.scrollTo = ({ top }) => {
    const previous = offset;
    scrollRoot.scrollTop = top;
    // Browsers do not emit a scroll event when an out-of-range write stays at zero.
    if (offset !== previous) scrollRoot.dispatchEvent(new dom.window.Event('scroll'));
  };
  scrollRoot.getBoundingClientRect = () => ({ top: 0, width: 800, height: viewportHeight });
  const heightDescriptor = Object.getOwnPropertyDescriptor(dom.window.HTMLElement.prototype, 'offsetHeight');
  const rectDescriptor = Object.getOwnPropertyDescriptor(dom.window.Element.prototype, 'getBoundingClientRect');
  Object.defineProperty(dom.window.Element.prototype, 'getBoundingClientRect', {
    configurable: true,
    writable: true,
    value() {
      return { top: (Number.parseFloat(this.style.top) || 0) - offset, width: 800, height: this.offsetHeight };
    },
  });
  Object.defineProperty(dom.window.HTMLElement.prototype, 'offsetHeight', {
    configurable: true,
    get() {
      return heights[Number(this.dataset.index)] ?? 0;
    },
  });
  const root = createRoot(scrollRoot);
  let savedState;
  const render = () => root.render(h(VirtualTimeline, {
    items: keys.map((key) => ({ key })),
    renderItem: (item) => item.key,
    initialState,
    onSaveState: (state) => { savedState = state; },
    canLoadOlderHistory: false,
    historyRequestKey: 'session:1',
  }));
  try {
    await act(async () => render());
    await run({
      scrollRoot,
      indexes: () => Array.from(scrollRoot.querySelectorAll('[data-index]'), (row) => Number(row.dataset.index)),
      save: async () => {
        await act(async () => root.unmount());
        return savedState;
      },
    });
  } finally {
    await act(async () => root.unmount());
    Object.defineProperty(dom.window.HTMLElement.prototype, 'offsetHeight', heightDescriptor);
    Object.defineProperty(dom.window.Element.prototype, 'getBoundingClientRect', rectDescriptor);
    container.replaceChildren();
  }
}

test('a short conversation mounts every row without waiting for a scroll event', async () => {
  await fixture({
    heights: [90, 68, 90, 68, 24, 24, 24, 84],
    viewportHeight: 800,
  }, async ({ indexes, scrollRoot, save }) => {
    assert.equal(scrollRoot.scrollTop, 0);
    assert.deepEqual(indexes(), [0, 1, 2, 3, 4, 5, 6, 7]);
    assert.equal((await save()).offset, 0);
  });
});

test('a long conversation follows the host scroll to the bottom and back to older rows', async () => {
  await fixture({ heights: Array(100).fill(120), viewportHeight: 600 }, async ({ indexes, scrollRoot, save }) => {
    assert.equal(scrollRoot.scrollTop, 0);
    assert.equal(indexes()[0], 0);
    assert.ok(indexes().length < 100);
    await act(async () => scrollRoot.scrollTo({ top: scrollRoot.scrollHeight }));
    assert.ok(indexes().includes(99));
    assert.ok(!indexes().includes(0));
    await act(async () => scrollRoot.scrollTo({ top: 0 }));
    assert.equal(indexes()[0], 0);
    assert.equal((await save()).offset, 0);
  });
});

test('a saved conversation restores its measured viewport and reading position', async () => {
  const measurements = Array.from({ length: 100 }, (_, index) => ({
    index,
    key: `row-${index}`,
    start: index * 128,
    end: index * 128 + 120,
    size: 120,
    lane: 0,
  }));
  await fixture({
    heights: Array(100).fill(120),
    viewportHeight: 600,
    initialState: { measurements, offset: 2560 },
  }, async ({ indexes, scrollRoot, save }) => {
    assert.equal(scrollRoot.scrollTop, 2560);
    assert.ok(indexes().includes(20));
    assert.ok(!indexes().includes(0));
    assert.equal((await save()).offset, 2560);
  });
});
