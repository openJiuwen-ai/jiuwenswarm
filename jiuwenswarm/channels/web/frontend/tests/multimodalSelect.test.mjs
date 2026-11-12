import assert from 'node:assert/strict';
import test from 'node:test';
import { build } from 'esbuild';
import { JSDOM } from 'jsdom';

const dom = new JSDOM('<!doctype html><body><dialog open><div id="root"></div></dialog></body>', {
  url: 'http://localhost', pretendToBeVisual: true,
});
for (const name of ['window', 'document', 'navigator', 'Node', 'HTMLElement']) {
  Object.defineProperty(globalThis, name, { configurable: true, value: name === 'window' ? dom.window : dom.window[name] });
}
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
const { act, createElement, useState } = await import('react');
const { createRoot } = await import('react-dom/client');
await build({
  entryPoints: ['src/components/ui/Select/Select.tsx'], outfile: 'node_modules/.cache/multimodal-select.mjs',
  bundle: true, format: 'esm', platform: 'node', packages: 'external', loader: { '.css': 'empty' },
});
const { Select } = await import('../node_modules/.cache/multimodal-select.mjs');
const options = [{ value: 'vision', label: 'Images' }, { value: 'audio', label: 'Audio' }];
async function key(element, value) {
  await act(async () => element.dispatchEvent(new dom.window.KeyboardEvent('keydown', {
    key: value, bubbles: true, cancelable: true,
  })));
}

test('controlled multiple selection focuses only after positioning and supports keyboard toggle/escape', async () => {
  const root = createRoot(document.getElementById('root'));
  let selected;
  function Fixture() {
    const [value, setValue] = useState([]);
    selected = value;
    return createElement(Select, { multiple: true, value, options, 'aria-label': 'Capabilities',
      onChange: setValue, panelHeader: 'Select supported capabilities' });
  }
  try {
    await act(async () => root.render(createElement(Fixture)));
    const trigger = document.querySelector('[data-testid="ui-select-trigger"]');
    trigger.focus();
    await key(trigger, 'ArrowDown');
    const panel = document.querySelector('[role="listbox"]');
    assert.equal(panel.getAttribute('aria-multiselectable'), 'true');
    assert.equal(panel.style.visibility, 'visible');
    assert.equal(document.activeElement, panel);
    await key(panel, 'ArrowDown');
    assert.equal(document.activeElement.dataset.variant, 'vision');
    await key(document.activeElement, ' ');
    assert.deepEqual(selected, ['vision']);
    assert.equal(trigger.getAttribute('aria-expanded'), 'true');
    await key(document.activeElement, 'ArrowDown');
    await key(document.activeElement, 'Enter');
    assert.deepEqual(selected, ['vision', 'audio']);
    await key(document.activeElement, 'Escape');
    assert.equal(trigger.getAttribute('aria-expanded'), 'false');
    assert.equal(document.activeElement, trigger);
  } finally {
    await act(async () => root.unmount());
  }
});

test('single selection retains scalar callbacks and closes after an option click', async () => {
  const root = createRoot(document.getElementById('root'));
  const received = [];
  try {
    await act(async () => root.render(createElement(Select, { options, defaultValue: 'vision', onChange: (value) => received.push(value) })));
    const trigger = document.querySelector('[data-testid="ui-select-trigger"]');
    await act(async () => trigger.click());
    await act(async () => document.querySelector('[role="option"][data-variant="audio"]').click());
    assert.deepEqual(received, ['audio']);
    assert.equal(trigger.getAttribute('aria-expanded'), 'false');
    assert.equal(trigger.textContent, 'Audio');
  } finally {
    await act(async () => root.unmount());
  }
});
