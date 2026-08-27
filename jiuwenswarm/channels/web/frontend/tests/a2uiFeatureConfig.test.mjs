import assert from 'node:assert/strict';
import test from 'node:test';
import { act, createElement } from 'react';
import { createRoot } from 'react-dom/client';
import { A2UIProvider } from '@a2ui/react';
import i18next from 'i18next';
import { I18nextProvider } from 'react-i18next';
import { JSDOM, VirtualConsole } from 'jsdom';

import { A2UIMessageContent } from '../node_modules/.cache/a2ui-feature-config/A2UIMessageContent.js';
import {
  isA2UIFeatureEnabled,
  setA2UIFeatureEnabled,
  useA2UIFeatureEnabled,
} from '../node_modules/.cache/a2ui-feature-config/featureConfig.js';

function installDom() {
  const dom = new JSDOM('<!doctype html><div id="root"></div>', {
    virtualConsole: new VirtualConsole(),
  });
  const globals = {
    window: dom.window,
    document: dom.window.document,
    navigator: dom.window.navigator,
    Element: dom.window.Element,
    HTMLElement: dom.window.HTMLElement,
    Node: dom.window.Node,
    MutationObserver: dom.window.MutationObserver,
    getComputedStyle: dom.window.getComputedStyle.bind(dom.window),
    requestAnimationFrame: (callback) => setTimeout(callback, 0),
    cancelAnimationFrame: (id) => clearTimeout(id),
    IS_REACT_ACT_ENVIRONMENT: true,
  };
  const previousDescriptors = new Map();
  for (const [name, value] of Object.entries(globals)) {
    previousDescriptors.set(name, Object.getOwnPropertyDescriptor(globalThis, name));
    Object.defineProperty(globalThis, name, { configurable: true, writable: true, value });
  }
  return () => {
    for (const [name, descriptor] of previousDescriptors) {
      if (descriptor) Object.defineProperty(globalThis, name, descriptor);
      else delete globalThis[name];
    }
    dom.window.close();
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
          a2ui: {
            generating: 'Generating A2UI interface...',
            unavailable: 'A2UI unavailable',
            unavailableTitle: 'A2UI unavailable',
            retry: 'Try again',
            unsupportedProtocol: 'Unsupported protocol {{version}}',
          },
        },
      },
    },
  });
  return instance;
}

const VALID_A2UI_CONTENT = `<a2ui-json>
[
  {"beginRendering":{"surfaceId":"feature-test","root":"root"}},
  {"surfaceUpdate":{"surfaceId":"feature-test","components":[{"id":"root","component":{"Text":{"text":{"literalString":"Live A2UI content"}}}}]}}
]
</a2ui-json>`;

test('A2UI renderers react to feature changes without reloading the page', async () => {
  const restoreDom = installDom();
  setA2UIFeatureEnabled(true);
  const container = document.getElementById('root');
  const root = createRoot(container);
  let renderCount = 0;

  function Probe() {
    renderCount += 1;
    return createElement('span', null, useA2UIFeatureEnabled() ? 'enabled' : 'disabled');
  }

  try {
    await act(async () => root.render(createElement(Probe)));
    assert.equal(container.textContent, 'enabled');

    await act(async () => setA2UIFeatureEnabled(false));
    assert.equal(isA2UIFeatureEnabled(), false);
    assert.equal(container.textContent, 'disabled');
    assert.equal(renderCount, 2);

    await act(async () => setA2UIFeatureEnabled(false));
    assert.equal(renderCount, 2, 'setting the current value must not trigger a redundant render');
  } finally {
    await act(async () => root.unmount());
    setA2UIFeatureEnabled(true);
    restoreDom();
  }
});

test('mounted A2UI messages switch between text and interactive rendering immediately', async () => {
  const restoreDom = installDom();
  const i18n = createI18n();
  setA2UIFeatureEnabled(false);
  const container = document.getElementById('root');
  const root = createRoot(container);

  try {
    await act(async () => {
      root.render(
        createElement(
          A2UIProvider,
          { onAction: () => undefined },
          createElement(
            I18nextProvider,
            { i18n },
            createElement(A2UIMessageContent, {
              content: VALID_A2UI_CONTENT,
              messageId: 'feature-test-message',
            }),
          ),
        ),
      );
    });
    assert.equal(container.querySelectorAll('[data-testid="a2ui-surfaces"]').length, 0);

    await act(async () => {
      setA2UIFeatureEnabled(true);
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    assert.equal(container.querySelectorAll('[data-testid="a2ui-surfaces"]').length, 1);
    assert.match(container.textContent, /Live A2UI content/);

    await act(async () => setA2UIFeatureEnabled(false));
    assert.equal(container.querySelectorAll('[data-testid="a2ui-surfaces"]').length, 0);
  } finally {
    await act(async () => root.unmount());
    setA2UIFeatureEnabled(true);
    restoreDom();
  }
});
