import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { after, test } from 'node:test';
import { JSDOM } from 'jsdom';

const dom = new JSDOM('<!doctype html><html><body></body></html>', { url: 'http://localhost/' });
for (const [key, value] of Object.entries({
  window: dom.window, document: dom.window.document, navigator: dom.window.navigator,
  IS_REACT_ACT_ENVIRONMENT: true,
})) {
  Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
}
after(() => dom.window.close());

const { act, createElement: h } = await import('react');
const { createRoot } = await import('react-dom/client');
const { default: i18n } = await import('i18next');
const { initReactI18next } = await import('react-i18next');
const { TeamPanel } = await import('../node_modules/.cache/team-panel-load-error/src/components/TeamPanel/index.js');
const { webClientStubState } = await import('../node_modules/.cache/team-panel-load-error/tests/fixtures/team-panel-load-error/webClientStub.js');
const resources = Object.fromEntries(['zh', 'en'].map(language => [language, {
  translation: JSON.parse(readFileSync(new URL(`../src/i18n/locales/${language}.json`, import.meta.url), 'utf8')),
}]));
await i18n.use(initReactI18next).init({ resources, lng: 'zh', interpolation: { escapeValue: false } });

const twoTeamSessions = [
  { mode: 'team', team_name: '研发一组', session_id: 'sess-a', title: '周会纪要', last_message_at: 1730000000, created_at: 1730000000 },
  { mode: 'team', team_name: '研发二组', session_id: 'sess-b', title: '评审会', last_message_at: 1730001000, created_at: 1730001000 },
];

function createHarness() {
  async function render() {
    const container = document.createElement('div');
    document.body.appendChild(container);
    const root = createRoot(container);
    await act(async () => {
      root.render(h(TeamPanel, { onSessionsDeleted: () => {} }));
    });
    return {
      container,
      async settle() {
        await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
      },
      async click(selector) {
        const element = container.querySelector(selector);
        assert.ok(element, `expected element ${selector} to be present`);
        assert.equal(element.disabled, undefined || false, `expected ${selector} to be clickable`);
        await act(async () => element.click());
        await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
      },
      async unmount() {
        await act(async () => root.unmount());
        container.remove();
      },
    };
  }
  return { render };
}

const rowCount = (container) => container.querySelectorAll('[data-testid="team-panel-team-row"]').length;

test('a failed refresh keeps the already loaded team list visible', async () => {
  const { render } = createHarness();
  webClientStubState.failSessionList = false;
  webClientStubState.sessions = twoTeamSessions;
  const page = await render();
  try {
    await page.settle();
    assert.equal(rowCount(page.container), 2, 'expected two teams after the initial load');

    webClientStubState.failSessionList = true;
    await page.click('[data-testid="team-panel-refresh"]');
    assert.ok(page.container.querySelector('[data-testid="team-panel-error"]'),
      'expected the load error banner after a failed refresh');
    assert.equal(rowCount(page.container), 2,
      'a failed refresh must not wipe the already displayed team list');

    webClientStubState.failSessionList = false;
    await page.click('[data-testid="team-panel-refresh"]');
    assert.equal(page.container.querySelector('[data-testid="team-panel-error"]'), null,
      'the error banner should clear once the refresh succeeds');
    assert.equal(rowCount(page.container), 2, 'the teams should survive the failure and the recovery');
  } finally {
    await page.unmount();
  }
});

test('a failed initial load shows the error banner with no stale list', async () => {
  const { render } = createHarness();
  webClientStubState.failSessionList = true;
  webClientStubState.sessions = [];
  const page = await render();
  try {
    await page.settle();
    assert.ok(page.container.querySelector('[data-testid="team-panel-error"]'),
      'expected the load error banner on mount failure');
    assert.equal(rowCount(page.container), 0, 'no team rows can exist before a successful load');
    const refresh = page.container.querySelector('[data-testid="team-panel-refresh"]');
    assert.ok(refresh, 'the refresh button should act as the retry entry');
    assert.equal(refresh.disabled, false, 'loading should have ended so the user can retry');

    webClientStubState.failSessionList = false;
    await page.click('[data-testid="team-panel-refresh"]');
    assert.equal(page.container.querySelector('[data-testid="team-panel-error"]'), null);
    assert.equal(rowCount(page.container), 0, 'the empty backend still lists zero teams');
  } finally {
    await page.unmount();
  }
});
