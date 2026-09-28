import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import React, { act } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createRoot } from 'react-dom/client';
import i18next from 'i18next';
import { initReactI18next } from 'react-i18next';
import { JSDOM } from 'jsdom';

await i18next.use(initReactI18next).init({
  lng: 'zh',
  showSupportNotice: false,
  resources: {
    zh: {
      translation: {
        connectorMarket: {
          card: {
            install: '安装',
          },
        },
      },
    },
  },
  interpolation: { escapeValue: false },
});

test('market pages share the 1400px centered design surface', async () => {
  const { MarketplaceSurface } =
    await import('../node_modules/.cache/market-visual-contract/marketplace/MarketplaceSurface.js');

  const markup = renderToStaticMarkup(React.createElement(MarketplaceSurface, { variant: 'catalog' }, 'content'));

  assert.match(markup, /marketplace-surface--catalog/);
  assert.match(markup, /max-w-\[1400px\]/);
  assert.match(markup, /pt-16/);
});

test('cache notice raises a persistent right-side info toast and hides on fresh cache', async () => {
  const { CatalogCacheNotice } =
    await import('../node_modules/.cache/market-visual-contract/marketplace/CatalogCacheNotice.js');
  const { ToastStack } =
    await import('../node_modules/.cache/market-visual-contract/ui/Toast/Toast.js');

  const dom = new JSDOM('<!doctype html><html><body></body></html>', { url: 'http://localhost/' });
  const globals = { window: dom.window, document: dom.window.document, IS_REACT_ACT_ENVIRONMENT: true };
  const previous = new Map();
  for (const [name, value] of Object.entries(globals)) {
    previous.set(name, Object.getOwnPropertyDescriptor(globalThis, name));
    Object.defineProperty(globalThis, name, { configurable: true, writable: true, value });
  }

  const queryNotice = () => document.querySelector('[data-testid="marketplace-cache-notice"]');
  const host = document.createElement('div');
  document.body.appendChild(host);
  const root = createRoot(host);
  const renderNotice = async (cache) => {
    await act(async () => {
      root.render(
        React.createElement(React.Fragment, null,
          React.createElement(ToastStack),
          React.createElement(CatalogCacheNotice, { cache }),
        ),
      );
    });
  };

  try {
    // Normal refresh on fresh cache: no notice toast.
    await renderNotice({ state: 'fresh', refreshing: true, complete: true });
    assert.equal(queryNotice(), null);

    // Stale cache: persistent info toast in the right stack, with close always present.
    await renderNotice({ state: 'stale', refreshing: true, updated_at: 1789522670.483113 });
    const bubble = queryNotice();
    assert.ok(bubble, 'stale cache raises the notice toast');
    assert.match(bubble.className, /ui-toast--info/);
    assert.match(bubble.textContent, /当前显示旧缓存/);
    assert.match(bubble.textContent, /上次更新时间/);
    assert.doesNotMatch(bubble.textContent, /1789522670/);
    assert.ok(bubble.closest('.ui-toast-stack--right'), 'notice renders in the full-screen right stack');
    assert.ok(bubble.querySelector('[data-testid="ui-toast-close"]'), 'close is always visible');

    // Refresh failure reuses the same toast in place with the failure copy.
    await renderNotice({ state: 'fresh', refreshing: false, error: 'refresh_failed' });
    assert.match(queryNotice().textContent, /目录刷新失败/);

    // Back to fresh: the notice collapses (after the exit animation completes).
    await renderNotice({ state: 'fresh', refreshing: true, complete: true });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 260));
    });
    assert.equal(queryNotice(), null);

    await act(async () => root.unmount());
  } finally {
    for (const [name, descriptor] of previous) {
      if (descriptor) Object.defineProperty(globalThis, name, descriptor);
      else delete globalThis[name];
    }
    dom.window.close();
  }
});

test('market cards use the design card dimensions and typography', async () => {
  const { MarketCard } = await import('../node_modules/.cache/market-visual-contract/ConnectorMarket/MarketCard.js');

  const originalError = console.error;
  console.error = (...args) => {
    if (!String(args[0]).includes('useLayoutEffect does nothing on the server')) {
      originalError(...args);
    }
  };

  let markup;
  try {
    markup = renderToStaticMarkup(
      React.createElement(MarketCard, {
        title: '示例插件',
        description: '用于验证卡片布局',
        avatar: { firstChar: '示', style: { backgroundColor: 'rgba(157, 189, 252, 0.2)', boxShadow: '0 0 0 1px #9DBDFC', color: '#0b51de' } },
        state: 'idle',
        canOpenDetail: true,
        onOpenDetail() {},
        onQuickAdd() {},
      }),
    );
  } finally {
    console.error = originalError;
  }

  assert.match(markup, /class="page-card/);
  assert.match(markup, /entity-header/);
  const cardCss = readFileSync(new URL('../src/components/ui/PageCard/PageCard.css', import.meta.url), 'utf8');
  assert.match(cardCss, /height: 160px/);
  assert.match(cardCss, /min-width: 360px/);
});

test('connector card connect action resolves in both supported locales', async () => {
  const localeFiles = [
    ['zh', '../src/i18n/locales/zh.json', '连接'],
    ['en', '../src/i18n/locales/en.json', 'Connect'],
  ];

  for (const [language, relativePath, expected] of localeFiles) {
    const resources = JSON.parse(readFileSync(new URL(relativePath, import.meta.url), 'utf8'));
    const translator = i18next.createInstance();
    await translator.init({ lng: language, resources: { [language]: { translation: resources } } });
    assert.equal(translator.t('connectorMarket.card.connect'), expected);
  }
});
