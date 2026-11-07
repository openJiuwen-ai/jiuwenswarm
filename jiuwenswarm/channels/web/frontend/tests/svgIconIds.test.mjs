import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { act, createElement } from 'react';
import { createRoot } from 'react-dom/client';
import { JSDOM } from 'jsdom';
import {
  IconAgentPresetOutline16,
  IconCordisPluginOutline14,
  IconSettingsOutline14,
  IconSettingsOutline16,
} from '../node_modules/.cache/svg-icons/trajectoryIcons.mjs';

const assetPaths = [
  'agent-management/prompt-send.svg',
  'chat-overview.svg',
  'panel-collapse.svg',
  'settings/actions/refresh.svg',
  'settings/navigation/agent.svg',
  'share-export.svg',
  'sidebar/plus.svg',
  'view-horizontal.svg',
  'view-vertical.svg',
  'work-mode/back.svg',
  'work-mode/status-waiting.svg',
  'work-mode/unpin.svg',
  '智能体.svg',
];

function assertLocalReferences(document) {
  for (const element of document.querySelectorAll('svg, svg *')) {
    for (const attribute of element.attributes) {
      const references = [...attribute.value.matchAll(/url\(\s*['"]?#([^\s'"\)]+)['"]?\s*\)/g)]
        .map((match) => match[1]);
      if ((attribute.name === 'href' || attribute.name === 'xlink:href') && attribute.value.startsWith('#')) {
        references.push(attribute.value.slice(1));
      }
      for (const id of references) {
        const target = document.getElementById(id);
        assert.ok(target, `Missing SVG reference: ${id}`);
        assert.equal(target.closest('svg'), element.closest('svg'), `Cross-instance SVG reference: ${id}`);
        assert.equal([...document.querySelectorAll('svg [id]')].filter((node) => node.id === id).length, 1);
      }
    }
  }
}

test('repeated asset icons keep references local when an earlier instance is hidden', () => {
  const icons = assetPaths.map((path) => readFileSync(new URL(`../src/assets/${path}`, import.meta.url), 'utf8'));
  const dom = new JSDOM(`<main><div style="visibility:hidden">${icons.join('')}</div>${icons.join('')}${icons.join('')}</main>`);
  try {
    assert.equal(dom.window.document.querySelectorAll('svg').length, assetPaths.length * 3);
    assertLocalReferences(dom.window.document);
  } finally {
    dom.window.close();
  }
});

for (const Icon of [IconSettingsOutline14, IconSettingsOutline16, IconAgentPresetOutline16, IconCordisPluginOutline14]) {
  test(`${Icon.name} keeps local definitions and stable IDs across hiding and reordering`, () => {
    const dom = new JSDOM('<div id="root"></div>');
    const previous = new Map();
    for (const [key, value] of Object.entries({
      window: dom.window,
      document: dom.window.document,
      IS_REACT_ACT_ENVIRONMENT: true,
    })) {
      previous.set(key, Object.getOwnPropertyDescriptor(globalThis, key));
      Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
    }
    const root = createRoot(document.getElementById('root'));
    const render = (keys) => act(() => root.render(keys.map((key) => createElement('div', {
      key,
      'data-icon': key,
      style: { visibility: key === 'hidden' ? 'hidden' : 'visible' },
    }, createElement(Icon, { size: 24 })))));
    const definitionIds = (key) => [...document.querySelector(`[data-icon="${key}"]`).querySelectorAll('[id]')]
      .map((element) => element.id);
    try {
      render(['hidden', 'first', 'second']);
      const firstIds = definitionIds('first');
      const secondIds = definitionIds('second');
      assert.ok(firstIds.length > 0);
      assertLocalReferences(document);
      render(['second', 'first']);
      assert.deepEqual(definitionIds('first'), firstIds);
      assert.deepEqual(definitionIds('second'), secondIds);
      assertLocalReferences(document);
    } finally {
      act(() => root.unmount());
      dom.window.close();
      for (const [key, descriptor] of previous) {
        if (descriptor) Object.defineProperty(globalThis, key, descriptor);
        else delete globalThis[key];
      }
    }
  });
}
