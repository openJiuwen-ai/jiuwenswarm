import assert from 'node:assert/strict';
import test, { after } from 'node:test';
import { act, createElement, createRef } from 'react';
import { createRoot } from 'react-dom/client';
import { I18nextProvider } from 'react-i18next';
import { JSDOM } from 'jsdom';

// Reserved, non-resolving DOM origin only; fetch rejects network access and the WebSocket fixture returns picker data.
const dom = new JSDOM('<!doctype html><div class="chat-panel-shell"><div id="root"></div></div>', {
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
    if (![
      'agent_groups.list', 'commands.list', 'skills.list', 'command.compact',
      'session.assets.list', 'session.assets.register',
    ].includes(request.method)) {
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
          {
            name: 'team-skill',
            display_name: '旅行规划',
            description: 'Team skill',
            skill_type: 'swarm_skill',
            installed: true,
          },
        ],
        plugins: [],
      };
    } else if (request.method === 'command.compact') {
      slashRequests.push(request);
      payload = { result: 'noop' };
    } else if (request.method === 'session.assets.list' || request.method === 'session.assets.register') {
      payload = { items: [] };
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
const { useChatStore, useSessionStore, useWorkspaceStore, usePlanStore, useTeamSelectorStore } =
  await import('../node_modules/.cache/input-area-permission-merge/stores/index.js');
const { default: i18n } = await import('../node_modules/.cache/input-area-permission-merge/i18n/index.js');
const { setTeamOrganizationUiEnabled } = await import('../node_modules/.cache/input-area-permission-merge/features/teamOrganization/featureConfig.js');
const { isMultiModelCollaborationEnabled, setMultiModelCollaborationEnabled } =
  await import('../node_modules/.cache/input-area-permission-merge/features/multiModelCollaboration/featureConfig.js');
const { webClient } = await import('../node_modules/.cache/input-area-permission-merge/services/webClient.js');

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

for (const language of ['zh', 'en']) {
  for (const desktop of [false, true]) {
    test(`${language}: upload tooltip shows limits for ${desktop ? 'desktop' : 'web'} without title`, async () => {
      if (desktop) window.pywebview = { api: { select_local_files: async () => [] } };
      try {
        await mount({ language }, async () => {
          await click(byId('chat-panel-input-attach-trigger'));
          const option = byId('chat-panel-input-attach-menu-file');
          option.getBoundingClientRect = () => new dom.window.DOMRect(200, 300, 160, 32);
          assert.equal(option.hasAttribute('title'), false);
          await act(async () => option.dispatchEvent(new dom.window.MouseEvent('mouseover', { bubbles: true })));
          const tooltip = document.querySelector('[role="tooltip"]');
          const lines = language === 'zh'
            ? ['图片：单张最大 10M', ...(!desktop ? ['文件：单个最大 100M'] : []), '每条消息最多 20 个附件']
            : ['Images: up to 10M each', ...(!desktop ? ['Files: up to 100M each'] : []), 'Up to 20 attachments per message'];
          assert.equal(tooltip.textContent, lines.join('\n'));
          assert.equal(tooltip.style.whiteSpace, 'pre-line');
          assert.equal(tooltip.style.left, '366px');
          await act(async () => option.dispatchEvent(new dom.window.MouseEvent('mouseout', { bubbles: true })));
          assert.equal(document.querySelector('[role="tooltip"]'), null);
          await act(async () => option.dispatchEvent(new dom.window.MouseEvent('mouseover', { bubbles: true })));
          assert.ok(document.querySelector('[role="tooltip"]'));
          // Keyboard/AX activation does not dispatch pointerdown; closing the menu must still hide its tooltip.
          await click(option);
          await flushMicrotasks();
          assert.equal(document.querySelector('[role="tooltip"]'), null);
          await click(byId('chat-panel-input-attach-trigger'));
          const reopenedOption = byId('chat-panel-input-attach-menu-file');
          await act(async () => {
            reopenedOption.dispatchEvent(new dom.window.MouseEvent('mouseover', { bubbles: true }));
            reopenedOption.click();
          });
          await flushMicrotasks();
          assert.equal(document.querySelector('[role="tooltip"]'), null);
        });
      } finally {
        delete window.pywebview;
        delete window.__JIUWEN_DESKTOP__;
        delete window.__JIUWEN_DESKTOP_DND__;
      }
    });
  }
}

test('web plus opens browser picker directly without a server file dialog', async () => {
  await mount({}, async () => {
    let opened = 0;
    byId('chat-panel-input-file-input').click = () => { opened += 1; };
    await click(byId('chat-panel-input-attach-trigger'));
    await click(byId('chat-panel-input-attach-menu-file'));
    assert.equal(opened, 1);
  });
});

test('desktop plus accepts a document above 100 MiB as a path without reading its content', async () => {
  const pick = { kind: 'document', path: '/fixtures/large.txt', filename: 'large.txt', size: 101 * 1024 * 1024 };
  window.pywebview = { api: { select_local_files: async () => [pick] } };
  try {
    await mount({}, async ({ props, render }) => {
      const received = [];
      props.onPersistDocuments = async (_text, items) => {
        received.push(...items);
        return { media_items: items };
      };
      await render();
      await click(byId('chat-panel-input-attach-trigger'));
      await click(byId('chat-panel-input-attach-menu-file'));
      assert.equal(received.length, 1);
      assert.equal(received[0].path, pick.path);
      assert.equal(received[0].size_bytes, pick.size);
      assert.equal(received[0].base64Data, undefined);
      assert.equal(document.querySelectorAll('[data-testid="chat-panel-input-attachment-card"]').length, 1);
    });
  } finally {
    delete window.pywebview;
    delete window.__JIUWEN_DESKTOP__;
    delete window.__JIUWEN_DESKTOP_DND__;
  }
});

for (const [filename, mime, limit] of [['image.png', 'image/png', 10], ['document.txt', 'text/plain', 100]]) {
  for (const extraByte of [0, 1]) {
    test(`browser ${filename} ${extraByte ? 'rejects above' : 'accepts at'} ${limit} MiB`, async () => {
      await mount({}, async ({ props, render }) => {
        const received = [];
        const persist = async (_text, items) => {
          received.push(...items);
          return { media_items: items.map((item) => ({ ...item, path: `/uploads/${item.filename}` })) };
        };
        props.onPersistMedia = persist;
        props.onPersistDocuments = persist;
        await render();
        const file = new dom.window.File(['fixture'], filename, { type: mime });
        // Exercise File.size validation without allocating 100 MiB in each DOM test.
        // The backend transport check uses the full payload and compares stored bytes.
        Object.defineProperty(file, 'size', { value: limit * 1024 * 1024 + extraByte });
        const input = byId('chat-panel-input-file-input');
        Object.defineProperty(input, 'files', { value: [file] });
        await act(async () => {
          input.dispatchEvent(new dom.window.Event('change', { bubbles: true }));
          if (!extraByte) {
            for (let attempts = 0; received.length === 0 && attempts < 100; attempts += 1) {
              await new Promise((resolve) => setTimeout(resolve, 5));
            }
          }
        });
        assert.equal(received.length, extraByte ? 0 : 1);
        assert.equal(document.querySelectorAll('[data-testid="chat-panel-input-attachment-card"]').length, 1);
        if (extraByte) {
          assert.ok(document.body.textContent.includes(`${limit}.0 MB`));
          assert.ok(byId('chat-panel-input-attachment-card').textContent.includes(i18n.t('chat.uploadFailed')));
        }
      });
    });
  }
}

test('native image picks above 10 MiB are rejected before persist', async () => {
  await mount({}, async ({ props, render, inputAreaRef }) => {
    props.onPersistMedia = async () => assert.fail('oversized image must not upload');
    await render();
    await act(async () => inputAreaRef.current.appendLocalFilePicks([
      { kind: 'image', filename: 'large.png', size: 10 * 1024 * 1024 + 1, base64: 'dGVzdA==' },
    ]));
    assert.equal(document.querySelectorAll('[data-testid="chat-panel-input-attachment-card"]').length, 0);
    assert.ok(document.body.textContent.includes('10.0 MB'));
  });
});

let mountSequence = 0;
async function mount(
  { mode = 'agent', profile = 'default', language = 'en', sessionId = `input-permission-merge-${++mountSequence}` } = {},
  run,
) {
  useChatStore.getState().ensureRuntime(sessionId);
  useSessionStore.getState().ensureRuntime(sessionId);
  useSessionStore.getState().setMode(sessionId, mode);
  useChatStore.getState().ensureRuntime(sessionId);
  useChatStore.getState().setActiveSessionId(sessionId);
  const previousWorkspace = useWorkspaceStore.getState();
  const previousCollaboration = isMultiModelCollaborationEnabled();
  setMultiModelCollaborationEnabled(false);
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
    setMultiModelCollaborationEnabled(previousCollaboration);
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

const evolutionCommands = ['evolve', 'evolve_list', 'evolve_simplify', 'evolve_rebuild', 'evolve_rollback'];
for (const command of evolutionCommands) {
  for (const mode of ['agent', 'team']) {
    for (const method of ['click', 'Enter', 'Tab']) {
      test(`${mode}: /${command} completes a skill through ${method} and sends plain command text`, async () => {
        await mount({ mode }, async ({ props, render, sessionId }) => {
          const sent = [];
          props.onSubmit = (content) => sent.push(content);
          await render();
          const skill = mode === 'team' ? 'team-skill' : 'test-skill';
          // A full command without a space must show skills, not search for the command name.
          await typeSlash(`/${command}`);
          assert.ok(suggestionIds().includes(skill));
          await typeSlash(`/${command} `);
          assert.ok(suggestionIds().includes(skill));
          await typeSlash(`/${command} ${mode === 'team' ? 'Team' : 'compacting'}`);
          assert.deepEqual(suggestionIds(), [skill]);
          if (method === 'click') await click(byId('chat-panel-composer-suggestion-item', skill));
          else await pressKey(method);
          assert.equal(byId('chat-panel-input').textContent, `/${command} ${skill} `);
          assert.equal(document.querySelector('[data-testid="chat-panel-composer-suggestion"]'), null);
          assert.equal(byId('chat-panel-input').querySelector('[contenteditable="false"]'), null);
          assert.equal(useChatStore.getState().getRuntime(sessionId).inputValue, `/${command} ${skill} `);
          assert.deepEqual(sent, []);
          await click(byId('chat-panel-input-send'));
          assert.deepEqual(sent, [`/${command} ${skill}`]);
        });
      });
    }
  }
}

for (const command of evolutionCommands) {
  for (const mode of ['agent', 'team']) {
    test(`${mode}: evolution ${command} with no candidates releases Tab and sends on Enter`, async () => {
      await mount({ mode }, async ({ props, render }) => {
        const sent = [];
        props.onSubmit = (content) => sent.push(content);
        await render();
        // Cover both an unknown skill and an installed skill excluded by the mode filter.
        for (const name of ['missing', mode === 'team' ? 'test-skill' : 'team-skill']) {
          const text = `/${command} ${name}`;
          await typeSlash(text);
          assert.deepEqual(suggestionIds(), []);
          const beforeTab = sent.length;
          const event = new dom.window.KeyboardEvent('keydown', { key: 'Tab', bubbles: true, cancelable: true });
          await act(async () => byId('chat-panel-input').dispatchEvent(event));
          assert.equal(event.defaultPrevented, false);
          assert.equal(sent.length, beforeTab);
          assert.equal(byId('chat-panel-input').textContent, text);
          assert.equal(document.querySelector('[data-testid="chat-panel-composer-suggestion"]'), null);
          await typeSlash(text);
          await pressKey('Enter');
          assert.equal(sent.length, beforeTab + 1);
          assert.equal(sent.at(-1), text);
          assert.equal(byId('chat-panel-input').textContent, '');
        }
      });
    });
  }
}

test('evolution empty candidates preserve IME confirmation and Shift+Enter', async () => {
  await mount({ mode: 'team' }, async ({ props, render }) => {
    props.onSubmit = () => assert.fail('composition or Shift+Enter must not send');
    await render();
    const text = '/evolve_list missing';
    for (const options of [{ isComposing: true }, { keyCode: 229 }, { shiftKey: true }]) {
      await typeSlash(text);
      const event = new dom.window.KeyboardEvent('keydown', {
        key: 'Enter', bubbles: true, cancelable: true, ...options,
      });
      await act(async () => byId('chat-panel-input').dispatchEvent(event));
      assert.equal(event.defaultPrevented, false);
      assert.equal(byId('chat-panel-input').textContent, text);
      assert.notEqual(document.querySelector('[data-testid="chat-panel-composer-suggestion"]'), null);
    }
  });
});

test('evolution skill completion stops at later arguments and preserves them when editing the skill', async () => {
  await mount({ mode: 'team' }, async () => {
    for (const text of [
      '/evolve_simplify team-skill merge duplicate entries',
      '/evolve_rollback team-skill latest',
      '/evolve_list team-skill ',
    ]) {
      await typeSlash(text);
      assert.equal(document.querySelector('[data-testid="chat-panel-composer-suggestion"]'), null);
    }
    await typeSlash('/evolve_simplify tea-old merge duplicates');
    await act(async () => {
      const editor = byId('chat-panel-input');
      const range = document.createRange();
      range.setStart(editor.firstChild, '/evolve_simplify tea'.length);
      range.collapse(true);
      window.getSelection().removeAllRanges();
      window.getSelection().addRange(range);
      editor.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
    });
    assert.deepEqual(suggestionIds(), ['team-skill']);
    await pressKey('Tab');
    assert.equal(byId('chat-panel-input').textContent, '/evolve_simplify team-skill merge duplicates');
  });
});

test('evolution completion matches translated display names but inserts the directory name', async () => {
  await mount({ mode: 'team', language: 'zh' }, async () => {
    await typeSlash('/evolve_list 旅行');
    assert.deepEqual(suggestionIds(), ['team-skill']);
    await click(byId('chat-panel-composer-suggestion-item', 'team-skill'));
    assert.equal(byId('chat-panel-input').textContent, '/evolve_list team-skill ');
    assert.equal(document.querySelector('[data-testid="chat-panel-composer-suggestion"]'), null);
  });
});

test('unknown evolution skill query shows the skill empty state; ordinary slash search still works', async () => {
  await mount({ mode: 'team' }, async () => {
    await typeSlash('/evolve_list missing');
    assert.deepEqual(suggestionIds(), []);
    assert.equal(byId('chat-panel-composer-suggestion-empty').textContent, i18n.t('chat.slashPicker.noMatchingSkills'));
    await typeSlash('/Team');
    assert.deepEqual(suggestionIds(), ['team-skill']);
  });
});

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

test('persist session tag shows for real persist sessions; close hides it locally', async () => {
  await mount({}, async ({ render, sessionId }) => {
    await render();
    assert.equal(document.querySelector('[data-testid="chat-panel-persist-tag"]'), null);
    await act(async () => useSessionStore.getState().setPersistSession(sessionId, true));
    assert.notEqual(document.querySelector('[data-testid="chat-panel-persist-tag"]'), null);
    assert.equal(
      document.querySelector('[data-testid="chat-panel-persist-tag-label"]').textContent,
      i18n.t('persistSession.toolbarTag'),
    );
    await click(document.querySelector('[data-testid="chat-panel-persist-tag-close"]'));
    assert.equal(document.querySelector('[data-testid="chat-panel-persist-tag"]'), null);
    assert.equal(useSessionStore.getState().getRuntime(sessionId).persistSession, false);
  });
});

test('persist session tag never renders on the welcome page even when armed', async () => {
  await mount({ sessionId: 'new' }, async ({ render }) => {
    await render();
    await act(async () => useSessionStore.getState().setPersistSession('new', true));
    assert.equal(document.querySelector('[data-testid="chat-panel-persist-tag"]'), null);
  });
});

test('persist session tag drops its close button once the session has messages', async () => {
  await mount({}, async ({ render, sessionId }) => {
    await render();
    await act(async () => useSessionStore.getState().setPersistSession(sessionId, true));
    await act(async () =>
      useChatStore.getState().addMessage(sessionId, {
        id: 'persist-history-1',
        role: 'user',
        content: 'first task',
        timestamp: '2026-10-26T10:00:00.000Z',
      }),
    );
    const tag = document.querySelector('[data-testid="chat-panel-persist-tag"]');
    assert.notEqual(tag, null);
    assert.equal(document.querySelector('[data-testid="chat-panel-persist-tag-close"]'), null);
    assert.equal(tag.className.includes('chat-agent-tag--static'), true);
    assert.equal(useSessionStore.getState().getRuntime(sessionId).persistSession, true);
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
  test(`${mode}: persisted automatic profile is projected without saving during render`, async () => {
    await mount({ mode, profile: 'automatic' }, async ({ saved, sessionId }) => {
      const effective = mode === 'agent' ? 'automatic' : 'default';
      const trigger = byId('chat-panel-permission-selector-trigger');
      assert.equal(trigger.dataset.variant, effective);
      assert.match(trigger.textContent, new RegExp(i18n.t(`chat.config.permission.${effective}`)));
      await click(trigger);
      const options = [...document.querySelectorAll('[data-testid="chat-panel-permission-selector-option"]')];
      assert.deepEqual(
        options.map((option) => option.dataset.variant),
        mode === 'agent' ? ['default', 'automatic', 'full_access'] : ['default', 'full_access'],
      );
      assert.equal(byId('chat-panel-permission-selector-option', effective).getAttribute('aria-checked'), 'true');
      await click(byId('chat-panel-permission-selector-option', effective));
      assert.deepEqual(saved, []);
      await act(async () => useSessionStore.getState().setMode(sessionId, 'agent'));
      assert.equal(byId('chat-panel-permission-selector-trigger').dataset.variant, 'automatic');
      assert.deepEqual(saved, [], 'a mode projection must not overwrite the persisted automatic profile');
    });
  });
}

test('team hides the permission selector without overwriting the persisted profile', async () => {
  await mount({ mode: 'team', profile: 'automatic' }, async ({ saved, sessionId }) => {
    assert.equal(document.querySelector('[data-testid="chat-panel-permission-selector-trigger"]'), null);
    await act(async () => useSessionStore.getState().setMode(sessionId, 'agent'));
    assert.equal(byId('chat-panel-permission-selector-trigger').dataset.variant, 'automatic');
    assert.deepEqual(saved, []);
  });
});

test('actual composer follows organization Team identity; dissolution and disabling restore Owner view and ordinary sending', async () => {
  const groups = [
    { id: 'business-planning', displayName: '商业策划专家团', avatar: 'http://localhost/business.png' },
    { id: 'technology-insight', displayName: '技术洞察专家团', avatar: 'http://localhost/technology.png' },
    { id: 'software-architecture-design', displayName: '代码架构设计专家团', avatar: 'http://localhost/architecture.png' },
  ].map((group) => ({ ...group, name: group.id, installed: true, source: 'local', capabilities: { canUse: true } }));
  const teams = groups.map((group, index) => ({
    team_id: index === 0 ? 'owner' : `expert-${index}`, team_name: index === 0 ? 'owner' : `expert-${index}`,
    agent_group_name: group.id, organization_id: 'org', is_owner: index === 0, state: 'paused',
  }));
  let listedTeams = teams;
  const requests = [];
  const originalRequest = webClient.request;
  webClient.request = async (method, params) => {
    requests.push(method);
    if (method === 'agent_groups.list') return { agentGroups: groups };
    if (method === 'team.list') return { teams: listedTeams, default_team_id: 'owner' };
    if (method === 'team.snapshot') return { team_id: params.team_id, members: [], tasks: [] };
    if (method === 'commands.list') return { commands: [] };
    if (method === 'skills.list') return { skills: [], plugins: [] };
    if (method === 'session.assets.list') return { assets: [] };
    assert.fail(`Unexpected composer request: ${method}`);
  };
  let testedSession;
  try {
    await mount({ mode: 'team', language: 'zh' }, async ({ sessionId, props, render }) => {
      testedSession = sessionId;
      let identity;
      props.onAgentGroupIdentityChange = (value) => { identity = value; };
      await act(async () => useSessionStore.getState().setAgentGroupBinding(sessionId, 'business-planning'));
      await render();
      assert.equal(byId('chat-panel-agent-group-tag-label').textContent, '商业策划专家团');
      const ordinaryTag = byId('chat-panel-agent-group-tag').outerHTML;
      await act(async () => {
        useTeamSelectorStore.setState({ runtimes: {
          [sessionId]: { teams, selectedTeamId: 'owner', defaultTeamId: 'owner', loaded: true, loading: false },
        } });
        setTeamOrganizationUiEnabled(true);
      });
      for (const [index, team] of teams.entries()) {
        await click(byId('chat-panel-team-select-trigger'));
        await click(document.querySelector(`[data-team-id="${team.team_id}"]`));
        assert.equal(byId('chat-panel-agent-group-tag-label').textContent, groups[index].displayName);
        assert.equal(byId('chat-panel-agent-group-tag').querySelector('img').getAttribute('src'), groups[index].avatar);
        assert.equal(identity.displayName, groups[index].displayName);
        assert.equal(identity.avatarUrl, groups[index].avatar);
        assert.equal(useSessionStore.getState().runtimes[sessionId].agentGroupBinding, 'business-planning');
      }
      listedTeams = [{ ...teams[0], organization_id: null }];
      await act(async () => useTeamSelectorStore.getState().fetchTeams(sessionId));
      assert.equal(useTeamSelectorStore.getState().runtimes[sessionId].selectedTeamId, 'owner');
      assert.equal(byId('chat-panel-agent-group-tag').outerHTML, ordinaryTag);
      await click(byId('chat-panel-team-select-trigger'));
      assert.equal(document.querySelectorAll('[data-testid="chat-panel-team-select-option"]').length, 1);
      assert.equal(document.querySelector('[data-team-id="expert-1"]'), null);
      assert.equal(document.querySelector('[data-team-id="expert-2"]'), null);
      await click(document.querySelector('[data-team-id="owner"]'));
      await act(async () => setTeamOrganizationUiEnabled(false));
      assert.equal(document.querySelector('[data-testid="chat-panel-team-select"]'), null);
      assert.equal(byId('chat-panel-agent-group-tag').outerHTML, ordinaryTag);
      assert.equal(identity.displayName, '商业策划专家团');
      const orgRequestCount = requests.filter((method) => method.startsWith('team.')).length;
      const sent = [];
      props.onSubmit = (text) => sent.push(text);
      await render();
      await typeInput('普通团队消息');
      await click(byId('chat-panel-input-send'));
      assert.deepEqual(sent, ['普通团队消息']);
      assert.equal(requests.filter((method) => method.startsWith('team.')).length, orgRequestCount);
    });
  } finally {
    setTeamOrganizationUiEnabled(false);
    if (testedSession) useTeamSelectorStore.getState().removeRuntime(testedSession);
    webClient.request = originalRequest;
  }
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

test('automatic selection sends the profile contract and reflects the persisted prop', async () => {
  await mount({}, async ({ saved, props, render }) => {
    await click(byId('chat-panel-permission-selector-trigger'));
    await click(byId('chat-panel-permission-selector-option', 'automatic'));
    assert.deepEqual(saved, [{ permissions_profile: 'automatic' }]);
    assert.equal(document.querySelector('[data-testid="chat-panel-perm-warning-modal"]'), null);
    props.permissionProfile = 'automatic';
    await render();
    assert.equal(byId('chat-panel-permission-selector-trigger').dataset.variant, 'automatic');
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

test('design landing plus menu only offers file attachment', async () => {
  await mount({ sessionId: 'new' }, async () => {
    await act(async () => {
      useWorkspaceStore.setState({ workMode: 'design' });
    });
    await click(byId('chat-panel-input-attach-trigger'));
    assert.ok(document.querySelector('[data-testid="chat-panel-input-attach-menu-file"]'));
    for (const id of ['agent', 'skill', 'extension', 'design', 'plan', 'swarmflow', 'goal', 'personal-context']) {
      assert.equal(
        document.querySelector(`[data-testid="chat-panel-input-attach-menu-${id}"]`),
        null,
        `${id} stays hidden on the design landing`,
      );
    }
  });
});

test('work landing plus menu still offers more than file attachment', async () => {
  await mount({ sessionId: 'new' }, async () => {
    await click(byId('chat-panel-input-attach-trigger'));
    assert.ok(document.querySelector('[data-testid="chat-panel-input-attach-menu-file"]'));
    assert.ok(document.querySelector('[data-testid="chat-panel-input-attach-menu-agent"]'));
  });
});

for (const workMode of ['design', 'work', 'code']) {
  for (const enabled of [false, true]) {
    test(`${workMode} composer respects its routing with a model group and collaboration=${enabled}`, async () => {
      await mount({ sessionId: 'new' }, async ({ props, render, sessionId }) => {
        const sent = [];
        const selection = { type: 'model_group', id: 'saved-group' };
        props.onSubmit = (...args) => sent.push(args);
        await act(async () => {
          useWorkspaceStore.setState({ workMode });
          useSessionStore.getState().setSelectedModelSelection(sessionId, selection);
          setMultiModelCollaborationEnabled(enabled);
        });
        await render();
        await typeInput('send this draft');
        const blocked = workMode !== 'design' && !enabled;
        assert.equal(byId('chat-panel-input-send').disabled, blocked);
        await click(byId('chat-panel-input-send'));
        if (blocked) {
          await act(async () => byId('chat-panel-input').dispatchEvent(
            new dom.window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }),
          ));
          assert.deepEqual(sent, []);
          assert.equal(byId('chat-panel-input').textContent, 'send this draft');
        } else {
          assert.equal(sent.length, 1);
          assert.equal(sent[0][0], 'send this draft');
        }
        assert.deepEqual(useSessionStore.getState().getRuntime(sessionId).selectedModelSelection, selection);
      });
    });
  }
}

test('design landing with a saved model group hides stop and refuses a follow-up prompt', async () => {
  await mount({ sessionId: 'new' }, async ({ props, render, sessionId }) => {
    const cancelled = [];
    const sent = [];
    props.onCancel = () => cancelled.push('cancel');
    props.onSubmit = (...args) => sent.push(args);
    props.onInterrupt = () => sent.push('interrupt');
    props.isProcessing = true;
    await act(async () => {
      useWorkspaceStore.setState({ workMode: 'design' });
      useSessionStore.getState().setSelectedModelSelection(sessionId, { type: 'model_group', id: 'saved-group' });
      useChatStore.getState().setProcessing(sessionId, true);
    });
    await render();

    const editor = byId('chat-panel-input');
    const send = byId('chat-panel-input-send');
    assert.equal(send.dataset.variant, 'submitting');
    assert.equal(send.disabled, true);
    assert.equal(editor.getAttribute('contenteditable'), 'false');
    assert.equal(editor.getAttribute('aria-disabled'), 'true');
    assert.equal(editor.getAttribute('data-placeholder'), i18n.t('chat.placeholder'));
    assert.notEqual(editor.getAttribute('data-placeholder'), i18n.t('chat.placeholderProcessingQueue'));
    assert.equal(byId('chat-panel-input-attach-trigger').disabled, true);

    editor.textContent = 'send another prompt';
    await act(async () => {
      editor.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
      editor.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
    });
    await act(async () => {
      send.click();
    });
    assert.deepEqual(cancelled, []);
    assert.deepEqual(sent, []);
    assert.deepEqual(useChatStore.getState().getRuntime(sessionId).taskQueue, []);
  });
});

test('work landing still offers stop and a follow-up queue while processing', async () => {
  await mount({ sessionId: 'new' }, async ({ props, render, sessionId }) => {
    props.isProcessing = true;
    await act(async () => {
      useChatStore.getState().setProcessing(sessionId, true);
    });
    await render();
    const editor = byId('chat-panel-input');
    assert.equal(byId('chat-panel-input-send').dataset.variant, 'stop');
    assert.equal(editor.getAttribute('data-placeholder'), i18n.t('chat.placeholderProcessingQueue'));
    assert.equal(editor.getAttribute('contenteditable'), 'true');
  });
});


test('paused Owner retains its pause state while organization work exposes stop independently', async () => {
  const { startExpertRequest, finishExpertRequest } = await import(
    '../node_modules/.cache/input-area-permission-merge/features/teamOrganization/processing.js'
  );
  await mount({ mode: 'team' }, async ({ sessionId, props, render }) => {
    const actions = [];
    props.onCancel = () => actions.push('cancel');
    props.onInterrupt = (text) => actions.push(text);
    props.isProcessing = true;
    try {
      await act(async () => {
        useChatStore.getState().setPaused(sessionId, true);
        setTeamOrganizationUiEnabled(true);
        startExpertRequest(`${sessionId}::expert`, 'background', 'org_expert_background');
      });
      await render();
      assert.equal(useChatStore.getState().getRuntime(sessionId).isPaused, true);
      assert.equal(byId('chat-panel-input-send').getAttribute('data-variant'), 'stop');
      assert.equal(byId('chat-panel-input').getAttribute('data-placeholder'), i18n.t('chat.placeholderTeamMode'));
      await click(byId('chat-panel-input-send'));
      assert.deepEqual(actions, ['cancel']);
      assert.equal(useChatStore.getState().getRuntime(sessionId).isPaused, true);
      await act(async () => finishExpertRequest(`${sessionId}::expert`, 'background'));
      await render();
      assert.equal(byId('chat-panel-input-send').getAttribute('data-variant'), 'send');
    } finally {
      await act(async () => {
        finishExpertRequest(`${sessionId}::expert`, 'background');
        setTeamOrganizationUiEnabled(false);
      });
    }
  });
});
