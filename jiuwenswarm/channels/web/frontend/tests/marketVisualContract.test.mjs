import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import i18next from 'i18next';
import { initReactI18next } from 'react-i18next';

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

test('cache notice stays hidden during normal refresh and identifies stale content', async () => {
  const { CatalogCacheNotice } =
    await import('../node_modules/.cache/market-visual-contract/marketplace/CatalogCacheNotice.js');

  const refreshingMarkup = renderToStaticMarkup(
    React.createElement(CatalogCacheNotice, {
      cache: { state: 'fresh', refreshing: true, complete: true },
    }),
  );
  assert.equal(refreshingMarkup, '');

  const staleMarkup = renderToStaticMarkup(
    React.createElement(CatalogCacheNotice, {
      cache: { state: 'stale', refreshing: true, updated_at: 1789522670.483113 },
    }),
  );
  assert.match(staleMarkup, /当前显示旧缓存/);
  assert.match(staleMarkup, /上次更新时间/);
  assert.doesNotMatch(staleMarkup, /1789522670/);

  const failedMarkup = renderToStaticMarkup(
    React.createElement(CatalogCacheNotice, {
      cache: { state: 'fresh', refreshing: false, error: 'refresh_failed' },
    }),
  );
  assert.match(failedMarkup, /目录刷新失败/);
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
