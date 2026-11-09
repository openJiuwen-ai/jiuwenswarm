import test from 'node:test';
import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import { JSDOM } from 'jsdom';
import { PresentationWindow, parsePresentationRequest, isWindowTransitionOptions } from '../node_modules/.cache/application-presentation/protocol.mjs';

test('transition opt-in accepts bounded timing without artwork or arbitrary screenshot targets', () => {
  const request = params => ({ protocol: 'workswarm.presentation', version: 1, type: 'request', id: 1, action: 'workspace', params });
  assert.ok(parsePresentationRequest(request({ transition: { durationMs: 1120 } })));
  assert.ok(parsePresentationRequest(request({ conversationKey: 'a', transition: { durationMs: 2000 } })));
  for (const value of [null, undefined, {}, { durationMs: 0 }, { durationMs: Infinity }, { durationMs: 2001 },
    { durationMs: 1120, image: 'external' }, { durationMs: 200.5 }]) {
    assert.equal(isWindowTransitionOptions(value), false);
    assert.equal(parsePresentationRequest(request({ transition: value })), null);
  }
});

test('the sandbox bridge waits for both resource frames and rejects foreign or stale receipts', async () => {
  const dom = new JSDOM('<div id="root"></div>', { url: 'https://example.test' });
  const previous = new Map();
  for (const [key, value] of Object.entries({ window: dom.window, document: dom.window.document, navigator: dom.window.navigator,
    IS_REACT_ACT_ENVIRONMENT: true })) {
    previous.set(key, Object.getOwnPropertyDescriptor(globalThis, key));
    Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
  }
  const calls = [], messages = [];
  const descriptor = { id: 'transition-a', direction: 'compact', durationMs: 1120, image: 'data:image/png;base64,eA==',
    workspace: { x: 0, y: 0, width: 800, height: 600 }, surface: { x: 600, y: 0, width: 400, height: 720 },
    viewport: { width: 1000, height: 720 } };
  descriptor.workArea = { x: 0, y: 25, width: 1440, height: 875 };
  dom.window.jiuwenPresentation = {
    bootstrap: async () => ({ resourceUrl: '/resource.html', title: 'Resource', config: {}, state: { reducedMotion: false }, initialWorkArea: descriptor.workArea }),
    ready: async options => { calls.push(['ready', options]); return descriptor; },
    startTransition: async id => { calls.push(['start', id]); return true; },
    finishTransition: async id => { calls.push(['finish', id]); return true; },
    failed: async () => { calls.push(['failed']); },
    onUpdate: () => () => {}, onResult: () => () => {}, onTransition: () => () => {}, request: async () => true,
  };
  const root = createRoot(dom.window.document.getElementById('root'));
  try {
    await act(async () => root.render(React.createElement(PresentationWindow)));
    const frame = dom.window.document.querySelector('iframe');
    frame.contentWindow.postMessage = message => messages.push(message);
    const send = (type, extra = {}, source = frame.contentWindow) => dom.window.dispatchEvent(new dom.window.MessageEvent('message', {
      source, data: { protocol: 'workswarm.presentation', version: 1, type, ...extra },
    }));
    await act(async () => send('ready', { transition: { durationMs: 1120 } }));
    assert.equal(messages[0].type, 'state'); assert.equal(messages[1].type, 'transition');
    assert.deepEqual(messages[0].initialWorkArea, descriptor.workArea);
    assert.deepEqual(messages[1].transition.workArea, descriptor.workArea);
    assert.equal(calls.some(call => call[0] === 'start'), false);
    await act(async () => send('transition-ready', { id: descriptor.id }, dom.window));
    await act(async () => send('transition-ready', { id: 'stale' }));
    assert.equal(calls.some(call => call[0] === 'start'), false);
    await act(async () => send('transition-ready', { id: descriptor.id }));
    assert.equal(messages.at(-1).type, 'transition-start');
    await act(async () => send('transition-finished', { id: descriptor.id }));
    assert.equal(messages.at(-1).type, 'transition-ended');
    assert.deepEqual(calls.map(call => call[0]), ['ready', 'start', 'finish']);
  } finally {
    await act(async () => root.unmount()); dom.window.close();
    for (const [key, value] of previous) {
      if (value) Object.defineProperty(globalThis, key, value); else delete globalThis[key];
    }
  }
});
