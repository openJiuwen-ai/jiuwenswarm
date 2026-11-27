import assert from 'node:assert/strict';
import test from 'node:test';
import { build } from 'esbuild';
import { JSDOM } from 'jsdom';
import React, { act } from 'react';

import i18n from 'i18next';
import { initReactI18next } from 'react-i18next';

// jsdom 必须在 react-dom 首次加载前挂到全局，否则 React 的事件系统不挂文档监听器。
const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost/chat' });
Object.assign(globalThis, {
  window: dom.window,
  document: dom.window.document,
  localStorage: dom.window.localStorage,
  sessionStorage: dom.window.sessionStorage,
  // EditableCombobox 用 instanceof HTMLElement 判断滚动目标；jsdom 只挂了 window 上的构造函数。
  HTMLElement: dom.window.HTMLElement,
  IS_REACT_ACT_ENVIRONMENT: true,
});
const { createRoot } = await import('react-dom/client');

await i18n.use(initReactI18next).init({
  lng: 'en',
  resources: {
    en: {
      translation: {
        sessionSidebar: {
          enterpriseContext: {
            user: 'User',
            userContext: 'User context',
            agentNameLabel: 'Agent name',
            agentNameHint: 'Agent name',
            switching: 'Switching...',
            logout: 'Log out',
            noMatch: 'No matches',
            pleaseSelect: 'Select',
          },
        },
      },
    },
  },
  interpolation: { escapeValue: false },
});

await build({
  stdin: {
    contents: [
      "export { EditableCombobox } from './src/components/SessionSidebar/EditableCombobox';",
      "export { EnterpriseContextSwitcher } from './src/components/SessionSidebar/EnterpriseContextSwitcher';",
      "export { resolveEditableComboboxCommit, resolveEditableComboboxStrictCommit } from './src/components/SessionSidebar/editableComboboxModel';",
      "export { EnterpriseContext } from './src/services/enterpriseContext';",
    ].join('\n'),
    resolveDir: process.cwd(),
    loader: 'tsx',
  },
  bundle: true,
  format: 'esm',
  platform: 'node',
  jsx: 'automatic',
  external: ['react', 'react/jsx-runtime', 'react-i18next'],
  define: { 'import.meta.env': '{}' },
  outfile: 'node_modules/.cache/enterprise-agent-context/agentContext.mjs',
});

const {
  EditableCombobox,
  EnterpriseContextSwitcher,
  EnterpriseContext,
  resolveEditableComboboxCommit,
  resolveEditableComboboxStrictCommit,
} = await import('../node_modules/.cache/enterprise-agent-context/agentContext.mjs');

async function render(element) {
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  await act(async () => {
    root.render(element);
  });
  return {
    async unmount() {
      await act(() => root.unmount());
      container.remove();
    },
  };
}

function setInputValue(input, value) {
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
  setter.call(input, value);
  input.dispatchEvent(new window.Event('input', { bubbles: true }));
}

function focusOut(input) {
  input.dispatchEvent(new window.Event('focusout', { bubbles: true }));
}

const options = [
  { value: 'a', label: 'Agent A' },
  { value: 'b', label: 'Agent B' },
];

test('non-strict commit keeps free text', () => {
  assert.equal(resolveEditableComboboxCommit(options, 'free text'), 'free text');
});

test('strict commit only accepts an existing option', () => {
  assert.equal(resolveEditableComboboxStrictCommit(options, 'free text'), null);
  assert.equal(resolveEditableComboboxStrictCommit(options, ' Agent A '), 'a');
  assert.equal(resolveEditableComboboxStrictCommit(options, 'b'), 'b');
});

test('strict combobox reverts free input on blur and does not notify', async () => {
  const calls = [];
  const view = await render(
    React.createElement(EditableCombobox, {
      ariaLabel: 'Agent name',
      options,
      value: 'b',
      strictSelection: true,
      onChange: value => calls.push(value),
    }),
  );
  try {
    const input = document.querySelector('input[role="combobox"]');
    await act(async () => {
      setInputValue(input, 'free text');
    });
    await act(async () => {
      focusOut(input);
    });
    assert.deepEqual(calls, []);
    assert.equal(input.value, 'Agent B');
  } finally {
    await view.unmount();
  }
});

test('strict combobox commits an exact label', async () => {
  const calls = [];
  const view = await render(
    React.createElement(EditableCombobox, {
      ariaLabel: 'Agent name',
      options,
      value: 'b',
      strictSelection: true,
      onChange: value => calls.push(value),
    }),
  );
  try {
    const input = document.querySelector('input[role="combobox"]');
    await act(async () => {
      setInputValue(input, 'Agent A');
    });
    await act(async () => {
      focusOut(input);
    });
    assert.deepEqual(calls, ['a']);
  } finally {
    await view.unmount();
  }
});

test('context popover has no custom mode and shows the agent name hint', async () => {
  const selected = {
    bot_id: 'bot-1',
    group_id: 'group-1',
    user_id: 'u',
    jiuwenclaw_id: 'cluster-1',
    jiuwenclaw_name: 'Cluster',
    agent_name: 'agent_svc-1_agent-1',
    agent_id: 'svc-1_agent-1',
    runtime_template_id: 'svc-1',
    agent_template_id: 'agent-1',
    group_name: 'Group',
  };
  const value = {
    user: { user_id: 'u', display_name: 'User U' },
    contexts: [selected],
    selected,
    contextError: '',
    contextSwitching: false,
    onContextChange: () => {},
    onLogout: () => {},
  };
  const view = await render(
    React.createElement(
      EnterpriseContext.Provider,
      { value },
      React.createElement(EnterpriseContextSwitcher),
    ),
  );
  try {
    const button = document.querySelector('[data-testid="session-sidebar-enterprise-context-button"]');
    await act(async () => {
      button.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
    });
    const description = document.querySelector('[data-testid="session-sidebar-enterprise-agent-name-hint"]');
    assert.equal(description.textContent, 'Agent name');
    assert.ok(document.querySelector('[data-testid="session-sidebar-editable-combobox"]'));
    assert.doesNotMatch(document.body.textContent, /Custom/);
    assert.doesNotMatch(document.body.textContent, /Select agent/);
  } finally {
    await view.unmount();
  }
});
