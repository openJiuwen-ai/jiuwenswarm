import assert from 'node:assert/strict';
import test, { after } from 'node:test';
import { act, createElement } from 'react';
import { createRoot } from 'react-dom/client';
import { I18nextProvider } from 'react-i18next';
import { JSDOM } from 'jsdom';

// Use the real HTTP transport, hook, stores and card; only the network is replaced.
const dom = new JSDOM('<!doctype html><div id="root"></div>', {
  url: 'https://skill-package-http.invalid', pretendToBeVisual: true,
});
dom.window.__JIUWEN_WEB_TRANSPORT__ = 'http';
let handleRequest;
const requests = [];
const jsonResponse = (data, status = 200) => new Response(JSON.stringify(data), {
  status, headers: { 'content-type': 'application/json' },
});
const successResponse = (data) => jsonResponse({ ok: true, data });
for (const [key, value] of Object.entries({
  window: dom.window, document: dom.window.document, navigator: dom.window.navigator,
  localStorage: dom.window.localStorage, HTMLElement: dom.window.HTMLElement, Node: dom.window.Node,
  CustomEvent: dom.window.CustomEvent, IS_REACT_ACT_ENVIRONMENT: true,
  requestAnimationFrame: dom.window.requestAnimationFrame.bind(dom.window),
  cancelAnimationFrame: dom.window.cancelAnimationFrame.bind(dom.window),
  fetch: async (input, init) => {
    const url = new URL(String(input), dom.window.location.href);
    assert.equal(url.hostname, 'skill-package-http.invalid');
    if (url.pathname === '/api/v1/connection/status') return successResponse({});
    const request = { url, init, body: init.body ? JSON.parse(init.body) : null };
    requests.push(request);
    assert.equal(typeof handleRequest, 'function', `Unexpected HTTP request: ${url.pathname}`);
    return handleRequest(request);
  },
  WebSocket: class { constructor() { throw new Error('HTTP tests must not open a WebSocket'); } },
})) Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
after(() => dom.window.close());

const { useWebSocket } = await import('../node_modules/.cache/permission-answer-transport/hooks/useWebSocket.js');
const { InteractionPrompt } = await import('../node_modules/.cache/permission-answer-transport/components/InteractionSlot/InteractionPrompt.js');
const { ToastStack } = await import('../node_modules/.cache/permission-answer-transport/components/ui/Toast/Toast.js');
const { toast } = await import('../node_modules/.cache/permission-answer-transport/components/ui/Toast/toastStore.js');
const { useChatStore, useSessionStore } = await import('../node_modules/.cache/permission-answer-transport/stores/index.js');
const { default: i18n } = await import('../node_modules/.cache/permission-answer-transport/i18n/index.js');
const { webClient } = await import('../node_modules/.cache/permission-answer-transport/services/webClient.js');
const sessionId = 'skill-package-http';
const question = {
  session_id: sessionId, request_id: 'symphony_experience_tool_http',
  source: 'skill_evolution_approval',
  evolution_meta: { rail_kind: 'symphony_experience', recipe_id: 'recipe-http', recipe_version: 1 },
  questions: [{
    header: '发现可复用的技能包', question: '**技能包名称**\nexcel-to-word-report',
    options: [{ label: '创建技能包', value: 'install' }, { label: '暂不创建', value: 'defer' }],
  }],
};
const answerParams = {
  session_id: sessionId, request_id: question.request_id,
  answers: [{ question: question.questions[0].question, selected_options: ['install'] }],
};
const installed = { accepted: true, resolved: true, installed: true };
const conflict = { accepted: false, resolved: false, installed: false, reason: 'skill_already_exists' };
const deferred = { accepted: true, resolved: true, installed: false, deferred: true };
const createButton = () => document.querySelector('[data-testid="interaction-slot-skill-package-create-button"]');
const deferButton = () => document.querySelector('[data-testid="interaction-slot-skill-package-defer-button"]');
const errorToast = () => document.querySelector('[data-testid="interaction-slot-skill-package-error"]:not(.ui-toast--closing)');

function responseGate() {
  let release;
  const promise = new Promise((resolve) => { release = resolve; });
  return { promise, release };
}

async function withClient(handler, run) {
  requests.length = 0;
  handleRequest = handler;
  try {
    await webClient.connect();
    await run();
  } finally {
    await webClient.disconnect();
    handleRequest = undefined;
  }
}

async function mounted(handler, run) {
  requests.length = 0;
  handleRequest = handler;
  await i18n.changeLanguage('zh');
  useChatStore.getState().ensureRuntime(sessionId);
  useChatStore.getState().setActiveSessionId(sessionId);
  useSessionStore.getState().ensureRuntime(sessionId);
  useSessionStore.getState().setMode(sessionId, 'agent');
  const observed = { results: [], errors: [] };
  function Host() {
    const api = useWebSocket({ activeSessionId: sessionId, onError: (error) => observed.errors.push(error) });
    const pending = useChatStore((state) => state.runtimes[sessionId]?.pendingQuestions[0]);
    return pending ? createElement(InteractionPrompt, {
      pending,
      onSubmit: async (...args) => {
        const result = await api.sendUserAnswer(sessionId, ...args);
        observed.results.push(result);
        return result;
      },
    }) : null;
  }
  const root = createRoot(document.getElementById('root'));
  try {
    await act(async () => root.render(createElement(I18nextProvider, { i18n }, createElement('div', null,
      createElement(Host), createElement(ToastStack)))));
    await act(async () => webClient.injectEvent({ type: 'event', event: 'chat.ask_user_question', payload: question }));
    assert.ok(createButton());
    await run(observed);
  } finally {
    await act(async () => root.unmount());
    toast.closeAll();
    await webClient.disconnect();
    useChatStore.getState().removeRuntime(sessionId);
    useSessionStore.getState().removeRuntime(sessionId);
    handleRequest = undefined;
  }
}

test('HTTP user-answer wait option reaches the query and waits for the unary result', async () => {
  const gate = responseGate();
  await withClient(() => gate.promise, async () => {
    let settled = false;
    const pending = webClient.request('chat.user_answer', answerParams, {
      awaitUserAnswerResult: true, timeoutMs: 120000,
    }).finally(() => { settled = true; });
    try {
      await new Promise((resolve) => setImmediate(resolve));
      assert.equal(settled, false);
      assert.equal(requests.length, 1);
      const { url, body, init } = requests[0];
      assert.equal(url.pathname, `/api/v1/chat/${sessionId}/actions/answer`);
      assert.equal(url.searchParams.get('wait_for_result'), 'true');
      assert.equal(init.method, 'POST');
      assert.equal(init.headers.Accept, 'application/json');
      assert.equal(body.request_id, answerParams.request_id);
      assert.deepEqual(body.answers, answerParams.answers);
      assert.equal(body.wait_for_result, undefined, 'transport option must not become an Agent answer field');
    } finally {
      gate.release(successResponse(installed));
      assert.deepEqual(await pending, installed);
    }
    assert.equal(webClient.getInflightCount(), 0);
  });
});

test('ordinary HTTP answers and other methods do not opt into final answer waiting', async () => {
  await withClient(() => successResponse({ accepted: true }), async () => {
    for (const [method, params, options] of [
      ['chat.user_answer', answerParams, undefined],
      ['chat.user_answer', answerParams, { awaitUserAnswerResult: false }],
      ['chat.interrupt', { session_id: sessionId, intent: 'cancel' }, { awaitUserAnswerResult: true }],
    ]) {
      assert.deepEqual(await webClient.request(method, params, options), { accepted: true });
      assert.equal(requests.at(-1).url.searchParams.has('wait_for_result'), false);
    }
  });
});

test('HTTP skill-package success closes the card only after the final result', async () => {
  const gate = responseGate();
  await mounted(() => gate.promise, async (observed) => {
    await act(async () => createButton().click());
    assert.deepEqual(observed.results, []);
    assert.equal(createButton().disabled, true);
    assert.equal(webClient.getInflightCount(), 1);
    await act(async () => gate.release(successResponse(installed)));
    assert.deepEqual(observed.results, [true]);
    assert.equal(createButton(), null);
    assert.equal(errorToast(), null);
    assert.equal(webClient.getInflightCount(), 0);
  });
});

test('HTTP conflict preserves the actionable card and successful deferral closes it', async () => {
  await mounted(({ body }) => successResponse(body.answers[0].selected_options[0] === 'defer' ? deferred : conflict),
    async (observed) => {
      await act(async () => createButton().click());
      assert.deepEqual(observed.results, [false]);
      assert.match(errorToast().textContent, /同名.*未覆盖/);
      assert.equal(createButton().disabled, false);
      assert.equal(deferButton().disabled, false);
      await act(async () => deferButton().click());
      assert.deepEqual(observed.results, [false, true]);
      assert.deepEqual(requests.at(-1).body.answers[0].selected_options, ['defer']);
      assert.equal(createButton(), null);
      assert.equal(webClient.getInflightCount(), 0);
    });
});

test('an older HTTP Gateway admission ACK remains unconfirmed and does not dismiss the card', async () => {
  await mounted(() => successResponse({ accepted: true, session_id: sessionId, request_id: question.request_id }),
    async (observed) => {
      await act(async () => createButton().click());
      assert.deepEqual(observed.results, [false]);
      assert.match(errorToast().textContent, /暂未确认.*检查技能列表/);
      assert.equal(createButton().disabled, false);
      assert.equal(deferButton().disabled, false);
      assert.equal(useChatStore.getState().getRuntime(sessionId).pendingQuestions.length, 1);
    });
});

test('HTTP failure envelopes retain their error identity and release the request', async () => {
  await withClient(({ init }) => jsonResponse({
    ok: false, request_id: init.headers['X-Request-Id'],
    error: { code: 'BAD_REQUEST', message: 'Answer rejected' },
  }, 400), async () => {
    await assert.rejects(webClient.request('chat.user_answer', answerParams, { awaitUserAnswerResult: true }),
      (error) => {
        assert.equal(error.code, 'BAD_REQUEST');
        assert.equal(error.message, 'Answer rejected');
        assert.equal(error.requestId, requests[0].init.headers['X-Request-Id']);
        return true;
      });
    assert.equal(webClient.getInflightCount(), 0);
  });
});

test('HTTP transport errors keep the skill-package card available', async () => {
  await mounted(() => { throw new TypeError('Connection lost'); }, async (observed) => {
    await act(async () => createButton().click());
    assert.deepEqual(observed.results, [false]);
    assert.match(errorToast().textContent, /暂未确认/);
    assert.equal(createButton().disabled, false);
    assert.equal(deferButton().disabled, false);
    assert.equal(webClient.getInflightCount(), 0);
  });
});

test('HTTP final-result timeout aborts the fetch and restores the card controls', async () => {
  const originalSetTimeout = window.setTimeout;
  let expireRequest;
  window.setTimeout = function (callback, delay, ...args) {
    const timer = originalSetTimeout.call(this, callback, delay, ...args);
    if (delay === 120000) expireRequest = () => { window.clearTimeout(timer); callback(...args); };
    return timer;
  };
  try {
    await mounted(({ init }) => new Promise((_resolve, reject) => {
      init.signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true });
    }), async (observed) => {
      await act(async () => createButton().click());
      assert.equal(createButton().disabled, true);
      assert.equal(typeof expireRequest, 'function');
      await act(async () => expireRequest());
      assert.equal(requests[0].init.signal.aborted, true);
      assert.deepEqual(observed.results, [false]);
      assert.match(errorToast().textContent, /暂未确认/);
      assert.equal(createButton().disabled, false);
      assert.equal(deferButton().disabled, false);
      assert.equal(webClient.getInflightCount(), 0);
    });
  } finally {
    window.setTimeout = originalSetTimeout;
  }
});
