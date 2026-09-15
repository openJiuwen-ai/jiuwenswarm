import assert from 'node:assert/strict';
import test, { after } from 'node:test';
import { act, createElement } from 'react';
import { createRoot } from 'react-dom/client';
import { I18nextProvider } from 'react-i18next';
import { JSDOM } from 'jsdom';

const dom = new JSDOM('<div id="root"></div>', {
  url: 'https://authorization-prompt.invalid',
});

for (const [key, value] of Object.entries({
  window: dom.window,
  document: dom.window.document,
  navigator: dom.window.navigator,
  HTMLElement: dom.window.HTMLElement,
  Node: dom.window.Node,
  IS_REACT_ACT_ENVIRONMENT: true,
})) {
  Object.defineProperty(globalThis, key, {
    configurable: true,
    writable: true,
    value,
  });
}

after(() => dom.window.close());

const { AuthorizationPrompt } = await import(
  '../node_modules/.cache/authorization-prompt-localization/components/InteractionSlot/AuthorizationPrompt.js'
);

const { default: i18n } = await import(
  '../node_modules/.cache/authorization-prompt-localization/i18n/index.js'
);

test('English authorization UI translates Chinese backend labels without changing answer values', async () => {
  const previousLanguage = i18n.resolvedLanguage;
  const answers = [];
  await i18n.changeLanguage('en');
  const root = createRoot(document.getElementById('root'));

  try {
    const pending = {
      request_id: 'authorization-test',
      source: 'permission_interrupt',
      questions: [{
        header: '权限审批: bash',
        question: 'write C:\\workspace\\hello.txt',
        options: [
          { value: 'allow_once', label: '本次允许', description: '仅本次授权执行' },
          { value: 'session_allow', label: '会话内记住', description: '本次会话内自动放行同类操作' },
          { value: 'always_allow', label: '永久记住', description: '写回磁盘，所有会话均自动放行' },
          { value: 'reject', label: '拒绝', description: '拒绝执行此工具' },
        ],
      }],
    };

    await act(async () => root.render(createElement(
      I18nextProvider,
      { i18n },
      createElement(AuthorizationPrompt, {
        pending,
        onSubmit: async (_requestId, selectedAnswers) => {
          answers.push(selectedAnswers);
          return true;
        },
      }),
    )));

    const prompt = document.querySelector('[data-testid="interaction-slot-auth-prompt"]');

    assert.equal(
      prompt.querySelector('[data-testid="interaction-slot-auth-title"]').textContent,
      'Authorization: bash',
    );

    assert.deepEqual(
      [...prompt.querySelectorAll('[data-testid="interaction-slot-auth-action-button"]')]
        .map((button) => button.textContent),
      ['Skip', 'Always allow', 'Remember for session', 'Allow once'],
    );

    await act(async () => prompt.querySelector('[data-variant="allow-once"]').click());

    assert.deepEqual(answers, [[{ selected_options: ['allow_once'] }]]);
  } finally {
    await act(async () => root.unmount());
    await i18n.changeLanguage(previousLanguage || 'zh');
  }
});

test('Chinese authorization UI preserves Chinese backend labels', async () => {
  const previousLanguage = i18n.resolvedLanguage;
  await i18n.changeLanguage('zh');
  const root = createRoot(document.getElementById('root'));

  try {
    const pending = {
      request_id: 'authorization-zh-test',
      source: 'permission_interrupt',
      questions: [{
        header: '权限审批: bash',
        question: 'write C:\\workspace\\hello.txt',
        options: [
          { value: 'allow_once', label: '本次允许', description: '仅本次授权执行' },
          { value: 'session_allow', label: '会话内记住', description: '本次会话内自动放行同类操作' },
          { value: 'always_allow', label: '永久记住', description: '写回磁盘，所有会话均自动放行' },
          { value: 'reject', label: '拒绝', description: '拒绝执行此工具' },
        ],
      }],
    };

    await act(async () => root.render(createElement(
      I18nextProvider,
      { i18n },
      createElement(AuthorizationPrompt, {
        pending,
        onSubmit: async () => true,
      }),
    )));

    const prompt = document.querySelector('[data-testid="interaction-slot-auth-prompt"]');

    assert.equal(
      prompt.querySelector('[data-testid="interaction-slot-auth-title"]').textContent,
      '权限审批: bash',
    );

    assert.deepEqual(
      [...prompt.querySelectorAll('[data-testid="interaction-slot-auth-action-button"]')]
        .map((button) => button.textContent),
      ['拒绝', '永久记住', '会话内记住', '本次允许'],
    );
  } finally {
    await act(async () => root.unmount());
    await i18n.changeLanguage(previousLanguage || 'zh');
  }
});

test('Unknown authorization actions preserve backend labels', async () => {
  const previousLanguage = i18n.resolvedLanguage;
  await i18n.changeLanguage('en');
  const root = createRoot(document.getElementById('root'));

  try {
    const pending = {
      request_id: 'authorization-unknown-action-test',
      source: 'permission_interrupt',
      questions: [{
        header: '权限审批: custom_tool',
        question: 'custom operation',
        options: [
          {
            value: 'custom_action',
            label: '自定义操作',
            description: '执行自定义操作',
          },
        ],
      }],
    };

    await act(async () => root.render(createElement(
      I18nextProvider,
      { i18n },
      createElement(AuthorizationPrompt, {
        pending,
        onSubmit: async () => true,
      }),
    )));

    const prompt = document.querySelector('[data-testid="interaction-slot-auth-prompt"]');

    assert.equal(
      prompt.querySelector('[data-testid="interaction-slot-auth-title"]').textContent,
      'Authorization: custom_tool',
    );

    assert.deepEqual(
      [...prompt.querySelectorAll('[data-testid="interaction-slot-auth-action-button"]')]
        .map((button) => button.textContent),
      ['自定义操作'],
    );
  } finally {
    await act(async () => root.unmount());
    await i18n.changeLanguage(previousLanguage || 'zh');
  }
});
