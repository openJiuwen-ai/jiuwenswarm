import assert from 'node:assert/strict';
import test from 'node:test';
import { act, createElement } from 'react';
import { createRoot } from 'react-dom/client';
import i18next from 'i18next';
import { I18nextProvider } from 'react-i18next';
import { JSDOM } from 'jsdom';

import { FilePreview } from '../node_modules/.cache/artifact-text-preview/FilePreview.js';

// "# EUR/CNY " + GBK bytes of "交易策略报告" -> what a bash heredoc produced on zh-CN Windows.
const GBK_REPORT = Uint8Array.from([
  ...Buffer.from('# EUR/CNY ', 'ascii'),
  0xbd, 0xbb, 0xd2, 0xd7, 0xb2, 0xdf, 0xc2, 0xd4, 0xb1, 0xa8, 0xb8, 0xe6,
  0x0a,
]);

function installGlobals(values) {
  const previousDescriptors = new Map();
  for (const [name, value] of Object.entries(values)) {
    previousDescriptors.set(name, Object.getOwnPropertyDescriptor(globalThis, name));
    Object.defineProperty(globalThis, name, { configurable: true, writable: true, value });
  }
  return () => {
    for (const [name, descriptor] of previousDescriptors) {
      if (descriptor) Object.defineProperty(globalThis, name, descriptor);
      else delete globalThis[name];
    }
  };
}

function createI18n() {
  const instance = i18next.createInstance();
  void instance.init({
    lng: 'en',
    fallbackLng: 'en',
    initImmediate: false,
    showSupportNotice: false,
    resources: {
      en: {
        translation: {
          common: { loading: 'Loading' },
          artifacts: { previewFailed: 'Preview failed', invalidJson: 'Invalid JSON' },
        },
      },
    },
  });
  return instance;
}

function stubDownloadResponse(bytes) {
  const original = globalThis.fetch;
  const copy = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
  globalThis.fetch = async () => ({
    ok: true,
    headers: { get: () => 'text/markdown' },
    // Mimics the browser default for a response without charset.
    text: async () => new TextDecoder('utf-8').decode(copy),
    arrayBuffer: async () => copy,
  });
  return () => {
    globalThis.fetch = original;
  };
}

async function settle(rounds = 20) {
  for (let index = 0; index < rounds; index += 1) {
    await act(async () => {
      await new Promise(resolve => setTimeout(resolve, 0));
    });
  }
}

test('gbk artifact renders readable text in the preview pane', async () => {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', { url: 'https://example.test/' });
  const restoreGlobals = installGlobals({
    Event: dom.window.Event,
    HTMLElement: dom.window.HTMLElement,
    IS_REACT_ACT_ENVIRONMENT: true,
    Node: dom.window.Node,
    document: dom.window.document,
    navigator: dom.window.navigator,
    window: dom.window,
  });
  const restoreFetch = stubDownloadResponse(GBK_REPORT);
  const container = dom.window.document.querySelector('#root');
  const i18n = createI18n();
  let root;
  try {
    await act(async () => {
      root = createRoot(container);
    });
    await act(async () => {
      root.render(createElement(
        I18nextProvider,
        { i18n },
        createElement(FilePreview, {
          artifact: {
            id: 'gbk-report',
            name: 'strategy_report.md',
            mimeType: 'text/markdown',
            downloadUrl: '/file-api/download?token=gbk',
          },
        }),
      ));
    });
    await settle();

    const text = container.textContent ?? '';
    assert.ok(container.querySelector('[data-testid="artifact-markdown-preview"]'), text);
    assert.ok(text.includes('交易策略报告'), text);
    assert.equal(text.includes('�'), false, text);
  } finally {
    if (root) await act(async () => root.unmount());
    restoreFetch();
    restoreGlobals();
    dom.window.close();
  }
});
