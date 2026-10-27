import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test, { before } from 'node:test';
import { build } from 'esbuild';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createInstance } from 'i18next';
import { I18nextProvider } from 'react-i18next';
import { JSDOM } from 'jsdom';

let MoaProgress, activeMoaPhase;
before(async () => {
  await build({
    entryPoints: ['src/components/ChatPanel/MoaProgress.tsx'],
    bundle: true,
    platform: 'node',
    format: 'esm',
    packages: 'external',
    outfile: 'node_modules/.cache/moa-progress/MoaProgress.mjs',
  });
  ({ MoaProgress, activeMoaPhase } = await import('../node_modules/.cache/moa-progress/MoaProgress.mjs'));
});

test('MoA display selects active verification before parallel reference operations', () => {
  const operations = {
    first: { phase: 'reference', status: 'completed' },
    second: { phase: 'reference', status: 'started' },
    verifier: { phase: 'verification', status: 'started' },
  };
  assert.equal(activeMoaPhase(operations), 'verification');
  operations.verifier.status = 'failed';
  assert.equal(activeMoaPhase(operations), 'reference');
  operations.second.status = 'cancelled';
  assert.equal(activeMoaPhase(operations), undefined);
  assert.equal(activeMoaPhase(undefined), undefined);
});

test('MoA hint renders only one localized status line and disappears when idle', async () => {
  const i18n = createInstance();
  const resources = Object.fromEntries(['zh', 'en'].map((language) => [language, {
    translation: JSON.parse(readFileSync(new URL(`../src/i18n/locales/${language}.json`, import.meta.url), 'utf8')),
  }]));
  await i18n.init({ lng: 'zh', resources, interpolation: { escapeValue: false } });
  const render = (phase) => renderToStaticMarkup(createElement(I18nextProvider, { i18n }, createElement(MoaProgress, { phase })));
  assert.equal(render(undefined), '');
  for (const [language, referenceLabel, verificationLabel] of [
    ['zh', '正在运行参考模型…', '正在评估任务执行进展…'],
    ['en', 'Running reference models…', 'Evaluating task progress…'],
  ]) {
    await i18n.changeLanguage(language);
    for (const [phase, label] of [['reference', referenceLabel], ['verification', verificationLabel]]) {
      const dom = new JSDOM(render(phase));
      const hint = dom.window.document.querySelector('[data-testid="chat-panel-moa-progress"]');
      assert.equal(hint.textContent, label);
      assert.equal(hint.getAttribute('role'), 'status');
      assert.equal(hint.getAttribute('aria-live'), 'polite');
      assert.equal(hint.getAttribute('data-variant'), phase);
      assert.equal(hint.querySelectorAll('button, details, pre').length, 0, 'there is no expandable execution detail');
      dom.window.close();
    }
  }
});
