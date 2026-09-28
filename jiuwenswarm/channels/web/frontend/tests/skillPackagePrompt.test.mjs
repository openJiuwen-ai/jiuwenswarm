import assert from 'node:assert/strict';
import test, { after } from 'node:test';
import { act, createElement } from 'react';
import { createRoot } from 'react-dom/client';
import { I18nextProvider } from 'react-i18next';
import { JSDOM } from 'jsdom';

const dom = new JSDOM('<div id="root"></div>', {
  url: 'https://skill-package.invalid',
  pretendToBeVisual: true,
});
for (const [key, value] of Object.entries({
  window: dom.window,
  document: dom.window.document,
  navigator: dom.window.navigator,
  localStorage: dom.window.localStorage,
  HTMLElement: dom.window.HTMLElement,
  Node: dom.window.Node,
  IS_REACT_ACT_ENVIRONMENT: true,
}))
  Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
after(() => dom.window.close());

const { InteractionSlot } =
  await import('../node_modules/.cache/skill-package-prompt/components/InteractionSlot/index.js');
const { classifyPrompt } =
  await import('../node_modules/.cache/skill-package-prompt/components/InteractionSlot/promptRouting.js');
const { default: i18n } = await import('../node_modules/.cache/skill-package-prompt/i18n/index.js');
const { useChatStore } = await import('../node_modules/.cache/skill-package-prompt/stores/index.js');

const question = {
  header: '发现可复用的技能包',
  question: '系统发现这套能力组合在类似任务中表现稳定。\n\n**技能包名称**\n研究组合',
  options: [
    { label: '创建技能包', value: 'install' },
    { label: '暂不创建', value: 'defer' },
  ],
  multi_select: false,
};
const pending = {
  request_id: 'symphony_experience_recipe-1_2',
  questions: [question],
  evolutionMeta: { rail_kind: 'symphony_experience' },
};

async function mounted(onSubmit, run, pendingQuestion = pending) {
  useChatStore.setState({
    activeSessionId: 'skill-package-session',
    runtimes: {
      'skill-package-session': {
        pendingQuestions: [pendingQuestion],
      },
    },
  });
  const container = document.createElement('div');
  document.body.replaceChildren(container);
  const root = createRoot(container);
  try {
    await act(async () =>
      root.render(
        createElement(I18nextProvider, { i18n }, createElement(InteractionSlot, { onSubmit })),
      ),
    );
    await run();
  } finally {
    await act(async () => root.unmount());
  }
}

test('routes Symphony experience candidates to the existing interaction prompt', () => {
  assert.equal(classifyPrompt(pending), 'experience');
  assert.equal(classifyPrompt({ ...pending, evolutionMeta: undefined }), 'experience');
  assert.equal(classifyPrompt({ request_id: 'ask-1', questions: [question] }), 'interaction');
  assert.equal(
    classifyPrompt({ request_id: 'plan-1', questions: [question], planApprovalKind: 'plan_approval' }),
    'legacy',
  );
  assert.equal(
    classifyPrompt({ request_id: 'permission-1', source: 'permission_interrupt', questions: [question] }),
    'authorization',
  );
});

test('renders the skill package prompt in the existing interaction window', async () => {
  const submissions = [];
  await mounted(
    async (...args) => {
      submissions.push(args);
      return true;
    },
    async () => {
      assert.equal(
        document.querySelector('[data-testid="interaction-slot-ix-title-text"]').textContent,
        '发现可复用的技能包',
      );
      assert.match(document.querySelector('[data-testid="interaction-slot-ix-body"]').textContent, /研究组合/);
      assert.doesNotMatch(document.body.textContent, /沉淀/);
      assert.notEqual(document.querySelector('[data-testid="interaction-slot-ix-prompt"]'), null);
      assert.equal(document.querySelector('[data-testid="interaction-slot-skill-package-prompt"]'), null);
      assert.equal(document.querySelector('[data-testid="interaction-slot-ix-options"]'), null);
      assert.equal(document.querySelector('[data-testid="interaction-slot-ix-cancel-button"]'), null);
      assert.equal(document.querySelector('[data-testid="interaction-slot-ix-skip-button"]'), null);
      assert.equal(document.querySelector('[data-testid="interaction-slot-ix-confirm-button"]'), null);
      assert.notEqual(document.querySelector('[data-testid="interaction-slot-skill-package-defer-button"]'), null);
      assert.notEqual(document.querySelector('[data-testid="interaction-slot-skill-package-create-button"]'), null);
      assert.deepEqual(submissions, []);
    },
  );
});

test('submits install directly from the interaction window', async () => {
  const submissions = [];
  await mounted(
    async (requestId, answers, source) => {
      submissions.push([requestId, answers, source]);
      return true;
    },
    async () => {
      await act(async () =>
        document.querySelector('[data-testid="interaction-slot-skill-package-create-button"]').click(),
      );
      assert.deepEqual(submissions, [
        [pending.request_id, [{ question: question.question, selected_options: ['install'] }], undefined],
      ]);
    },
  );
});

test('shows the creating state and blocks duplicate actions', async () => {
  let finish;
  const submitted = new Promise((resolve) => {
    finish = resolve;
  });
  await mounted(
    async () => {
      await submitted;
      return true;
    },
    async () => {
      const createButton = document.querySelector(
        '[data-testid="interaction-slot-skill-package-create-button"]',
      );
      const deferButton = document.querySelector(
        '[data-testid="interaction-slot-skill-package-defer-button"]',
      );
      await act(async () => createButton.click());
      assert.equal(createButton.textContent, '正在创建…');
      assert.equal(createButton.disabled, true);
      assert.equal(deferButton.disabled, true);
      await act(async () => finish());
    },
  );
});

test('submits defer directly from the interaction window', async () => {
  const submissions = [];
  await mounted(
    async (...args) => {
      submissions.push(args);
      return true;
    },
    async () => {
      await act(async () =>
        document.querySelector('[data-testid="interaction-slot-skill-package-defer-button"]').click(),
      );
      assert.deepEqual(submissions, [
        [pending.request_id, [{ question: question.question, selected_options: ['defer'] }], undefined],
      ]);
    },
  );
});

test('keeps ordinary user follow-up controls unchanged', async () => {
  const ordinary = {
    request_id: 'ask-1',
    source: 'ask_user_interrupt',
    questions: [
      {
        header: '日期确认',
        question: '是否明天出发？',
        options: [{ label: '是' }, { label: '否' }],
        multi_select: false,
      },
    ],
  };
  await mounted(
    async () => true,
    async () => {
      assert.notEqual(document.querySelector('[data-testid="interaction-slot-ix-options"]'), null);
      assert.notEqual(document.querySelector('[data-testid="interaction-slot-ix-cancel-button"]'), null);
      assert.notEqual(document.querySelector('[data-testid="interaction-slot-ix-skip-button"]'), null);
      assert.notEqual(document.querySelector('[data-testid="interaction-slot-ix-confirm-button"]'), null);
      assert.equal(document.querySelector('[data-testid="interaction-slot-skill-package-create-button"]'), null);
    },
    ordinary,
  );
});
