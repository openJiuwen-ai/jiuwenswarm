import assert from 'node:assert/strict';
import test from 'node:test';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { build } from 'esbuild';
import { act, createElement } from 'react';
import { createRoot } from 'react-dom/client';
import { JSDOM } from 'jsdom';

const outfile = resolve('node_modules/.cache/team-progress-animation/hook.mjs');
await build({ entryPoints: ['src/components/teamArea/useTweenedPercent.ts'], outfile, bundle: true, packages: 'external', platform: 'node', format: 'esm' });
const { useTweenedPercent } = await import(pathToFileURL(outfile));

test('switching context resets the value and reduced motion stops an active animation', async () => {
  const dom = new JSDOM('<div id="root"></div>');
  const oldWindow = globalThis.window;
  const oldDocument = globalThis.document;
  globalThis.window = dom.window;
  globalThis.document = dom.window.document;
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  let listener;
  let scheduled;
  let cancelled = false;
  dom.window.matchMedia = () => ({ matches: false, addEventListener: (_, fn) => { listener = fn; }, removeEventListener: () => {} });
  dom.window.requestAnimationFrame = (fn) => { scheduled = fn; return 1; };
  dom.window.cancelAnimationFrame = () => { cancelled = true; };
  const root = createRoot(document.getElementById('root'));
  function Probe({ value }) { return createElement('span', null, useTweenedPercent(value)); }
  const render = (key, value) => act(async () => root.render(createElement(Probe, { key, value })));
  try {
    await render('first', 100);
    await render('second', 0);
    assert.equal(document.querySelector('span').textContent, '0');
    await render('second', 100);
    assert.equal(typeof scheduled, 'function');
    await act(async () => listener({ matches: true }));
    assert.equal(cancelled, true);
    assert.equal(document.querySelector('span').textContent, '100');
  } finally {
    await act(async () => root.unmount());
    dom.window.close();
    globalThis.window = oldWindow;
    globalThis.document = oldDocument;
    delete globalThis.IS_REACT_ACT_ENVIRONMENT;
  }
});

test('animation advances through intermediate values and retargets from its displayed value', async (context) => {
  const dom = new JSDOM('<div id="root"></div>');
  const oldWindow = globalThis.window;
  const oldDocument = globalThis.document;
  globalThis.window = dom.window;
  globalThis.document = dom.window.document;
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  let now = 0;
  context.mock.method(performance, 'now', () => now);
  const frames = new Map();
  let nextFrame = 0;
  dom.window.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
  dom.window.requestAnimationFrame = (callback) => { frames.set(++nextFrame, callback); return nextFrame; };
  dom.window.cancelAnimationFrame = (id) => frames.delete(id);
  const root = createRoot(document.getElementById('root'));
  function Probe({ value }) { return createElement('span', null, useTweenedPercent(value)); }
  const render = (value) => act(async () => root.render(createElement(Probe, { value })));
  const displayed = () => Number(document.querySelector('span').textContent);
  const advance = (time) => act(async () => {
    now = time;
    const callbacks = [...frames.values()];
    frames.clear();
    callbacks.forEach((callback) => callback(now));
  });
  try {
    await render(0);
    await render(100);
    assert.equal(displayed(), 0);
    await advance(250);
    const intermediate = displayed();
    assert.ok(intermediate > 0 && intermediate < 100);
    await render(40);
    assert.equal(displayed(), intermediate);
    assert.equal(frames.size, 1);
    await advance(500);
    assert.ok(displayed() > 40 && displayed() < intermediate);
    await advance(750);
    assert.equal(displayed(), 40);
    assert.equal(frames.size, 0);
    await render(100);
    assert.equal(frames.size, 1);
  } finally {
    await act(async () => root.unmount());
    assert.equal(frames.size, 0);
    dom.window.close();
    globalThis.window = oldWindow;
    globalThis.document = oldDocument;
    delete globalThis.IS_REACT_ACT_ENVIRONMENT;
  }
});
