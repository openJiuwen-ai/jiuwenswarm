import assert from 'node:assert/strict';
import test from 'node:test';
import { act, createElement } from 'react';
import { createRoot } from 'react-dom/client';
import i18next from 'i18next';
import { I18nextProvider } from 'react-i18next';
import { JSDOM } from 'jsdom';

import { MarkdownRenderer } from '../node_modules/.cache/markdown-renderer/MarkdownRenderer.js';
import { convertSvgToPng, downloadBlob, saveBlob } from '../node_modules/.cache/markdown-renderer/diagrams/diagramExport.js';
import { MermaidDiagram } from '../node_modules/.cache/markdown-renderer/diagrams/MermaidDiagram.js';
import { SvgDiagram } from '../node_modules/.cache/markdown-renderer/diagrams/SvgDiagram.js';
import { getSvgMarkupStatus, getSvgPreview, SVG_PREVIEW_DOCUMENT, updateSvgPreview } from '../node_modules/.cache/markdown-renderer/diagrams/svgPreview.js';
import { UNTRUSTED_STATIC_PREVIEW_CSP, UNTRUSTED_STATIC_PREVIEW_SANDBOX } from '../node_modules/.cache/markdown-renderer/isolatedPreview.js';

const SVG_NAMESPACE = 'http://www.w3.org/2000/svg';

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
          diagram: {
            image: 'Image',
            code: 'Code',
            moreActions: 'More diagram actions',
            downloadImage: 'Download as image',
            copyCode: 'Copy code',
            copied: 'Copied',
            copyFailed: 'Copy failed',
            preparingImage: 'Preparing image…',
            downloadImageFailed: 'Could not create image',
          },
          svg: {
            streaming: 'Drawing…',
            invalid: 'SVG code contains errors',
            previewTitle: 'SVG image preview',
          },
          mermaid: {
            rendering: 'Rendering…',
            zoomIn: 'Zoom in',
            zoomOut: 'Zoom out',
            fitView: 'Fit view',
          },
        },
      },
    },
  });
  return instance;
}

test('classifies streaming, valid, and malformed SVG markup with browser DOM parsing rules', () => {
  const dom = new JSDOM();
  const restore = installGlobals({ DOMParser: dom.window.DOMParser });
  try {
    const unclosedPreview = getSvgPreview('<svg><g>');
    assert.equal(getSvgMarkupStatus(unclosedPreview, false, true), 'streaming');
    assert.equal(getSvgMarkupStatus(unclosedPreview, false, false), 'invalid');
    assert.equal(getSvgMarkupStatus(getSvgPreview(`<svg xmlns="${SVG_NAMESPACE}"><rect /></svg>`), false, false), 'ready');
    assert.equal(getSvgMarkupStatus(getSvgPreview(`<svg xmlns="${SVG_NAMESPACE}"><rect /></svg>`), true, false), 'ready');
    assert.equal(getSvgMarkupStatus(getSvgPreview('<svg viewBox="0 0 950 580"><rect /></svg>'), true, false), 'ready');
    assert.equal(getSvgMarkupStatus(getSvgPreview(`<svg xmlns="${SVG_NAMESPACE}"><g></svg>`), true, false), 'invalid');
    assert.equal(getSvgMarkupStatus(getSvgPreview('<div />'), true, false), 'invalid');

    const normalized = new dom.window.DOMParser().parseFromString(unclosedPreview.markup, 'image/svg+xml');
    assert.equal(normalized.querySelector('parsererror'), null);
    assert.equal(normalized.documentElement.namespaceURI, SVG_NAMESPACE);
  } finally {
    restore();
    dom.window.close();
  }
});

test('derives the SVG preview aspect ratio from intrinsic dimensions', () => {
  const dom = new JSDOM();
  const restore = installGlobals({ DOMParser: dom.window.DOMParser });
  try {
    assert.equal(getSvgPreview(`<svg xmlns="${SVG_NAMESPACE}" viewBox="0 0 800 600" />`).aspectRatio, 4 / 3);
    assert.equal(getSvgPreview(`<svg xmlns="${SVG_NAMESPACE}" viewBox="0 0 800 1200"><g>`).aspectRatio, 2 / 3);
    assert.equal(getSvgPreview(`<svg xmlns="${SVG_NAMESPACE}" width="640px" height="320" />`).aspectRatio, 2);
    assert.equal(getSvgPreview(`<svg xmlns="${SVG_NAMESPACE}" viewBox="0 0 0 100" />`).aspectRatio, null);
    assert.equal(getSvgPreview('<div />'), null);
  } finally {
    restore();
    dom.window.close();
  }
});

test('updates an existing SVG preview in place and removes stale nodes and attributes', () => {
  const dom = new JSDOM(SVG_PREVIEW_DOCUMENT);
  const restore = installGlobals({ Node: dom.window.Node });
  const frame = { contentDocument: dom.window.document };
  try {
    updateSvgPreview(frame, `<svg xmlns="${SVG_NAMESPACE}" viewBox="0 0 100 80" data-version="first"><g id="layer"><rect id="shape" width="40" /></g></svg>`);
    const originalRoot = dom.window.document.body.firstElementChild;
    assert.ok(originalRoot);
    assert.equal(originalRoot.namespaceURI, SVG_NAMESPACE);

    updateSvgPreview(
      frame,
      `<svg xmlns="${SVG_NAMESPACE}" viewBox="0 0 120 90" aria-label="Updated"><g id="layer"><circle id="shape" r="12">label</circle></g></svg>`,
    );

    assert.strictEqual(dom.window.document.body.firstElementChild, originalRoot);
    assert.equal(originalRoot.getAttribute('viewBox'), '0 0 120 90');
    assert.equal(originalRoot.getAttribute('aria-label'), 'Updated');
    assert.equal(originalRoot.hasAttribute('data-version'), false);
    assert.equal(originalRoot.querySelector('rect'), null);
    assert.equal(originalRoot.querySelector('circle')?.textContent, 'label');

    updateSvgPreview(frame, '<div>not an SVG</div>');
    assert.strictEqual(dom.window.document.body.firstElementChild, originalRoot);
  } finally {
    restore();
    dom.window.close();
  }
});

test('renders SVG markup only inside a sandboxed iframe and falls back to code for invalid markup', async () => {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', { url: 'https://example.test/' });
  const clipboardWrites = [];
  Object.defineProperty(dom.window.navigator, 'clipboard', {
    configurable: true,
    value: { writeText: async value => clipboardWrites.push(value) },
  });
  const restore = installGlobals({
    DOMParser: dom.window.DOMParser,
    Event: dom.window.Event,
    HTMLElement: dom.window.HTMLElement,
    HTMLIFrameElement: dom.window.HTMLIFrameElement,
    IS_REACT_ACT_ENVIRONMENT: true,
    MouseEvent: dom.window.MouseEvent,
    Node: dom.window.Node,
    document: dom.window.document,
    navigator: dom.window.navigator,
    window: dom.window,
  });
  const container = dom.window.document.querySelector('#root');
  const i18n = createI18n();
  const streamingCode = `<svg xmlns="${SVG_NAMESPACE}" viewBox="0 0 800 1200"><g>`;
  const validCode = `<svg xmlns="${SVG_NAMESPACE}" viewBox="0 0 10 10"><script>alert('outside')</script><rect width="10" height="10" /></svg>`;
  let root;
  try {
    root = createRoot(container);
    await act(async () => {
      root.render(createElement(I18nextProvider, { i18n }, createElement(SvgDiagram, { code: streamingCode, complete: false, isStreaming: true })));
    });
    assert.equal(container.querySelector('[data-svg-status="streaming"] iframe')?.style.aspectRatio, `${2 / 3}`);

    await act(async () => {
      root.render(createElement(I18nextProvider, { i18n }, createElement(SvgDiagram, { code: validCode, complete: true, isStreaming: false })));
    });

    const diagram = container.querySelector('[data-svg-status="ready"]');
    const frame = diagram?.querySelector('iframe');
    assert.ok(frame);
    assert.equal(frame.getAttribute('sandbox'), UNTRUSTED_STATIC_PREVIEW_SANDBOX);
    assert.equal(frame.getAttribute('sandbox').includes('allow-scripts'), false);
    assert.equal(frame.style.aspectRatio, '1');
    for (const directive of UNTRUSTED_STATIC_PREVIEW_CSP.split('; ')) {
      assert.match(frame.getAttribute('srcdoc'), new RegExp(directive.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
    }
    assert.equal(container.innerHTML.includes("alert('outside')"), false);

    await act(async () => {
      diagram.querySelector('[aria-label="More diagram actions"]').dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));
    });
    const copyButton = Array.from(diagram.querySelectorAll('[role="menuitem"]')).find(button => button.textContent.includes('Copy code'));
    assert.ok(copyButton);
    await act(async () => {
      copyButton.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));
    });
    assert.deepEqual(clipboardWrites, [validCode]);
    assert.match(diagram.textContent, /Copied/);

    await act(async () => root.unmount());
    root = createRoot(container);
    await act(async () => {
      root.render(createElement(I18nextProvider, { i18n }, createElement(SvgDiagram, { code: '<div>not an SVG</div>', complete: true, isStreaming: false })));
    });
    const invalidDiagram = container.querySelector('[data-svg-status="invalid"]');
    assert.ok(invalidDiagram?.querySelector('.svg-diagram__code-view'));
    assert.ok(invalidDiagram?.querySelector('.diagram-toolbar-status--warning'));
    assert.equal(invalidDiagram?.querySelector('.diagram-toolbar-status--error'), null);
    assert.match(container.textContent, /SVG code contains errors/);
    assert.equal(Array.from(container.querySelectorAll('button')).find(button => button.textContent === 'Image')?.disabled, true);
    assert.equal(invalidDiagram?.querySelector('iframe'), null);

    await act(async () => {
      invalidDiagram?.querySelector('[aria-label="More diagram actions"]')?.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));
    });
    assert.equal(invalidDiagram?.querySelector('[data-variant="download-source"]'), null);
    assert.equal(invalidDiagram?.querySelector('[data-variant="download-image"]')?.disabled, false);
  } finally {
    if (root) await act(async () => root.unmount());
    restore();
    dom.window.close();
  }
});

test('renders Mermaid through the shared viewer and preserves the code fallback contract', async () => {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', { url: 'https://example.test/' });
  class ResizeObserverStub {
    observe() {}
    disconnect() {}
  }
  const restore = installGlobals({
    HTMLElement: dom.window.HTMLElement,
    IS_REACT_ACT_ENVIRONMENT: true,
    MouseEvent: dom.window.MouseEvent,
    ResizeObserver: ResizeObserverStub,
    document: dom.window.document,
    navigator: dom.window.navigator,
    window: dom.window,
  });
  const container = dom.window.document.querySelector('#root');
  const i18n = createI18n();
  let root;
  try {
    root = createRoot(container);
    const renderSvg = async () => '<svg width="100%" height="100%" viewBox="0 0 120 60" style="max-width: 120px"><rect width="120" height="60" /></svg>';
    await act(async () => {
      root.render(createElement(I18nextProvider, { i18n }, createElement(MermaidDiagram, { code: 'graph TD; A-->B', renderSvg })));
    });

    const diagram = container.querySelector('[data-mermaid-status="rendered"]');
    const renderedSvg = diagram?.querySelector('.mermaid-svg-wrapper svg');
    assert.ok(renderedSvg);
    assert.equal(renderedSvg.style.width, '120px');
    assert.equal(renderedSvg.style.height, '60px');
    assert.equal(diagram.getAttribute('data-markdown-block'), 'wide');
    assert.equal(diagram.querySelector('[aria-label="More diagram actions"]').getAttribute('aria-haspopup'), 'menu');

    const codeTab = Array.from(diagram.querySelectorAll('button')).find(button => button.textContent === 'Code');
    await act(async () => codeTab.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true })));
    assert.equal(diagram.querySelector('.mermaid-code-view code').textContent, 'graph TD; A-->B');

    await act(async () => root.unmount());
    root = createRoot(container);
    await act(async () => {
      root.render(
        createElement(
          I18nextProvider,
          { i18n },
          createElement(MermaidDiagram, {
            code: 'invalid diagram',
            renderSvg: async () => {
              throw new Error('parse failed');
            },
          }),
        ),
      );
    });
    assert.equal(container.querySelector('pre.mermaid-error[data-mermaid-status="error"] code').textContent, 'invalid diagram');
  } finally {
    if (root) await act(async () => root.unmount());
    restore();
    dom.window.close();
  }
});

test('ignores a stale Mermaid render after the source changes', async () => {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>');
  class ResizeObserverStub {
    observe() {}
    disconnect() {}
  }
  const pending = new Map();
  const renderSvg = (_id, code) => new Promise(resolve => pending.set(code, resolve));
  const restore = installGlobals({
    HTMLElement: dom.window.HTMLElement,
    IS_REACT_ACT_ENVIRONMENT: true,
    ResizeObserver: ResizeObserverStub,
    document: dom.window.document,
    navigator: dom.window.navigator,
    window: dom.window,
  });
  const container = dom.window.document.querySelector('#root');
  const i18n = createI18n();
  let root;
  try {
    root = createRoot(container);
    await act(async () => {
      root.render(createElement(I18nextProvider, { i18n }, createElement(MermaidDiagram, { code: 'first', renderSvg })));
    });
    await act(async () => {
      root.render(createElement(I18nextProvider, { i18n }, createElement(MermaidDiagram, { code: 'second', renderSvg })));
    });
    await act(async () => pending.get('first')('<svg data-source="first" viewBox="0 0 10 10" />'));
    assert.equal(container.querySelector('[data-source="first"]'), null);
    await act(async () => pending.get('second')('<svg data-source="second" viewBox="0 0 10 10" />'));
    assert.ok(container.querySelector('[data-source="second"]'));
  } finally {
    if (root) await act(async () => root.unmount());
    restore();
    dom.window.close();
  }
});

test('keeps a browser-renderable incomplete SVG visible with an invalid status', async () => {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>');
  const restore = installGlobals({
    DOMParser: dom.window.DOMParser,
    Event: dom.window.Event,
    HTMLElement: dom.window.HTMLElement,
    HTMLIFrameElement: dom.window.HTMLIFrameElement,
    IS_REACT_ACT_ENVIRONMENT: true,
    Node: dom.window.Node,
    document: dom.window.document,
    navigator: dom.window.navigator,
    window: dom.window,
  });
  const container = dom.window.document.querySelector('#root');
  const i18n = createI18n();
  const markdown = `\`\`\`svg\n<svg xmlns="${SVG_NAMESPACE}" viewBox="0 0 100 100"><g>`;
  let root;
  try {
    root = createRoot(container);
    await act(async () => {
      root.render(createElement(I18nextProvider, { i18n }, createElement(MarkdownRenderer, { content: markdown, isStreaming: true })));
    });
    assert.match(container.textContent, /Drawing/);
    assert.ok(container.querySelector('[data-svg-status="streaming"] iframe'));

    await act(async () => {
      root.render(createElement(I18nextProvider, { i18n }, createElement(MarkdownRenderer, { content: markdown, isStreaming: false })));
    });
    assert.doesNotMatch(container.textContent, /Drawing/);
    assert.match(container.textContent, /SVG code contains errors/);
    const diagram = container.querySelector('[data-svg-status="invalid"]');
    assert.ok(diagram?.querySelector('iframe'));
    assert.equal(diagram.querySelector('[aria-pressed="true"]')?.textContent, 'Image');
    assert.equal(Array.from(diagram.querySelectorAll('button')).find(button => button.textContent === 'Image')?.disabled, false);
  } finally {
    if (root) await act(async () => root.unmount());
    restore();
    dom.window.close();
  }
});

test('keeps the browser-renderable portion of multiple SVG roots visible with an invalid status', async () => {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>');
  const restore = installGlobals({
    DOMParser: dom.window.DOMParser,
    HTMLElement: dom.window.HTMLElement,
    IS_REACT_ACT_ENVIRONMENT: true,
    Node: dom.window.Node,
    document: dom.window.document,
    navigator: dom.window.navigator,
    window: dom.window,
  });
  const container = dom.window.document.querySelector('#root');
  const i18n = createI18n();
  const markdown = ['```svg', '<svg viewBox="0 0 10 10" />', '<svg viewBox="0 0 20 20" />', '<svg viewBox="0 0 30 30" />', '```'].join('\n');
  let root;
  try {
    root = createRoot(container);
    await act(async () => {
      root.render(createElement(I18nextProvider, { i18n }, createElement(MarkdownRenderer, { content: markdown })));
    });

    const diagram = container.querySelector('[data-svg-status="invalid"]');
    assert.ok(diagram?.querySelector('iframe'));
    assert.equal(diagram.querySelector('[aria-pressed="true"]')?.textContent, 'Image');
    assert.equal(Array.from(diagram.querySelectorAll('button')).find(button => button.textContent === 'Image')?.disabled, false);
    assert.match(container.textContent, /SVG code contains errors/);
  } finally {
    if (root) await act(async () => root.unmount());
    restore();
    dom.window.close();
  }
});

test('keeps Markdown behavior compatible while dispatching supported fenced blocks', async () => {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', { url: 'https://example.test/' });
  const restore = installGlobals({
    DOMParser: dom.window.DOMParser,
    Event: dom.window.Event,
    HTMLElement: dom.window.HTMLElement,
    HTMLIFrameElement: dom.window.HTMLIFrameElement,
    IS_REACT_ACT_ENVIRONMENT: true,
    Node: dom.window.Node,
    document: dom.window.document,
    navigator: dom.window.navigator,
    window: dom.window,
  });
  const container = dom.window.document.querySelector('#root');
  const i18n = createI18n();
  const markdown = [
    '# 标题',
    '',
    '[外链](https://example.com) 与 [片段](#标题)',
    '',
    '| A | B | | --- | --- | | 1 | 2 |',
    '',
    '```svg',
    `<svg xmlns="${SVG_NAMESPACE}" viewBox="0 0 10 10"><rect width="10" height="10" /></svg>`,
    '```',
    '',
    '```xml',
    `<svg xmlns="${SVG_NAMESPACE}" viewBox="0 0 10 10"><circle cx="5" cy="5" r="4" /></svg>`,
    '```',
    '',
    '```html',
    `<div class="icon"><svg viewBox="0 0 10 10"><path d="M0 0h10v10z" /></svg></div>`,
    '```',
    '',
    '```mermaid',
    'graph TD; A-->B',
    '',
    '<section data-host-injection="blocked">raw html</section>',
  ].join('\n');
  let root;
  try {
    root = createRoot(container);
    await act(async () => {
      root.render(createElement(I18nextProvider, { i18n }, createElement(MarkdownRenderer, { content: markdown, testId: 'markdown' })));
    });

    assert.equal(container.querySelector('h1').id, '标题');
    const links = Array.from(container.querySelectorAll('a'));
    assert.equal(links.find(link => link.textContent === '外链').target, '_blank');
    assert.equal(links.find(link => link.textContent === '片段').hasAttribute('target'), false);
    assert.ok(container.querySelector('.chat-markdown-table-wrap table'));
    assert.ok(container.querySelector('[data-svg-status="ready"] iframe'));
    assert.ok(container.querySelector('pre code.language-xml'));
    assert.ok(container.querySelector('pre code.language-html'));
    assert.ok(container.querySelector('pre code.language-mermaid'));
    assert.equal(container.querySelector('[data-host-injection="blocked"]'), null);
    assert.match(container.textContent, /<section data-host-injection="blocked">raw html<\/section>/);
  } finally {
    if (root) await act(async () => root.unmount());
    restore();
    dom.window.close();
  }
});

test('renders dollar and native LaTeX delimiters through KaTeX', async () => {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>');
  const restore = installGlobals({
    HTMLElement: dom.window.HTMLElement,
    IS_REACT_ACT_ENVIRONMENT: true,
    Node: dom.window.Node,
    document: dom.window.document,
    navigator: dom.window.navigator,
    window: dom.window,
  });
  const container = dom.window.document.querySelector('#root');
  const markdown = [
    String.raw`Inline $x^2$ and \(\frac{a}{b}\).`,
    '',
    '$$',
    String.raw`\int_0^1 x\,dx`,
    '$$',
    '',
    String.raw`Same-line display: \[S=(1,0)\]`,
    '',
    String.raw`\[`,
    String.raw`\sum_{i=1}^{n} i`,
    String.raw`\]`,
  ].join('\n');
  let root;
  try {
    root = createRoot(container);
    await act(async () => {
      root.render(createElement(MarkdownRenderer, { className: 'chat-markdown', content: markdown }));
    });

    assert.equal(container.querySelectorAll('.katex').length, 5);
    assert.equal(container.querySelectorAll('.katex-display').length, 3);
    assert.equal(container.querySelectorAll('.katex-mathml math').length, 5);
    assert.match(container.textContent, /x2/);
    assert.match(container.textContent, /S=\(1,0\)/);
  } finally {
    if (root) await act(async () => root.unmount());
    restore();
    dom.window.close();
  }
});

test('isolates LaTeX parsing from code and incomplete streaming delimiters', async () => {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>');
  const restore = installGlobals({
    HTMLElement: dom.window.HTMLElement,
    IS_REACT_ACT_ENVIRONMENT: true,
    Node: dom.window.Node,
    document: dom.window.document,
    navigator: dom.window.navigator,
    window: dom.window,
  });
  const container = dom.window.document.querySelector('#root');
  const markdown = [
    String.raw`Rendered: \(x+y\).`,
    '',
    'Inline code: `$not_math$ and \\(not_math\\)`.',
    '',
    '```text',
    String.raw`$not_math$ and \[not_math\]`,
    '```',
    '',
    String.raw`Incomplete stream: \(x+y`,
    '',
    'Price: $100.',
  ].join('\n');
  let root;
  try {
    root = createRoot(container);
    await act(async () => {
      root.render(createElement(MarkdownRenderer, { className: 'chat-markdown', content: markdown }));
    });

    assert.equal(container.querySelectorAll('.katex').length, 1);
    const codeBlocks = Array.from(container.querySelectorAll('code'));
    assert.ok(codeBlocks.some(code => code.textContent === String.raw`$not_math$ and \(not_math\)`));
    assert.ok(codeBlocks.some(code => code.textContent.includes(String.raw`$not_math$ and \[not_math\]`)));
    assert.match(container.textContent, /Incomplete stream: \(x\+y/);
    assert.match(container.textContent, /Price: \$100\./);
  } finally {
    if (root) await act(async () => root.unmount());
    restore();
    dom.window.close();
  }
});

test('downloads a blob through a temporary anchor and always revokes its object URL', () => {
  const dom = new JSDOM('<!doctype html><html><body></body></html>');
  const calls = [];
  const objectUrl = 'blob:test-download';
  const originalClick = dom.window.HTMLAnchorElement.prototype.click;
  dom.window.HTMLAnchorElement.prototype.click = function click() {
    calls.push({ connected: this.isConnected, download: this.download, href: this.href });
  };
  const restore = installGlobals({
    URL: {
      createObjectURL: blob => {
        calls.push({ created: blob });
        return objectUrl;
      },
      revokeObjectURL: url => calls.push({ revoked: url }),
    },
    document: dom.window.document,
  });
  const blob = new Blob(['source'], { type: 'image/svg+xml' });
  try {
    downloadBlob(blob, 'diagram.svg');
    assert.strictEqual(calls[0].created, blob);
    assert.deepEqual(calls[1], { connected: true, download: 'diagram.svg', href: objectUrl });
    assert.deepEqual(calls[2], { revoked: objectUrl });
    assert.equal(dom.window.document.body.children.length, 0);
  } finally {
    restore();
    dom.window.HTMLAnchorElement.prototype.click = originalClick;
    dom.window.close();
  }
});

test('saves diagram blobs through the bounded desktop chunk bridge', async () => {
  const dom = new JSDOM('<!doctype html><html><body></body></html>');
  const calls = [];
  class FileReaderStub {
    readAsDataURL(blob) {
      calls.push(['encode', blob.size]);
      this.result = 'data:image/svg+xml;charset=utf-8;base64,PHN2Zy8+';
      queueMicrotask(() => this.onload());
    }
  }
  dom.window.pywebview = {
    api: {
      begin_blob_save: async (...args) => {
        calls.push(['begin', ...args]);
        return { ok: true, cancelled: false, transfer_id: 'transfer-1' };
      },
      append_blob_save: async (...args) => {
        calls.push(['append', ...args]);
        return true;
      },
      finish_blob_save: async (...args) => {
        calls.push(['finish', ...args]);
        return { ok: true, cancelled: false };
      },
      abort_blob_save: async (...args) => {
        calls.push(['abort', ...args]);
        return true;
      },
    },
  };
  const restore = installGlobals({
    FileReader: FileReaderStub,
    document: dom.window.document,
    window: dom.window,
  });
  try {
    const outcome = await saveBlob(new Blob(['<svg/>'], { type: 'image/svg+xml;charset=utf-8' }), 'diagram.svg');
    assert.equal(outcome, 'saved');
    assert.deepEqual(calls, [
      ['begin', 'diagram.svg', 'image/svg+xml;charset=utf-8', 6],
      ['encode', 6],
      ['append', 'transfer-1', 'PHN2Zy8+'],
      ['finish', 'transfer-1'],
    ]);
  } finally {
    restore();
    dom.window.close();
  }
});

test('converts bounded SVG dimensions to PNG and rejects oversized exports', async () => {
  const dom = new JSDOM();
  const drawCalls = [];
  const revokedUrls = [];
  const canvas = {
    getContext: () => ({ drawImage: (...args) => drawCalls.push(args) }),
    height: 0,
    toBlob: callback => callback(new Blob(['png'], { type: 'image/png' })),
    width: 0,
  };
  class LoadedImage {
    decoding = 'auto';
    naturalHeight = 0;
    naturalWidth = 0;

    set src(value) {
      this.currentSrc = value;
      queueMicrotask(() => this.onload());
    }
  }
  const restore = installGlobals({
    DOMParser: dom.window.DOMParser,
    Image: LoadedImage,
    URL: {
      createObjectURL: () => 'blob:test-image',
      revokeObjectURL: url => revokedUrls.push(url),
    },
    document: { createElement: tagName => (tagName === 'canvas' ? canvas : null) },
  });
  try {
    const png = await convertSvgToPng(`<svg xmlns="${SVG_NAMESPACE}" viewBox="0 0 320.2 199.1"><rect width="100%" height="100%" /></svg>`);
    assert.equal(png.type, 'image/png');
    assert.equal(canvas.width, 321);
    assert.equal(canvas.height, 200);
    assert.equal(drawCalls.length, 1);
    assert.deepEqual(revokedUrls, ['blob:test-image']);

    await assert.rejects(
      convertSvgToPng(`<svg xmlns="${SVG_NAMESPACE}" viewBox="0 0 9000 100"><rect width="100%" height="100%" /></svg>`),
      /SVG export dimensions are unsupported/,
    );
    assert.deepEqual(revokedUrls, ['blob:test-image', 'blob:test-image']);
  } finally {
    restore();
    dom.window.close();
  }
});
