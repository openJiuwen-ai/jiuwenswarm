import assert from 'node:assert/strict';
import test from 'node:test';
import { act, createElement } from 'react';
import { createRoot } from 'react-dom/client';
import { JSDOM } from 'jsdom';
import { webClient, dispatchWebUINodeEvent, RenderTree } from '../node_modules/.cache/webui-dispatch/dispatch.mjs';

const target = { session_id: 's1', slot_id: 'conversation.input.below', package_id: 'pkg.demo', instance_key: 'main' };

test('component events preserve the rendered declaration and put editable fields in data', async () => {
  const calls = [];
  const previous = webClient.request;
  webClient.request = async (...args) => { calls.push(args); };
  try {
    const node = { component: 'Input', events: { 'ui.change': { id: 'edit', args: { id: 5 } } } };
    await dispatchWebUINodeEvent(node, 'ui.change', target, 3, '0.1', { value: 'Beijing', isComposing: false });
    assert.deepEqual(calls, [['webui.dispatch', {
      event: 'ui.change', session_id: 's1', target,
      payload: { node_key: '0.1', revision: 3, action_id: 'edit', args: { id: 5 }, data: { value: 'Beijing', isComposing: false } },
    }]]);
    await dispatchWebUINodeEvent(node, 'ui.click', target, 3, '0.1', {});
    await dispatchWebUINodeEvent({ ...node, props: { disabled: true } }, 'ui.change', target, 3, '0.1', {});
    assert.equal(calls.length, 1);
  } finally {
    webClient.request = previous;
  }
});

test('in-flight duplicate suppression distinguishes node, revision and editable data', async () => {
  const calls = [];
  let release;
  const pending = new Promise((resolve) => { release = resolve; });
  const previous = webClient.request;
  webClient.request = async (...args) => { calls.push(args); await pending; };
  try {
    const node = { component: 'Button', action: { id: 'refresh', args: {} } };
    const requests = [
      dispatchWebUINodeEvent(node, 'ui.press', target, 1, '0'),
      dispatchWebUINodeEvent(node, 'ui.press', target, 1, '0'),
      dispatchWebUINodeEvent(node, 'ui.press', target, 1, '0.1'),
      dispatchWebUINodeEvent(node, 'ui.press', target, 2, '0'),
      dispatchWebUINodeEvent(node, 'ui.press', target, 1, '0', { value: 'changed' }),
    ];
    assert.equal(calls.length, 4);
    release();
    await Promise.all(requests);
  } finally {
    release();
    webClient.request = previous;
  }
});

test('the existing page renderer sends the path and revision of the clicked button', async () => {
  const dom = new JSDOM('<!doctype html><div id="root"></div>');
  const previousGlobals = new Map();
  for (const [name, value] of Object.entries({
    window: dom.window, document: dom.window.document, navigator: dom.window.navigator,
    HTMLElement: dom.window.HTMLElement, IS_REACT_ACT_ENVIRONMENT: true,
  })) {
    previousGlobals.set(name, Object.getOwnPropertyDescriptor(globalThis, name));
    Object.defineProperty(globalThis, name, { configurable: true, writable: true, value });
  }
  const root = createRoot(dom.window.document.getElementById('root'));
  const previousRequest = webClient.request;
  const calls = [];
  webClient.request = async (...args) => { calls.push(args); };
  try {
    await act(async () => root.render(createElement(RenderTree, {
      target, revision: 7,
      node: { component: 'Row', children: [
        { component: 'Text', props: { value: 'Title' } },
        { component: 'Button', props: { label: 'Refresh' }, events: { 'ui.click': { id: 'refresh' } } },
      ] },
    })));
    await act(async () => dom.window.document.querySelector('[data-testid="webui-button"]').click());
    assert.equal(calls.length, 1);
    assert.deepEqual(calls[0][1].payload, { node_key: '0.1', revision: 7, action_id: 'refresh', args: {}, data: {} });
    assert.equal(calls[0][1].event, 'ui.click');
  } finally {
    await act(async () => root.unmount());
    webClient.request = previousRequest;
    for (const [name, descriptor] of previousGlobals) {
      if (descriptor) Object.defineProperty(globalThis, name, descriptor);
      else delete globalThis[name];
    }
    dom.window.close();
  }
});
