import assert from 'node:assert/strict';
import test from 'node:test';
import { build } from 'esbuild';
import { JSDOM } from 'jsdom';

// The DOM has to exist before React is imported: react-dom caches whether the
// environment supports DOM events at module load, and without that cache the
// input-change fallback path throws under jsdom.
const dom = new JSDOM('<!doctype html><body></body>', { url: 'http://localhost:5173' });
globalThis.window = dom.window;
globalThis.document = dom.window.document;
globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const { act, createElement } = await import('react');
const { createRoot } = await import('react-dom/client');

// Render the real ModelNameField with a minimal UI boundary, so the assertions
// see the same events the settings dialog produces.
await build({
  entryPoints: ['src/features/settings/modules/models/ModelNameField.tsx'],
  outfile: 'node_modules/.cache/model-name-field.mjs',
  bundle: true,
  platform: 'node',
  format: 'esm',
  packages: 'external',
  loader: { '.css': 'empty' },
  plugins: [
    {
      name: 'model-name-field-boundaries',
      setup(builder) {
        const mocks = {
          '../../../../components/ui': `
            import React from 'react';
            export const Input = (props) =>
              React.createElement('input', {
                id: props.id,
                value: props.value,
                disabled: props.disabled,
                placeholder: props.placeholder,
                'data-testid': props['data-testid'],
                'data-variant': props['data-variant'],
                onChange: (event) => props.onChange(event.target.value),
                onBlur: props.onBlur,
              });
          `,
          'react-i18next': 'export const useTranslation = () => ({ t: (key) => key });',
        };
        builder.onResolve({ filter: /.*/ }, (args) =>
          mocks[args.path] ? { path: args.path, namespace: 'mock' } : undefined,
        );
        builder.onLoad({ filter: /.*/, namespace: 'mock' }, (args) => ({
          contents: mocks[args.path],
          loader: 'jsx',
          resolveDir: process.cwd(),
        }));
      },
    },
  ],
});
const { ModelNameField } = await import('../node_modules/.cache/model-name-field.mjs');

const setValue = Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, 'value').set;

async function mount(props, run) {
  const container = dom.window.document.createElement('div');
  dom.window.document.body.appendChild(container);
  const root = createRoot(container);
  const changes = [];
  act(() =>
    root.render(
      createElement(ModelNameField, {
        id: 'model-name',
        mode: 'combo',
        disabled: false,
        invalid: false,
        fetchStatus: '',
        fetching: false,
        showRefresh: false,
        fetchDisabled: false,
        emptyText: 'empty',
        onOpen: () => {},
        onFetch: () => {},
        onChange: (value) => changes.push(value),
        onBlur: () => {},
        ...props,
      }),
    ),
  );
  const input = () => container.querySelector('[data-testid="settings-model-name-field-input"]');
  // The list renders through a portal onto the body.
  const menu = () => dom.window.document.querySelector('[data-testid="settings-model-name-field-listbox"]');
  const focus = () => act(() => input().dispatchEvent(new dom.window.FocusEvent('focusin', { bubbles: true })));
  const blur = () => act(() => input().dispatchEvent(new dom.window.FocusEvent('focusout', { bubbles: true })));
  const type = (text) =>
    act(() => {
      const element = input();
      setValue.call(element, text);
      element.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
    });
  const clickOutside = () =>
    act(() => {
      dom.window.document.body.dispatchEvent(new dom.window.Event('pointerdown', { bubbles: true }));
    });
  try {
    await run({ input, menu, focus, blur, type, clickOutside, changes });
  } finally {
    act(() => root.unmount());
    container.remove();
  }
}

test('a typed custom model name is committed when the list closes on an outside click', async () => {
  await mount({ value: 'my-model', allowCustomValue: true, options: [] }, async (ui) => {
    ui.focus();
    assert.ok(ui.menu(), 'focus opens the model list');
    ui.type('manual-replacement');
    ui.clickOutside();
    assert.deepEqual(ui.changes, ['manual-replacement']);
    assert.equal(ui.menu(), null, 'the list closes');
    // The same click also blurs the input; the name must not be committed twice.
    ui.blur();
    assert.deepEqual(ui.changes, ['manual-replacement']);
    assert.equal(ui.input().value, 'manual-replacement', 'the input keeps what was typed');
  });
});

test('a typed custom model name is committed on blur', async () => {
  await mount({ value: 'my-model', allowCustomValue: true, options: [] }, async (ui) => {
    ui.focus();
    ui.type('manual-replacement');
    ui.blur();
    assert.deepEqual(ui.changes, ['manual-replacement']);
  });
});

test('a fixed option list keeps typed text as a search filter, never as the model name', async () => {
  await mount(
    { value: 'qwen-default', allowCustomValue: false, options: ['qwen-default', 'qwen-next'] },
    async (ui) => {
      ui.focus();
      ui.type('manual-replacement');
      ui.clickOutside();
      assert.deepEqual(ui.changes, [], 'typing must not overwrite the selected model');
    },
  );
});

test('clearing a typed custom model name commits an empty value for validation', async () => {
  await mount({ value: 'my-model', allowCustomValue: true, options: [] }, async (ui) => {
    ui.focus();
    ui.type('my-model');
    ui.type('');
    ui.blur();
    assert.deepEqual(ui.changes, [''], 'an explicit clear must reach form validation');
  });
});

test('opening the list without editing never commits an empty value', async () => {
  await mount({ value: 'my-model', allowCustomValue: true, options: [] }, async (ui) => {
    ui.focus();
    ui.clickOutside();
    assert.deepEqual(ui.changes, [], 'opening the menu is not an edit');
  });
});
