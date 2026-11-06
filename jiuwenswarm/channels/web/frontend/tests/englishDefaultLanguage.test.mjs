import assert from 'node:assert/strict';
import { mkdir } from 'node:fs/promises';
import { test } from 'node:test';
import { build } from 'esbuild';
import { JSDOM } from 'jsdom';

const output = new URL('../node_modules/.cache/english-default/i18n.mjs', import.meta.url);
await mkdir(new URL('.', output), { recursive: true });
await build({
  entryPoints: [new URL('../src/i18n/index.ts', import.meta.url).pathname.replace(/^\/(\w:)/, '$1')],
  outfile: output.pathname.replace(/^\/(\w:)/, '$1'),
  bundle: true,
  packages: 'external',
  platform: 'node',
  format: 'esm',
});

for (const [saved, expected] of [[null, 'en'], ['en', 'en'], ['zh', 'zh'], ['fr', 'en']]) {
  test(`UI language with saved preference ${saved} resolves to ${expected}`, async () => {
    const dom = new JSDOM('', { url: 'https://example.test' });
    globalThis.window = dom.window;
    globalThis.document = dom.window.document;
    Object.defineProperty(globalThis, 'navigator', {
      configurable: true,
      value: { language: 'zh-CN', languages: ['zh-CN'] },
    });
    if (saved !== null) window.localStorage.setItem('i18nextLng', saved);
    const { default: i18n } = await import(`${output.href}?saved=${saved}`);
    if (!i18n.isInitialized) await new Promise(resolve => i18n.on('initialized', resolve));
    assert.equal(i18n.resolvedLanguage, expected);
    assert.equal(window.localStorage.getItem('i18nextLng'), expected);
    dom.window.close();
    delete globalThis.window;
    delete globalThis.document;
    delete globalThis.navigator;
  });
}
