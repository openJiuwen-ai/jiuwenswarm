import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { act, createElement } from 'react';
import { createRoot } from 'react-dom/client';
import i18next from 'i18next';
import { I18nextProvider } from 'react-i18next';
import { JSDOM } from 'jsdom';
import { ContextUsageIndicator } from '../node_modules/.cache/context-usage/components/ChatPanel/ContextUsageIndicator.js';
import { useSessionStore } from '../node_modules/.cache/context-usage/stores/sessionStore.js';
import { useChatStore } from '../node_modules/.cache/context-usage/stores/chatStore.js';

const fixture = JSON.parse(readFileSync(new URL('./fixtures/contextUsage.v1.json', import.meta.url), 'utf8'));
const translations = Object.fromEntries(
  ['zh', 'en'].map((language) => [
    language,
    {
      translation: JSON.parse(
        readFileSync(new URL('../src/i18n/locales/' + language + '.json', import.meta.url), 'utf8'),
      ),
    },
  ]),
);

async function mount(language, run) {
  const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost', pretendToBeVisual: true });
  const globals = {
    window: dom.window,
    document: dom.window.document,
    Node: dom.window.Node,
    HTMLElement: dom.window.HTMLElement,
    IS_REACT_ACT_ENVIRONMENT: true,
    ResizeObserver: class {
      observe() {}
      disconnect() {}
    },
    fetch: () => {
      throw new Error('Context UI must never query the backend');
    },
  };
  const descriptors = new Map(
    Object.keys(globals).map((key) => [key, Object.getOwnPropertyDescriptor(globalThis, key)]),
  );
  for (const [key, value] of Object.entries(globals)) {
    Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
  }
  dom.window.requestAnimationFrame = (callback) => {
    callback();
    return 0;
  };
  const sessionId = fixture.product_session_id;
  useSessionStore.getState().ensureRuntime(sessionId);
  useChatStore.getState().setActiveSessionId(sessionId);
  const i18n = i18next.createInstance();
  await i18n.init({ lng: language, resources: translations, initImmediate: false, showSupportNotice: false });
  const root = createRoot(document.getElementById('root'));
  try {
    await act(async () => root.render(createElement(I18nextProvider, { i18n }, createElement(ContextUsageIndicator))));
    const receive = async (payload) => act(async () => useSessionStore.getState().receiveContextUsage(payload));
    const click = async (element) => act(async () => element.click());
    await run({ receive, click, sessionId, dom });
  } finally {
    await act(async () => root.unmount());
    useSessionStore.getState().removeRuntime(sessionId);
    useSessionStore.getState().removeRuntime('other-session');
    useChatStore.getState().setActiveSessionId(null);
    for (const [key, descriptor] of descriptors) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else delete globalThis[key];
    }
    dom.window.close();
  }
}

for (const language of ['zh', 'en']) {
  test(language + ': localizes mapped parts and displays every unmapped backend key verbatim', async () => {
    await mount(language, async ({ receive, click }) => {
      const markupKey = '<img src=x onerror=alert(1)>';
      const longKey = 'backend_category_'.repeat(15);
      const unknownKeys = [
        'attachments',
        '__proto__',
        'constructor',
        'toString',
        'chat.contextUsage.tools',
        markupKey,
        longKey,
      ];
      const unknownParts = Object.fromEntries(
        unknownKeys.map((key, index) => [
          key,
          { category: key, tokens: index * 10, percentage_of_window: index === 0 ? null : 0.123 },
        ]),
      );
      const payload = structuredClone(fixture);
      payload.parts = { ...unknownParts, messages: fixture.parts.messages, tools: fixture.parts.tools };
      await receive(payload);
      await click(document.querySelector('.context-usage-trigger'));

      const toolsLabel = translations[language].translation.chat.contextUsage.tools;
      const rows = Array.from(document.querySelectorAll('.context-usage-category'));
      assert.deepEqual(
        rows.map((row) => row.querySelector('.context-usage-category__label').textContent),
        [toolsLabel, language === 'zh' ? '对话消息' : 'Conversation messages', ...unknownKeys],
      );
      assert.equal(
        rows[0].querySelector('.context-usage-category__dot').style.backgroundColor,
        'var(--color-context-tools)',
      );
      assert.equal(
        rows[1].querySelector('.context-usage-category__dot').style.backgroundColor,
        'var(--color-context-messages)',
      );
      for (const [index, row] of rows.slice(2).entries()) {
        assert.equal(row.querySelector('strong').textContent, String(index * 10));
        assert.equal(
          row.querySelector('.context-usage-category__dot').style.backgroundColor,
          'var(--color-text-secondary)',
        );
      }
      const segments = Array.from(document.querySelectorAll('.context-usage-breakdown__segment'));
      assert.equal(segments.length, rows.length - 1);
      for (const segment of segments.slice(2)) {
        assert.equal(segment.style.width, '12.3%');
        assert.equal(segment.style.backgroundColor, 'var(--color-text-secondary)');
      }
      assert.equal(document.querySelector('[role="dialog"] img'), null);
      assert.match(document.querySelector('.context-usage-detail__metric').textContent, /50%/);

      payload.parts = { attachments: { category: 'attachments', tokens: 7, percentage_of_window: 0.125 } };
      await receive(payload);
      assert.equal(document.querySelectorAll('.context-usage-category').length, 1);
      assert.equal(document.querySelector('.context-usage-category__label').textContent, 'attachments');
      assert.equal(document.querySelector('.context-usage-category strong').textContent, '7');
      assert.equal(document.querySelector('.context-usage-breakdown__segment').style.width, '12.5%');
    });
  });

  test(language + ': live snapshot updates tooltip and open details without a request', async () => {
    await mount(language, async ({ receive, click }) => {
      assert.equal(document.querySelector('.context-usage-trigger'), null);
      const initial = structuredClone(fixture);
      initial.session_kv_cache_hit_rate = 0.8829792874980116;
      initial.kv_cache.request.hit_rate = 0.9976856905811974;
      initial.kv_cache.session.weighted_hit_rate = 0.25;
      await receive(initial);
      const trigger = document.querySelector('.context-usage-trigger');
      assert.match(trigger.getAttribute('aria-label'), /50%/);
      await act(async () => trigger.focus());
      const tooltip = document.querySelector('[role="tooltip"]');
      assert.match(tooltip.textContent, /50%/);
      assert.match(tooltip.textContent, /88\.3%/);
      assert.doesNotMatch(tooltip.textContent, /99\.8%|25%|最近一次|last call/);
      assert.equal(
        tooltip.querySelector('.context-usage-tooltip__row:last-child span').textContent,
        language === 'zh' ? 'KV命中率' : 'KV hit rate',
      );
      await click(trigger);
      assert.equal(document.querySelector('[role="tooltip"]'), null);
      assert.equal(document.querySelectorAll('.context-usage-category').length, 4);
      assert.equal(document.querySelectorAll('.context-usage-breakdown__segment').length, 4);
      assert.match(document.querySelector('[role="dialog"]').textContent, language === 'zh' ? /技能24/ : /Skills24/);
      assert.equal(
        document.querySelectorAll('.context-usage-category')[1].textContent,
        translations[language].translation.chat.contextUsage.tools + '136',
      );
      assert.equal(document.querySelector('.context-usage-detail__kv strong').textContent, '88.3%');
      assert.equal(
        document.querySelector('.context-usage-detail__kv span').textContent,
        language === 'zh' ? 'KV命中率' : 'KV hit rate',
      );

      const next = structuredClone(fixture);
      next.context_window = { input_tokens: 2400, limit_tokens: 2000, occupancy_rate: 1.2 };
      next.parts.skills.percentage_of_window = 0.123;
      next.session_kv_cache_hit_rate = 0;
      await receive(next);
      assert.match(document.querySelector('.context-usage-detail__metric').textContent, /120%/);
      assert.equal(document.querySelector('.context-usage-ring__value').getAttribute('stroke-dasharray'), '100 0');
      assert.equal(document.querySelectorAll('.context-usage-breakdown__segment')[2].style.width, '12.3%');
      assert.match(document.querySelector('.context-usage-detail__kv').textContent, /0%/);

      await act(async () =>
        document.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true })),
      );
      assert.equal(document.querySelector('[role="dialog"]'), null);
      assert.equal(document.activeElement, trigger);
      await click(trigger);
      await click(document.querySelector('.context-usage-detail__close'));
      assert.equal(document.querySelector('[role="dialog"]'), null);
      await click(trigger);
      await act(async () => document.body.dispatchEvent(new window.Event('pointerdown', { bubbles: true })));
      assert.equal(document.querySelector('[role="dialog"]'), null);
    });
  });

  test(language + ': null fields remain unknown and no old category or KV value survives', async () => {
    await mount(language, async ({ receive, click }) => {
      await receive(structuredClone(fixture));
      const trigger = document.querySelector('.context-usage-trigger');
      await click(trigger);
      const next = structuredClone(fixture);
      next.context_window = { input_tokens: null, limit_tokens: null, occupancy_rate: null };
      next.session_kv_cache_hit_rate = null;
      next.parts = {};
      await receive(next);
      assert.equal(document.querySelector('.context-usage-ring__value'), null);
      assert.equal(document.querySelectorAll('.context-usage-category').length, 0);
      assert.equal(document.querySelectorAll('.context-usage-breakdown__segment').length, 0);
      const metric = document.querySelector('.context-usage-detail__metric').textContent;
      assert.match(metric, language === 'zh' ? /未报告/ : /Not reported/);
      assert.doesNotMatch(metric, /0%|1K|2.0K/);
      assert.equal(document.querySelector('.context-usage-detail__kv'), null);
      await click(document.querySelector('.context-usage-detail__close'));
      const tooltip = document.querySelector('[role="tooltip"]');
      assert.equal(tooltip.querySelectorAll('.context-usage-tooltip__row').length, 1);
      assert.doesNotMatch(tooltip.textContent, language === 'zh' ? /KV命中率/ : /KV hit rate/);
      assert.equal(document.querySelector('[role="alert"]'), null);
    });
  });
}

test('popovers stay above the trigger without reading their height and retain horizontal bounds', async () => {
  await mount('en', async ({ receive, click, dom }) => {
    let triggerRect = new dom.window.DOMRect(972, 500, 28, 28);
    let popupWidth = 258;
    const observers = new Map();
    globalThis.ResizeObserver = class {
      constructor(callback) {
        this.callback = callback;
      }
      observe(element) {
        observers.set(this, element);
      }
      disconnect() {
        observers.delete(this);
      }
    };
    const originalGetRect = dom.window.HTMLElement.prototype.getBoundingClientRect;
    dom.window.HTMLElement.prototype.getBoundingClientRect = function () {
      if (this.classList.contains('context-usage-trigger')) return triggerRect;
      if (this.matches('.context-usage-tooltip, .context-usage-detail')) {
        return {
          width: popupWidth,
          get height() {
            throw new Error('Popover placement must not measure height');
          },
        };
      }
      return originalGetRect.call(this);
    };
    const style = document.createElement('style');
    style.textContent = readFileSync(
      new URL('../src/components/ChatPanel/ContextUsageIndicator.css', import.meta.url),
      'utf8',
    );
    document.head.append(style);
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1200 });

    await receive(structuredClone(fixture));
    const trigger = document.querySelector('.context-usage-trigger');
    await act(async () => trigger.focus());
    const tooltip = document.querySelector('[role="tooltip"]');
    assert.equal(tooltip.style.top, '495px');
    assert.equal(tooltip.style.left, '745px');
    assert.equal(tooltip.style.transform, 'translateY(-100%)');

    await click(trigger);
    const detail = document.querySelector('[role="dialog"]');
    assert.equal(detail.style.top, '493px');
    assert.equal(detail.style.left, '742px');
    assert.equal(detail.style.transform, 'translateY(-100%)');
    const expectedStyle = document.createElement('div').style;
    expectedStyle.maxHeight = 'calc(500px - 7px - 8px)';
    assert.equal(detail.style.maxHeight, expectedStyle.maxHeight);
    assert.equal(window.getComputedStyle(detail).display, 'flex');
    assert.equal(window.getComputedStyle(detail.querySelector('.context-usage-detail__content')).overflowY, 'auto');

    await act(async () => {
      popupWidth = 400;
      for (const observer of observers.keys()) observer.callback();
    });
    assert.equal(detail.style.left, '600px');
    assert.equal(detail.style.top, '493px');

    await act(async () => {
      triggerRect = new dom.window.DOMRect(10, 200, 28, 28);
      Object.defineProperty(window, 'innerWidth', { configurable: true, value: 320 });
      window.dispatchEvent(new window.Event('resize'));
    });
    assert.equal(detail.style.left, '8px');
    assert.equal(detail.style.top, '193px');
    assert.equal(detail.style.transform, 'translateY(-100%)');
    expectedStyle.maxHeight = 'calc(200px - 7px - 8px)';
    assert.equal(detail.style.maxHeight, expectedStyle.maxHeight);
  });
});

test('team mode renders and updates only the leader snapshot while workers are running', async () => {
  await mount('en', async ({ receive, click, sessionId }) => {
    await act(async () => useSessionStore.getState().setMode(sessionId, 'team'));

    const leader = structuredClone(fixture);
    leader.role = 'leader';
    leader.depth = 2;
    leader.team_id = 'runtime-team';
    leader.member_name = 'leader-name';
    await receive(leader);
    const trigger = document.querySelector('.context-usage-trigger');
    assert.ok(trigger);
    await click(trigger);
    assert.match(document.querySelector('.context-usage-detail__metric').textContent, /50%/);

    const worker = structuredClone(fixture);
    worker.role = 'teammate';
    worker.member_name = 'worker-1';
    worker.context_window = { input_tokens: 1800, limit_tokens: 2000, occupancy_rate: 0.9 };
    await receive(worker);
    assert.match(document.querySelector('.context-usage-detail__metric').textContent, /50%/);

    const nextLeader = structuredClone(leader);
    nextLeader.context_window = { input_tokens: 1500, limit_tokens: 2000, occupancy_rate: 0.75 };
    await receive(nextLeader);
    assert.match(document.querySelector('.context-usage-detail__metric').textContent, /75%/);
  });
});

test('switching session or mode closes details; reopening an unobserved session never queries history', async () => {
  await mount('en', async ({ receive, click, sessionId }) => {
    await receive(structuredClone(fixture));
    await click(document.querySelector('.context-usage-trigger'));
    await act(async () => {
      useSessionStore.getState().ensureRuntime('other-session');
      useChatStore.getState().setActiveSessionId('other-session');
    });
    assert.equal(document.querySelector('[role="dialog"]'), null);
    assert.equal(document.querySelector('.context-usage-trigger'), null);
    await act(async () => useChatStore.getState().setActiveSessionId(sessionId));
    assert.equal(document.querySelector('[role="dialog"]'), null);
    await click(document.querySelector('.context-usage-trigger'));
    await act(async () => useSessionStore.getState().setMode(sessionId, 'team'));
    assert.equal(document.querySelector('[role="dialog"]'), null);
    assert.equal(document.querySelector('.context-usage-trigger'), null);
  });
});
