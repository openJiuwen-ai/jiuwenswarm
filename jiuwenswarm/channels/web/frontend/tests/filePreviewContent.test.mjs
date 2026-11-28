import assert from 'node:assert/strict';
import { after, test } from 'node:test';
import React, { act } from 'react';
import { JSDOM } from 'jsdom';
import i18next from 'i18next';
import { initReactI18next } from 'react-i18next';

const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost/' });
Object.assign(globalThis, {
  window: dom.window,
  document: dom.window.document,
  localStorage: dom.window.localStorage,
  sessionStorage: dom.window.sessionStorage,
  IS_REACT_ACT_ENVIRONMENT: true,
});
Object.defineProperty(globalThis, 'navigator', { configurable: true, value: dom.window.navigator });
// Bridge jsdom's window.setTimeout to the global one so node:test mock timers
// can fast-forward the copy feedback timer used via window.setTimeout.
dom.window.setTimeout = (fn, ms, ...rest) => setTimeout(fn, ms, ...rest);
dom.window.clearTimeout = (h) => clearTimeout(h);
after(() => dom.window.close());
const { createRoot } = await import('react-dom/client');
await i18next.use(initReactI18next).init({
  lng: 'en', showSupportNotice: false, resources: { en: { translation: {} } },
});
const { FilePreviewContent } = await import(
  '../node_modules/.cache/skill-search-races/components/ui/FilePreviewPanel/FilePreviewContent.js'
);

const LABELS = {
  selectPrompt: 'select a file',
  notPreviewable: 'not previewable',
  loading: 'loading',
  readError: 'read error',
  copy: 'Copy',
  copied: 'Copied!',
  copyFailed: 'Copy failed',
  download: 'Download',
  downloadFailed: 'Download failed',
  noPreview: 'no preview',
};

const copyButton = () => document.querySelector('[data-testid="file-preview-content-copy-btn"]');

test('连续复制：新反馈不被上一条的复位定时器提前清掉', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  globalThis.__writeClipboardImpl = async () => true;
  const rootEl = createRoot(document.getElementById('root'));
  await act(async () => {
    rootEl.render(React.createElement(FilePreviewContent, {
      selected: true,
      selectedPreviewable: true,
      file: { path: '/x/a.md', content: 'hello world' },
      status: 'success',
      errorText: '',
      labels: LABELS,
      testId: 'file-preview-content',
    }));
  });
  assert.equal(copyButton().disabled, false);
  await act(async () => { copyButton().click(); });
  await act(async () => {});
  assert.ok(copyButton().textContent.includes('Copied!'), '第一次复制应显示已复制');
  t.mock.timers.tick(1000);
  await act(async () => { copyButton().click(); });
  await act(async () => {});
  assert.ok(copyButton().textContent.includes('Copied!'), '第二次复制应重新显示已复制');
  t.mock.timers.tick(700);
  await act(async () => {});
  assert.ok(copyButton().textContent.includes('Copied!'), '新反馈不应被第一次的 1.6s 定时器提前清掉');
  t.mock.timers.tick(1000);
  await act(async () => {});
  assert.ok(!copyButton().textContent.includes('Copied!'), '新反馈满 1.6s 后应正常复位');
  rootEl.unmount();
  await act(async () => {});
});
