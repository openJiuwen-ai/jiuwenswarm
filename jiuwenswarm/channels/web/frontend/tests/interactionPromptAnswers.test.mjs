import assert from 'node:assert/strict';
import test, { after } from 'node:test';
import { act, createElement } from 'react';
import { createRoot } from 'react-dom/client';
import { Simulate } from 'react-dom/test-utils';
import { I18nextProvider } from 'react-i18next';
import { JSDOM } from 'jsdom';

const dom = new JSDOM('<div id="root"></div>', {
  url: 'https://interaction-prompt.invalid',
  pretendToBeVisual: true,
});
for (const [key, value] of Object.entries({
  window: dom.window,
  document: dom.window.document,
  navigator: dom.window.navigator,
  localStorage: dom.window.localStorage,
  HTMLElement: dom.window.HTMLElement,
  HTMLTextAreaElement: dom.window.HTMLTextAreaElement,
  Node: dom.window.Node,
  IS_REACT_ACT_ENVIRONMENT: true,
})) {
  Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
}
after(() => dom.window.close());

const { InteractionSlot } =
  await import('../node_modules/.cache/interaction-slot/components/InteractionSlot/index.mjs');
const { default: i18n } = await import('../node_modules/.cache/interaction-slot/i18n/index.mjs');
const { useChatStore } = await import('../node_modules/.cache/interaction-slot/stores/index.mjs');

const SESSION_ID = 'interaction-prompt-session';

async function renderSlot(pending, onSubmit = async () => true) {
  useChatStore.setState({
    activeSessionId: SESSION_ID,
    runtimes: {
      [SESSION_ID]: {
        messages: [],
        pendingQuestions: [pending],
      },
    },
  });
  const container = document.createElement('div');
  document.body.replaceChildren(container);
  const root = createRoot(container);
  await act(async () =>
    root.render(
      createElement(I18nextProvider, { i18n }, createElement(InteractionSlot, { onSubmit })),
    ),
  );
  return { root, container };
}

async function typeCustom(element, value) {
  await act(async () => {
    Simulate.change(element, { target: { value } });
  });
  assert.equal(element.value, value);
}

test('multi-select submits selected options together with the custom input', async () => {
  const submissions = [];
  const pending = {
    request_id: 'multi-select-1',
    source: 'ask_user_interrupt',
    questions: [
      {
        header: '团队安排',
        question: '选择本周参与成员',
        multi_select: true,
        options: [
          { label: '音乐', value: 'music' },
          { label: '美术', value: 'art' },
          { label: 'Other', value: 'Other' },
        ],
      },
    ],
  };
  const { root } = await renderSlot(pending, async (...args) => {
    submissions.push(args);
    return true;
  });
  try {
    const options = [...document.querySelectorAll('[data-testid="interaction-slot-ix-option"]')];
    assert.equal(options.length, 2);
    await act(async () => options[0].click());
    await act(async () => options[1].click());

    const custom = document.querySelector('[data-testid="interaction-slot-ix-custom-input"]');
    await typeCustom(custom, '每周三下午需要调整');

    const confirm = document.querySelector('[data-testid="interaction-slot-ix-confirm-button"]');
    await act(async () => confirm.click());

    const summary = useChatStore.getState().runtimes[SESSION_ID].messages[0]?.content ?? '';
    assert.match(summary, /每周三下午需要调整/);

    assert.deepEqual(submissions, [
      [
        'multi-select-1',
        [
          {
            question: '选择本周参与成员',
            selected_options: ['music', 'art'],
            custom_input: '每周三下午需要调整',
          },
        ],
        'ask_user_interrupt',
      ],
    ]);
  } finally {
    await act(async () => root.unmount());
  }
});

test('a new pending request id resets the page and previous answers', async () => {
  const first = {
    request_id: 'reset-1',
    source: 'ask_user_interrupt',
    questions: [
      {
        header: '第一页',
        question: '第一问',
        options: [{ label: 'A', value: 'a' }],
      },
      {
        header: '第二页',
        question: '第二问',
        options: [{ label: 'B', value: 'b' }],
      },
    ],
  };
  const second = {
    request_id: 'reset-2',
    source: 'ask_user_interrupt',
    questions: [
      {
        header: '新一轮',
        question: '新问题',
        options: [{ label: 'C', value: 'c' }],
      },
    ],
  };
  const { root } = await renderSlot(first);
  try {
    await act(async () =>
      document.querySelector('[data-testid="interaction-slot-ix-option"]').click(),
    );
    await act(async () =>
      document.querySelector('[data-testid="interaction-slot-ix-confirm-button"]').click(),
    );
    assert.equal(
      document.querySelector('[data-testid="interaction-slot-ix-title-text"]').textContent,
      '第二页',
    );

    await act(async () => {
      useChatStore.setState({
        runtimes: {
          [SESSION_ID]: {
            pendingQuestions: [second],
          },
        },
      });
    });

    assert.equal(
      document.querySelector('[data-testid="interaction-slot-ix-title-text"]').textContent,
      '新一轮',
    );
    assert.equal(document.querySelector('[data-testid="interaction-slot-ix-pager"]'), null);
    assert.equal(document.querySelector('.ix-option--selected'), null);
  } finally {
    await act(async () => root.unmount());
  }
});

test('selecting a predefined option after custom input submits only the option', async () => {
  const submissions = [];
  const pending = {
    request_id: 'single-select-custom-1',
    source: 'ask_user_interrupt',
    questions: [
      {
        header: '方案确认',
        question: '选择方案',
        options: [
          { label: '方案A', value: 'a' },
          { label: 'Other', value: 'Other' },
        ],
      },
    ],
  };
  const { root } = await renderSlot(pending, async (...args) => {
    submissions.push(args);
    return true;
  });
  try {
    await act(async () =>
      document.querySelector('[data-testid="interaction-slot-ix-option-custom"]').click(),
    );
    await typeCustom(
      document.querySelector('[data-testid="interaction-slot-ix-custom-input"]'),
      '残留的自定义补充',
    );
    await act(async () =>
      document.querySelector('[data-testid="interaction-slot-ix-option"]').click(),
    );
    await act(async () =>
      document.querySelector('[data-testid="interaction-slot-ix-confirm-button"]').click(),
    );

    assert.deepEqual(submissions, [
      [
        'single-select-custom-1',
        [{ question: '选择方案', selected_options: ['a'] }],
        'ask_user_interrupt',
      ],
    ]);
    assert.equal('custom_input' in submissions[0][1][0], false);
    const summary = useChatStore.getState().runtimes[SESSION_ID].messages[0]?.content ?? '';
    assert.match(summary, /"answers":\["a"\]/);
    assert.doesNotMatch(summary, /残留的自定义补充/);
  } finally {
    await act(async () => root.unmount());
  }
});
