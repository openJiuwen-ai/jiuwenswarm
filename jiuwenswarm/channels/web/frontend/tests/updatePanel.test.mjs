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

const { act, createElement } = await import('react');
const { createRoot } = await import('react-dom/client');
const { default: i18n } = await import('i18next');
const { initReactI18next } = await import('react-i18next');
const { UpdatePanel } = await import('../node_modules/.cache/update-panel/index.mjs');
const resources = Object.fromEntries(['zh', 'en'].map(language => [language, {
  translation: JSON.parse(readFileSync(new URL(`../src/i18n/locales/${language}.json`, import.meta.url), 'utf8')),
}]));
await i18n.use(initReactI18next).init({ resources, lng: 'zh', interpolation: { escapeValue: false } });

async function withPanel(status, language, check) {
  const container = document.createElement('div');
  document.body.appendChild(container);
  const root = createRoot(container);
  const request = async method => method === 'updater.get_status' ? status : {};
  try {
    await act(async () => {
      await i18n.changeLanguage(language);
      root.render(createElement(UpdatePanel, { isConnected: true, request }));
    });
    await check(container);
  } finally {
    await act(async () => root.unmount());
    container.remove();
  }
}

for (const [language, label] of [['zh', '构建于'], ['en', 'Built on']]) {
  test(`shows the full daily version and a separate build date in ${language}`, async () => {
    await withPanel({ current_version: '0.2.8.dev20261026', current_build_date: '2026-10-26' }, language, container => {
      const version = container.querySelector('[data-testid="update-panel-info-card-value"][data-variant="current_version"]');
      const buildDate = container.querySelector('[data-testid="update-panel-build-date"]');
      assert.equal(version.textContent, '0.2.8.dev20261026');
      assert.match(buildDate.textContent, new RegExp(label));
      assert.equal(buildDate.querySelector('time').dateTime, '2026-10-26');
    });
  });
}

test('older servers and source checkouts omit the build date without extra separators', async () => {
  for (const current_build_date of [undefined, '', null, 42]) {
    await withPanel({ current_version: '0.2.8', current_build_date }, 'zh', container => {
      assert.equal(container.querySelector('[data-testid="update-panel-build-date"]'), null);
      assert.equal(container.querySelector('[data-testid="update-panel-current-build"]').textContent, '0.2.8');
    });
  }
});

test('release notifications keep the packaging date separate from the latest publication time', async () => {
  await withPanel({ current_version: '0.2.7', current_build_date: '2026-10-20' }, 'zh', async container => {
    await act(async () => window.dispatchEvent(new dom.window.CustomEvent('jiuwenswarm:updater-status', {
      detail: {
        current_version: '0.2.7', current_build_date: '2026-10-20',
        latest_version: '0.2.8', published_at: '2026-10-26T08:00:00Z', has_update: true,
      },
    })));
    assert.equal(container.querySelector('[data-testid="update-panel-build-date"] time').dateTime, '2026-10-20');
    assert.equal(container.querySelector('[data-testid="update-panel-info-card-value"][data-variant="latest_version"]').textContent, '0.2.8');
  });
});
