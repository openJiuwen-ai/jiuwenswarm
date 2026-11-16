import assert from 'node:assert/strict';
import test from 'node:test';
import { JSDOM } from 'jsdom';

const dom = new JSDOM('<!doctype html><html><body></body></html>', { url: 'http://localhost/' });
for (const [key, value] of Object.entries({
  window: dom.window,
  document: dom.window.document,
  HTMLElement: dom.window.HTMLElement,
  Node: dom.window.Node,
  IS_REACT_ACT_ENVIRONMENT: true,
})) {
  Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
}
const { act, createElement } = await import('react');
const { createRoot } = await import('react-dom/client');
const { EditableCombobox } = await import('../node_modules/.cache/editable-combobox/EditableCombobox.mjs');

async function mount({ top, height = 600, count = 20 }) {
  window.innerHeight = height;
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  const changes = [];
  const options = Array.from({ length: count }, (_, index) => ({ value: `agent-${index}`, label: `Agent ${index}` }));
  await act(async () => root.render(createElement(EditableCombobox, {
    ariaLabel: 'Agent', options, value: 'agent-0', onChange: value => changes.push(value),
  })));
  const combobox = container.firstChild;
  combobox.getBoundingClientRect = () => ({ top, bottom: top + 32 });
  // jsdom has no layout engine; provide the measured sizes used by the component.
  Object.defineProperty(window.HTMLElement.prototype, 'scrollHeight', {
    configurable: true,
    get() { return this.getAttribute('role') === 'listbox' ? this.children.length * 30 + 8 : 0; },
  });
  await act(async () => container.querySelector('button').click());
  return {
    container, changes,
    get list() { return container.querySelector('[role="listbox"]'); },
    async close() {
      await act(async () => root.unmount());
      container.remove();
    },
  };
}

function measureList(list, { height = 90, padding = 0 } = {}) {
  Object.defineProperty(list, 'clientHeight', { value: height });
  Object.defineProperty(list, 'clientTop', { value: 1 });
  list.getBoundingClientRect = () => ({ top: 100, bottom: 102 + height });
  for (const [index, row] of Array.from(list.children).entries()) {
    row.getBoundingClientRect = () => {
      const top = 101 + padding + index * 30 - list.scrollTop;
      return { top, bottom: top + 30 };
    };
  }
}

test('opens upward near the viewport bottom and keeps every Agent selectable', async () => {
  const view = await mount({ top: 520 });
  try {
    assert.equal(view.list.dataset.side, 'top');
    assert.equal(view.list.style.maxHeight, '178px');
    assert.equal(view.list.children.length, 20);
    await act(async () => view.list.lastChild.click());
    assert.deepEqual(view.changes, ['agent-19']);
    assert.equal(view.list, null);
  } finally { await view.close(); }
});

test('opens downward when the full panel fits below', async () => {
  const view = await mount({ top: 80 });
  try { assert.equal(view.list.dataset.side, 'bottom'); }
  finally { await view.close(); }
});

test('caps the panel to the larger available side in a short viewport', async () => {
  const view = await mount({ top: 110, height: 240 });
  try {
    assert.equal(view.list.dataset.side, 'top');
    assert.equal(view.list.style.maxHeight, '98px');
  } finally { await view.close(); }
});

test('repositions on resize and ancestor scroll, then removes listeners on close', async () => {
  const view = await mount({ top: 300, height: 800 });
  try {
    assert.equal(view.list.dataset.side, 'bottom');
    await act(async () => {
      window.innerHeight = 400;
      window.dispatchEvent(new window.Event('resize'));
    });
    assert.equal(view.list.dataset.side, 'top');
    view.container.firstChild.getBoundingClientRect = () => ({ top: 40, bottom: 72 });
    await act(async () => view.container.dispatchEvent(new window.Event('scroll')));
    assert.equal(view.list.dataset.side, 'bottom');
    await act(async () => view.container.querySelector('button').click());
    assert.equal(view.list, null);
    window.dispatchEvent(new window.Event('resize'));
  } finally { await view.close(); }
});

test('keyboard navigation scrolls the active Agent into the list viewport', async () => {
  const view = await mount({ top: 520 });
  try {
    measureList(view.list);
    const input = view.container.querySelector('input');
    for (let index = 0; index < 10; index++) {
      await act(async () => input.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true })));
    }
    assert.equal(view.list.scrollTop, 240);
    await act(async () => input.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true })));
    assert.deepEqual(view.changes, ['agent-10']);
  } finally { await view.close(); }
});

test('keyboard scrolling respects panel borders and padding without moving its ancestor', async () => {
  const view = await mount({ top: 520 });
  try {
    measureList(view.list, { padding: 4 });
    view.container.scrollTop = 45;
    const input = view.container.querySelector('input');
    const press = async key => act(async () => input.dispatchEvent(
      new window.KeyboardEvent('keydown', { key, bubbles: true }),
    ));
    await press('ArrowDown');
    assert.equal(view.list.scrollTop, 0, 'a fully visible option needs no scrolling');
    for (let index = 1; index < 10; index++) await press('ArrowDown');
    assert.equal(view.list.scrollTop, 244);
    for (let index = 0; index < 10; index++) await press('ArrowUp');
    assert.equal(view.list.scrollTop, 4);
    assert.equal(view.container.scrollTop, 45);
    assert.deepEqual(view.changes, []);
    await press('Escape');
    assert.equal(view.list, null);
  } finally { await view.close(); }
});

test('filtering recalculates the panel height without changing the selected Agent', async () => {
  const view = await mount({ top: 420 });
  try {
    assert.equal(view.list.dataset.side, 'top');
    const input = view.container.querySelector('input');
    await act(async () => {
      Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set.call(input, 'Agent 19');
      input.dispatchEvent(new window.Event('input', { bubbles: true }));
    });
    assert.equal(view.list.children.length, 1);
    assert.equal(view.list.dataset.side, 'bottom');
    assert.deepEqual(view.changes, []);
  } finally { await view.close(); }
});
