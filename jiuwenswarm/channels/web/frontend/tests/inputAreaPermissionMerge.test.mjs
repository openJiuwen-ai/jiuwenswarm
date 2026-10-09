import assert from 'node:assert/strict';
import test, { after } from 'node:test';
import { act, createElement, createRef } from 'react';
import { createRoot } from 'react-dom/client';
import { I18nextProvider } from 'react-i18next';
import { JSDOM } from 'jsdom';

// Reserved, non-resolving DOM origin only; fetch rejects network access and the WebSocket fixture returns picker data.
const dom = new JSDOM('<!doctype html><div id="root" class="chat-panel-shell"></div>', {
  url: 'https://input-area.invalid',
  pretendToBeVisual: true,
});
const slashRequests = [];
let completeCompact;
class MockWebSocket {
  static OPEN = 1;
  static CLOSED = 3;

  constructor(url) {
    this.url = url;
    this.readyState = 0;
    queueMicrotask(() => {
      this.readyState = MockWebSocket.OPEN;
      this.onopen?.();
    });
  }

  send(rawMessage) {
    const request = JSON.parse(rawMessage);
    if (!['agent_groups.list', 'commands.list', 'skills.list', 'command.compact'].includes(request.method)) {
      throw new Error(`Unexpected WebSocket request: ${request.method}`);
    }
    let payload = {
      agentGroups: [
        {
          id: 'group-1',
          name: 'group-1',
          displayName: '可选专家团',
          installed: true,
          source: 'local',
          capabilities: { canUse: true },
        },
      ],
    };
    if (request.method === 'commands.list') {
      payload = {
        commands: ['fork', 'compact', 'plan', 'goal', 'persist'].map((name) => ({
          name,
          description: name,
          takesArgs: ['goal', 'persist'].includes(name),
        })),
      };
    } else if (request.method === 'skills.list') {
      payload = {
        skills: [
          { name: 'test-skill', description: 'Test skill for compacting text', skill_type: 'skill', installed: true },
          { name: 'team-skill', description: 'Team skill', skill_type: 'swarm_skill', installed: true },
        ],
        plugins: [],
      };
    } else if (request.method === 'command.compact') {
      slashRequests.push(request);
      payload = { result: 'noop' };
    }
    const respond = () => {
      this.onmessage?.({
        data: JSON.stringify({ type: 'res', id: request.id, ok: true, payload }),
      });
    };
    if (request.method === 'command.compact') completeCompact = respond;
    else queueMicrotask(respond);
  }

  close() {
    this.readyState = MockWebSocket.CLOSED;
    this.onclose?.({ code: 1000, reason: 'test', wasClean: true });
  }
}

const globals = {
  window: dom.window,
  document: dom.window.document,
  navigator: dom.window.navigator,
  localStorage: dom.window.localStorage,
  Node: dom.window.Node,
  NodeFilter: dom.window.NodeFilter,
  HTMLElement: dom.window.HTMLElement,
  MutationObserver: dom.window.MutationObserver,
  CustomEvent: dom.window.CustomEvent,
  FileReader: dom.window.FileReader,
  getComputedStyle: dom.window.getComputedStyle.bind(dom.window),
  requestAnimationFrame: dom.window.requestAnimationFrame.bind(dom.window),
  cancelAnimationFrame: dom.window.cancelAnimationFrame.bind(dom.window),
  IS_REACT_ACT_ENVIRONMENT: true,
  ResizeObserver: class {
    observe() {}
    unobserve() {}
    disconnect() {}
  },
  fetch: () => {
    throw new Error('InputArea permission interactions must not perform HTTP requests');
  },
  WebSocket: MockWebSocket,
};
const descriptors = new Map(Object.keys(globals).map((key) => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
for (const [key, value] of Object.entries(globals)) {
  Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
}
after(() => {
  dom.window.close();
  for (const [key, descriptor] of descriptors) {
    if (descriptor) Object.defineProperty(globalThis, key, descriptor);
    else delete globalThis[key];
  }
});

const { InputArea } =
  await import('../node_modules/.cache/input-area-permission-merge/components/ChatPanel/InputArea.js');
const { useChatStore, useSessionStore, useWorkspaceStore, usePlanStore } =
  await import('../node_modules/.cache/input-area-permission-merge/stores/index.js');
const { default: i18n } = await import('../node_modules/.cache/input-area-permission-merge/i18n/index.js');

function byId(id, variant) {
  const selector = `[data-testid="${id}"]${variant === undefined ? '' : `[data-variant="${variant}"]`}`;
  const matches = document.querySelectorAll(selector);
  assert.equal(matches.length, 1, `${selector} must identify one actual InputArea element`);
  return matches[0];
}
const click = async (element) => act(async () => element.click());
const typeInput = async (value) => act(async () => {
  const editor = byId('chat-panel-input');
  editor.textContent = value;
  editor.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
});

for (const queuePaused of [false]) {
  test(`busy composer queues by default even when queuePaused=${queuePaused}`, async () => {
    await mount({}, async ({ props, render, sessionId }) => {
      const sent = [];
      props.isProcessing = true;
      props.onSubmit = (...args) => sent.push(args);
      props.onInterrupt = () => assert.fail('ordinary busy input must not interrupt');
      await act(async () => {
        useChatStore.getState().setProcessing(sessionId, true);
        useChatStore.getState().setQueuePaused(sessionId, queuePaused);
      });
      await render();
      await typeInput('queued from actual composer');
      await click(byId('chat-panel-input-send'));
      assert.deepEqual(sent, []);
      const queue = useChatStore.getState().getRuntime(sessionId).taskQueue;
      assert.equal(queue.length, 1);
      assert.equal(queue[0].content, 'queued from actual composer');
      assert.equal(queue[0].status, 'queued');
    });
  });
}

test('idle composer still submits normally', async () => {
  await mount({}, async ({ props, render, sessionId }) => {
    const sent = [];
    props.onSubmit = (text) => sent.push(text);
    await render();
    await typeInput('ordinary input');
    await click(byId('chat-panel-input-send'));
    assert.deepEqual(sent, ['ordinary input']);
    assert.deepEqual(useChatStore.getState().getRuntime(sessionId).taskQueue, []);
  });
});

const flushMicrotasks = async () =>
  act(async () => {
    for (let index = 0; index < 4; index += 1) await Promise.resolve();
  });

async function mount(
  { mode = 'agent', profile = 'default', language = 'en', sessionId = 'input-permission-merge' } = {},
  run,
) {
  useChatStore.getState().ensureRuntime(sessionId);
  useSessionStore.getState().ensureRuntime(sessionId);
  useSessionStore.getState().setMode(sessionId, mode);
  useChatStore.getState().ensureRuntime(sessionId);
  useChatStore.getState().setActiveSessionId(sessionId);
  const previousWorkspace = useWorkspaceStore.getState();
  useWorkspaceStore.setState({ workMode: 'work', projects: [], selectedProject: null });
  await i18n.changeLanguage(language);
  const saved = [];
  const switched = [];
  const inputAreaRef = createRef();
  const props = {
    onSubmit() {},
    onInterrupt() {},
    onCancel() {},
    onPersistMedia: async (_content, mediaItems) => ({
      media_items: mediaItems.map((item) => ({ ...item, path: `C:/test/${item.filename}` })),
    }),
    onPersistDocuments: async () => ({}),
    onSwitchMode: (next) => {
      switched.push(next);
      useSessionStore.getState().setMode(sessionId, next);
    },
    isProcessing: false,
    permissionProfile: profile,
    onSavePermission: async (update) => {
      saved.push(update);
    },
  };
  const root = createRoot(document.getElementById('root'));
  const render = async () =>
    act(async () => root.render(createElement(I18nextProvider, { i18n }, createElement(InputArea, { ...props, ref: inputAreaRef }))));
  const unmountInputArea = async () => act(async () => root.render(null));
  try {
    await render();
    await run({ saved, switched, props, render, unmountInputArea, sessionId, inputAreaRef });
  } finally {
    await act(async () => {
      for (const button of document.querySelectorAll('[data-testid="chat-panel-input-attachment-remove"]')) {
        button.click();
      }
    });
    await act(async () => root.unmount());
    useChatStore.getState().setActiveSessionId(null);
    useChatStore.getState().removeRuntime(sessionId);
    useSessionStore.getState().removeRuntime(sessionId);
    usePlanStore.getState().removeRuntime(sessionId);
    useWorkspaceStore.setState(previousWorkspace, true);
  }
}

test('pasted image in a historical session restores after InputArea unmounts on the new conversation', async () => {
  await mount({}, async ({ inputAreaRef, sessionId, render, unmountInputArea }) => {
    await act(async () => {
      inputAreaRef.current.appendLocalFilePicks([
        {
          kind: 'image',
          filename: 'clipboard-image.png',
          mime_type: 'image/png',
          size: 4,
          base64: 'dGVzdA==',
        },
      ]);
    });
    assert.notEqual(document.querySelector('[data-testid="chat-panel-input-attachment-card"]'), null);

    const newConversationId = 'new';
    useSessionStore.getState().ensureRuntime(newConversationId);
    useChatStore.getState().ensureRuntime(newConversationId);
    await act(async () => useChatStore.getState().setActiveSessionId(newConversationId));

    assert.equal(document.querySelector('[data-testid="chat-panel-input-attachment-card"]'), null);
    await unmountInputArea();

    await act(async () => useChatStore.getState().setActiveSessionId(sessionId));
    await render();
    assert.notEqual(document.querySelector('[data-testid="chat-panel-input-attachment-card"]'), null);

    useChatStore.getState().removeRuntime(newConversationId);
    useSessionStore.getState().removeRuntime(newConversationId);
  });
});

async function typeSlash(value) {
  await act(async () => {
    const editor = byId('chat-panel-input');
    editor.textContent = value;
    editor.focus();
    const range = document.createRange();
    range.selectNodeContents(editor);
    range.collapse(false);
    window.getSelection().removeAllRanges();
    window.getSelection().addRange(range);
    editor.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
  });
  await flushMicrotasks();
}

const pressKey = async (key) =>
  act(async () => {
    byId('chat-panel-input').dispatchEvent(new dom.window.KeyboardEvent('keydown', { key, bubbles: true }));
  });
const suggestionIds = () =>
  Array.from(
    document.querySelectorAll('[data-testid="chat-panel-composer-suggestion-item"]'),
    (item) => item.dataset.variant,
  );

test('welcome command menu updates on session and mode changes without hiding skills', async () => {
  await mount({ sessionId: 'new' }, async () => {
    await typeSlash('/');
    assert.deepEqual(suggestionIds(), ['plan', 'goal', 'persist', 'test-skill']);
    const realSession = 'slash-real-session';
    try {
      await act(async () => {
        useChatStore.getState().ensureRuntime(realSession);
        useSessionStore.getState().ensureRuntime(realSession);
        useChatStore.getState().setActiveSessionId(realSession);
      });
      await typeSlash('/');
      assert.deepEqual(suggestionIds(), ['fork', 'compact', 'plan', 'goal', 'persist', 'test-skill']);
      await act(async () => useSessionStore.getState().setMode(realSession, 'team'));
      assert.deepEqual(suggestionIds(), ['team-skill']);
    } finally {
      await act(async () => useChatStore.getState().setActiveSessionId('new'));
      useChatStore.getState().removeRuntime(realSession);
      useSessionStore.getState().removeRuntime(realSession);
    }
  });
});

for (const language of ['zh', 'en']) {
  for (const command of ['compact', 'fork']) {
    test(`${language}: welcome /${command} is blocked through Enter and send, preserving input and attachments`, async () => {
      await mount({ sessionId: 'new', language }, async ({ props, render, inputAreaRef, sessionId }) => {
        props.onSubmit = () => assert.fail('blocked commands must not submit chat messages');
        props.onForkSession = () => assert.fail('blocked commands must not fork');
        await render();
        await act(async () =>
          inputAreaRef.current.appendLocalFilePicks([
            { kind: 'image', filename: 'draft.png', mime_type: 'image/png', size: 4, base64: 'dGVzdA==' },
          ]),
        );
        await typeSlash(`/${command}`);
        assert.deepEqual(suggestionIds(), command === 'compact' ? ['test-skill'] : []);
        for (const submit of [() => pressKey('Enter'), () => click(byId('chat-panel-input-send'))]) {
          await submit();
          assert.equal(byId('chat-panel-input').textContent, `/${command}`);
          assert.equal(useChatStore.getState().getRuntime(sessionId).inputValue, `/${command}`);
          assert.equal(useChatStore.getState().getRuntime(sessionId).messages.length, 0);
          assert.deepEqual(useSessionStore.getState().getRuntime(sessionId).selectedSkills, []);
          assert.equal(useChatStore.getState().activeSessionId, 'new');
          assert.notEqual(document.querySelector('[data-testid="chat-panel-input-attachment-card"]'), null);
          const alerts = document.querySelectorAll('[data-testid="chat-panel-input-local-alert"]');
          assert.equal(alerts[alerts.length - 1].textContent, i18n.t('chat.commandRequiresSession'));
        }
      });
    });
  }
}

test('welcome Tab and unknown slash Enter do not send input; /plan still executes from the menu', async () => {
  await mount({ sessionId: 'new' }, async ({ props, render }) => {
    props.onSubmit = () => assert.fail('menu completion must not submit chat messages');
    await render();
    await typeSlash('/fork');
    await pressKey('Tab');
    assert.equal(byId('chat-panel-input').textContent, '/fork');
    assert.equal(document.querySelector('[data-testid="chat-panel-input-local-alert"]'), null);
    await typeSlash('/unknown-command');
    await pressKey('Enter');
    assert.equal(byId('chat-panel-input').textContent, '/unknown-command');
    await typeSlash('/plan');
    await pressKey('Enter');
    assert.notEqual(document.querySelector('[data-testid="chat-panel-plan-tag"]'), null);
    assert.equal(useChatStore.getState().getRuntime('new').messages.length, 0);
  });
});

test('real session still forks and prevents overlapping compact requests', async () => {
  await mount({}, async ({ props, render, sessionId }) => {
    const forked = [];
    props.onForkSession = async (sid) => forked.push(sid);
    await render();
    await typeSlash('/fork');
    await pressKey('Enter');
    assert.deepEqual(forked, [sessionId]);
    const previousRequests = slashRequests.length;
    try {
      await typeSlash('/compact');
      await pressKey('Enter');
      await typeSlash('/compact');
      await pressKey('Enter');
      assert.equal(slashRequests.length, previousRequests + 1);
      assert.equal(slashRequests.at(-1).params.session_id, sessionId);
    } finally {
      await act(async () => completeCompact?.());
      completeCompact = undefined;
    }
  });
});

for (const language of ['zh', 'en']) {
  test(`${language}: mode tooltip uses option DOMRect and already translated text`, async () => {
    await mount({ language }, async ({ switched }) => {
      await click(byId('chat-panel-mode-select-trigger'));
      const option = byId('chat-panel-mode-select-option', 'team');
      option.getBoundingClientRect = () => new dom.window.DOMRect(210, 320, 140, 46);
      await act(async () => option.dispatchEvent(new dom.window.MouseEvent('mouseover', { bubbles: true })));
      const tooltip = byId('chat-panel-mode-select-tooltip');
      assert.equal(tooltip.classList.contains('adaptive-tooltip'), true);
      assert.equal(tooltip.textContent, i18n.t('chat.config.mode.clusterDesc'));
      assert.notEqual(tooltip.textContent, 'chat.config.mode.clusterDesc');
      assert.equal(tooltip.style.position, 'fixed');
      assert.equal(tooltip.style.top, '326px');
      assert.equal(tooltip.style.left, '361px');
      await click(option);
      assert.deepEqual(switched, ['team']);
      assert.equal(document.querySelector('[data-testid="chat-panel-mode-select-tooltip"]'), null);
    });
  });
}

for (const mode of ['agent', 'auto_harness']) {
  test(`${mode}: persisted default profile is displayed without saving during render`, async () => {
    await mount({ mode, profile: 'default' }, async ({ saved, sessionId }) => {
      const effective = 'default';
      const trigger = byId('chat-panel-permission-selector-trigger');
      assert.equal(trigger.dataset.variant, effective);
      assert.match(trigger.textContent, new RegExp(i18n.t(`chat.config.permission.${effective}`)));
      await click(trigger);
      const options = [...document.querySelectorAll('[data-testid="chat-panel-permission-selector-option"]')];
      assert.deepEqual(
        options.map((option) => option.dataset.variant),
        ['default', 'full_access'],
      );
      assert.equal(byId('chat-panel-permission-selector-option', effective).getAttribute('aria-checked'), 'true');
      await click(byId('chat-panel-permission-selector-option', effective));
      assert.deepEqual(saved, []);
      await act(async () => useSessionStore.getState().setMode(sessionId, 'agent'));
      assert.equal(byId('chat-panel-permission-selector-trigger').dataset.variant, 'default');
      assert.deepEqual(saved, [], 'switching modes must not overwrite the persisted profile');
    });
  });
}

test('team hides the permission selector without overwriting the persisted profile', async () => {
  await mount({ mode: 'team', profile: 'default' }, async ({ saved, sessionId }) => {
    assert.equal(document.querySelector('[data-testid="chat-panel-permission-selector-trigger"]'), null);
    await act(async () => useSessionStore.getState().setMode(sessionId, 'agent'));
    assert.equal(byId('chat-panel-permission-selector-trigger').dataset.variant, 'default');
    assert.deepEqual(saved, []);
  });
});

test('team skills and Expert Teams are mutually exclusive', async () => {
  await mount({ mode: 'team', sessionId: 'new' }, async ({ sessionId }) => {
    await act(async () => useSessionStore.getState().addSelectedSkill(sessionId, 'team-skill-1'));
    await click(byId('chat-panel-input-attach-trigger'));
    await click(byId('chat-panel-input-attach-menu-agent'));
    await flushMicrotasks();

    const groupItem = byId('chat-panel-agent-group-picker-item', 'group-1');
    assert.equal(groupItem.getAttribute('aria-disabled'), 'true');
    assert.equal(groupItem.classList.contains('is-locked'), true);
    assert.equal(groupItem.getAttribute('data-tooltip'), i18n.t('chat.teamSkillsGroupLocked'));

    await click(groupItem);
    assert.deepEqual(useSessionStore.getState().runtimes[sessionId].agentGroupSelectionIntent, { kind: 'keep' });

    await act(async () => useSessionStore.getState().removeSelectedSkill(sessionId, 'team-skill-1'));
    assert.equal(groupItem.getAttribute('aria-disabled'), 'false');

    await act(async () =>
      useSessionStore.getState().setAgentGroupSelectionIntent(sessionId, { kind: 'select', id: 'group-1' }),
    );
    await act(async () => useSessionStore.getState().addSelectedSkill(sessionId, 'team-skill-2'));
    assert.deepEqual(useSessionStore.getState().runtimes[sessionId].selectedSkills, []);
  });
});

test('default selection sends the profile contract and reflects the persisted prop', async () => {
  await mount({ profile: 'full_access' }, async ({ saved, props, render }) => {
    await click(byId('chat-panel-permission-selector-trigger'));
    await click(byId('chat-panel-permission-selector-option', 'default'));
    assert.deepEqual(saved, [{ permissions_profile: 'default' }]);
    assert.equal(document.querySelector('[data-testid="chat-panel-perm-warning-modal"]'), null);
    props.permissionProfile = 'default';
    await render();
    assert.equal(byId('chat-panel-permission-selector-trigger').dataset.variant, 'default');
  });
});

for (const action of ['cancel', 'confirm']) {
  test(`full access ${action} preserves the warning flow`, async () => {
    await mount({}, async ({ saved }) => {
      await click(byId('chat-panel-permission-selector-trigger'));
      await click(byId('chat-panel-permission-selector-option', 'full_access'));
      assert.deepEqual(saved, []);
      assert.equal(
        byId('chat-panel-perm-warning-title').textContent,
        i18n.t('chat.config.permission.fullAccessWarning.title'),
      );
      await click(byId(`chat-panel-perm-warning-${action}`));
      assert.deepEqual(saved, action === 'confirm' ? [{ permissions_profile: 'full_access' }] : []);
      assert.equal(document.querySelector('[data-testid="chat-panel-perm-warning-modal"]'), null);
    });
  });
}
test('composer exposes Full-duplex only when idle with no text or attachments', async () => {
  await mount({}, async ({ sessionId, props, render }) => {
    props.onPersistDocuments = async (_content, items) => ({
      media_items: items.map((item) => ({ ...item, path: '/workspace/note.txt' })),
    });
    await render();
    assert.ok(byId('test-duplex-action'));
    await act(async () => useChatStore.getState().setInputValue(sessionId, 'read this file'));
    assert.equal(!!document.querySelector('[data-testid="test-duplex-action"]'), false, 'text hides voice action');
    assert.ok(byId('chat-panel-input-send'));
    await act(async () => useChatStore.getState().setInputValue(sessionId, ''));
    assert.ok(byId('test-duplex-action'));
    props.isProcessing = true;
    await render();
    assert.equal(!!document.querySelector('[data-testid="test-duplex-action"]'), false, 'processing hides voice action');
    props.isProcessing = false;
    await render();
    const input = byId('chat-panel-input-file-input');
    Object.defineProperty(input, 'files', {
      configurable: true,
      value: [new dom.window.File(['test'], 'note.txt', { type: 'text/plain' })],
    });
    await act(async () => input.dispatchEvent(new dom.window.Event('change', { bubbles: true })));
    assert.ok(byId('chat-panel-input-attachment-card'));
    assert.equal(!!document.querySelector('[data-testid="test-duplex-action"]'), false, 'attachment hides voice action');
    assert.ok(byId('chat-panel-input-send'));
  });
});
