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
        avatar: { firstChar: '示', color: 'bg-blue-500' },
        state: 'idle',
        canOpenDetail: true,
        onOpenDetail() {},
        onQuickAdd() {},
      }),
    );
  } finally {
    console.error = originalError;
  }

  assert.match(markup, /min-h-\[160px\]/);
  assert.match(markup, /h-12 w-12/);
  assert.match(markup, /text-\[18px\]/);
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
