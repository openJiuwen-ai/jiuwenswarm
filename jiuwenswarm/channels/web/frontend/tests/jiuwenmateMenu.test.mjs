import assert from 'node:assert/strict';
import test from 'node:test';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import { JSDOM } from 'jsdom';

import {
  MENU_GAP,
  VIEWPORT_MARGIN,
  computeMenuPosition,
} from '../node_modules/.cache/jiuwenmate-menu/components/menuPosition.js';
import { ActionMenu } from '../node_modules/.cache/jiuwenmate-menu/components/ActionMenu.js';

const VIEWPORT = { width: 1024, height: 768 };

const anchorOf = (left, top, right, bottom) => ({ left, top, right, bottom });
const rectOf = (left, top, right, bottom) => ({
  left,
  top,
  right,
  bottom,
  width: right - left,
  height: bottom - top,
  x: left,
  y: top,
});

test('opens below the trigger when the panel fits', () => {
  const position = computeMenuPosition(anchorOf(100, 100, 200, 120), { width: 100, height: 80 }, VIEWPORT, 'end');
  assert.deepEqual(position, { left: 100, top: 120 + MENU_GAP, placement: 'bottom' });
});

test('flips above the trigger when the bottom space is taken', () => {
  const position = computeMenuPosition(anchorOf(100, 700, 200, 740), { width: 100, height: 80 }, VIEWPORT, 'end');
  assert.deepEqual(position, { left: 100, top: 700 - MENU_GAP - 80, placement: 'top' });
});

test('clamps into the viewport when neither side fits', () => {
  const position = computeMenuPosition(anchorOf(100, 20, 200, 60), { width: 100, height: 700 }, VIEWPORT, 'end');
  assert.equal(position.placement, 'bottom');
  assert.equal(position.top, VIEWPORT.height - 700 - VIEWPORT_MARGIN);
  assert.ok(position.top >= VIEWPORT_MARGIN);
});

test('aligns the panel right edge with the trigger and clamps at the right edge', () => {
  const position = computeMenuPosition(anchorOf(900, 100, 1024, 120), { width: 200, height: 50 }, VIEWPORT, 'end');
  assert.equal(position.left, VIEWPORT.width - 200 - VIEWPORT_MARGIN);
});

test('aligns the panel left edge with the trigger when there is room', () => {
  const position = computeMenuPosition(anchorOf(100, 100, 140, 120), { width: 200, height: 50 }, VIEWPORT, 'start');
  assert.deepEqual(position, { left: 100, top: 120 + MENU_GAP, placement: 'bottom' });
});

test('aligns the panel left edge with the trigger and clamps at the right edge', () => {
  const position = computeMenuPosition(anchorOf(1000, 100, 1020, 120), { width: 200, height: 50 }, VIEWPORT, 'start');
  assert.equal(position.left, VIEWPORT.width - 200 - VIEWPORT_MARGIN);
});

test('panel is fixed to the trigger, stays inside the menu root, and follows scroll and resize', async () => {
  const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'http://localhost' });
  // This case swaps four globals; keep each one as a plain entry so a single
  // loop can put them all back afterwards.
  const swappedGlobals = [
    { key: 'window', original: globalThis.window },
    { key: 'document', original: globalThis.document },
    { key: 'IS_REACT_ACT_ENVIRONMENT', original: globalThis.IS_REACT_ACT_ENVIRONMENT },
    { key: 'ResizeObserver', original: globalThis.ResizeObserver },
  ];
  const putGlobalBack = ({ key, original }) => {
    if (original === undefined) Reflect.deleteProperty(globalThis, key);
    else globalThis[key] = original;
  };
  globalThis.window = dom.window;
  globalThis.document = dom.window.document;
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const observers = [];
  globalThis.ResizeObserver = class FakeResizeObserver {
    constructor(callback) {
      this.callback = callback;
      this.targets = [];
      this.disconnected = false;
      observers.push(this);
    }

    observe(target) {
      this.targets.push(target);
    }

    disconnect() {
      this.disconnected = true;
    }
  };
  const rects = new WeakMap();
  dom.window.Element.prototype.getBoundingClientRect = function getBoundingClientRect() {
    return rects.get(this) ?? rectOf(0, 0, 0, 0);
  };

  const container = dom.window.document.getElementById('root');
  const root = createRoot(container);
  try {
    // The menu lives inside a scroll container, like the schedule board table.
    await act(async () => {
      root.render(
        React.createElement(
          'div',
          { id: 'scroller', style: { overflow: 'auto' } },
          React.createElement(ActionMenu, {
            label: '更多',
            testId: 'jiuwenmate-test-menu-trigger',
            align: 'end',
            // The schedule board is the caller that opts into a viewport-anchored
            // panel; the default keeps the list in normal flow.
            floating: true,
            items: [{ key: 'delete', label: '删除', onSelect: () => undefined }],
          }),
        ),
      );
    });
    const scroller = container.querySelector('#scroller');
    const trigger = container.querySelector('[data-testid="jiuwenmate-test-menu-trigger"]');
    assert.ok(trigger, 'Missing menu trigger');
    rects.set(trigger, rectOf(300, 200, 400, 224));

    await act(async () => {
      trigger.click();
    });

    const list = container.querySelector('[data-testid="jiuwenmate-menu-list"]');
    assert.ok(list, 'Missing menu panel');
    assert.equal(list.style.position, 'fixed');
    assert.equal(list.style.left, '400px');
    assert.equal(list.style.top, `${224 + MENU_GAP}px`);
    // No portal: the panel stays a descendant of the menu root, so the existing
    // outside-click handler still treats menu item clicks as inside.
    assert.equal(list.parentElement, container.querySelector('.jiuwenmate-menu'));

    // A scroll event does not bubble, so only the window's capture-phase
    // listener can catch the pane scrolling under the trigger.
    rects.set(trigger, rectOf(300, 100, 400, 124));
    await act(async () => {
      scroller.dispatchEvent(new dom.window.Event('scroll'));
    });
    assert.equal(list.style.left, '400px');
    assert.equal(list.style.top, `${124 + MENU_GAP}px`);

    // Layout-only moves (no scroll, no window resize) come from the trigger
    // resizing; the observer must be watching it and re-place on its callback.
    assert.equal(observers.length, 1);
    assert.deepEqual(observers[0].targets, [trigger]);
    rects.set(trigger, rectOf(320, 150, 420, 174));
    await act(async () => {
      observers[0].callback([]);
    });
    assert.equal(list.style.left, '420px');
    assert.equal(list.style.top, `${174 + MENU_GAP}px`);

    await act(async () => {
      trigger.click();
    });
    assert.equal(container.querySelector('[data-testid="jiuwenmate-menu-list"]'), null);
    assert.equal(observers[0].disconnected, true);
  } finally {
    await act(async () => root.unmount());
    dom.window.close();
    swappedGlobals.forEach(putGlobalBack);
  }
});
