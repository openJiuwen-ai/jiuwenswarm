import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { after, test } from 'node:test';
import { JSDOM } from 'jsdom';

const css = readFileSync(new URL('../src/components/ui/EntityHeader/EntityHeader.css', import.meta.url), 'utf8');
const dom = new JSDOM(`<!doctype html><html><head><style>${css}</style></head><body></body></html>`);
for (const [key, value] of Object.entries({
  window: dom.window, document: dom.window.document, navigator: dom.window.navigator,
  IS_REACT_ACT_ENVIRONMENT: true,
})) {
  Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
}
after(() => dom.window.close());

const { createElement } = await import('react');
const { act } = await import('react-dom/test-utils');
const { createRoot } = await import('react-dom/client');
const { EntityTagList } = await import('../node_modules/.cache/entity-header/EntityHeader.mjs');

// JSDOM does not expand the flex gap shorthand into columnGap.
const getComputedStyle = dom.window.getComputedStyle.bind(dom.window);
dom.window.getComputedStyle = element => {
  const styles = getComputedStyle(element);
  if (element.classList.contains('entity-header__tags')) styles.columnGap = styles.gap;
  return styles;
};

async function withTags(tags, initialWidth, check) {
  let width = initialWidth;
  let notifyResize;
  let disconnected = false;
  const observed = new Set();
  const originalRect = dom.window.HTMLElement.prototype.getBoundingClientRect;
  dom.window.HTMLElement.prototype.getBoundingClientRect = function () {
    if (this.classList.contains('entity-header__tags')) return { width };
    if (this.classList.contains('entity-header__tag')) return { width: this.textContent.length * 8 + 16 };
    return originalRect.call(this);
  };
  globalThis.ResizeObserver = class {
    constructor(callback) { notifyResize = callback; }
    observe(element) { observed.add(element); }
    disconnect() { disconnected = true; }
  };
  const container = document.createElement('div');
  document.body.appendChild(container);
  const root = createRoot(container);
  const render = next => act(async () => root.render(createElement(EntityTagList, { tags: next })));
  try {
    await render(tags);
    await check({
      visible: () => [...container.querySelectorAll('[data-testid="entity-header-tag"]')].map(el => el.textContent),
      overflow: () => container.querySelector('[data-testid="entity-header-tag-overflow"]'),
      resize: async nextWidth => { width = nextWidth; await act(async () => notifyResize()); },
      render,
      observed,
    });
  } finally {
    await act(async () => root.unmount());
    container.remove();
    dom.window.HTMLElement.prototype.getBoundingClientRect = originalRect;
    delete globalThis.ResizeObserver;
  }
  assert.equal(disconnected, true);
}

test('empty, single, double and multiple tag lists display every tag when they fit', async () => {
  for (const tags of [[], ['A'], ['A', 'B'], ['A', 'B', 'C']]) {
    await withTags(tags, 84, ({ visible, overflow }) => {
      assert.deepEqual(visible(), tags);
      assert.equal(overflow(), null);
    });
  }
});

test('two tags reserve room for the overflow indicator and expose all text', async () => {
  const tags = ['Source: SwarmSkillHub', 'Team Skills'];
  await withTags(tags, 210, ({ visible, overflow }) => {
    assert.deepEqual(visible(), [tags[0]]);
    assert.equal(overflow().title, tags.join(' · '));
    assert.equal(overflow().getAttribute('aria-label'), tags.join(' · '));
  });
});

test('multiple tags retain the longest fitting prefix', async () => {
  await withTags(['A', 'B', 'C'], 80, ({ visible, overflow }) => {
    assert.deepEqual(visible(), ['A', 'B']);
    assert.ok(overflow());
  });
});

test('a single long tag and a first tag that cannot fit alongside overflow show only overflow', async () => {
  for (const tags of [['Long tag'], ['A', 'B'], ['A', 'B', 'C']]) {
    await withTags(tags, 49, ({ visible, overflow }) => {
      assert.deepEqual(visible(), []);
      assert.equal(overflow().title, tags.join(' · '));
    });
  }
});

test('exact-fit tags remain visible without reserving an unnecessary indicator', async () => {
  await withTags(['A', 'B'], 54, ({ visible, overflow }) => {
    assert.deepEqual(visible(), ['A', 'B']);
    assert.equal(overflow(), null);
  });
});

test('zero and very narrow widths recover after resizing for every tag count', async () => {
  for (const tags of [['A'], ['A', 'B'], ['A', 'B', 'C']]) {
    await withTags(tags, 0, async ({ visible, overflow, resize, observed }) => {
      assert.deepEqual(visible(), []);
      assert.ok(overflow());
      assert.equal(observed.size, 2);
      await resize(10);
      assert.deepEqual(visible(), []);
      await resize(200);
      assert.deepEqual(visible(), tags);
      assert.equal(overflow(), null);
      await resize(20);
      assert.deepEqual(visible(), []);
      assert.ok(overflow());
    });
  }
});

test('changing labels without changing their count remeasures the tag list', async () => {
  await withTags(['A', 'B'], 54, async ({ visible, overflow, render }) => {
    await render(['Long source', 'Team Skills']);
    assert.deepEqual(visible(), []);
    assert.equal(overflow().title, 'Long source · Team Skills');
    await render([]);
    assert.deepEqual(visible(), []);
    assert.equal(overflow(), null);
  });
});
