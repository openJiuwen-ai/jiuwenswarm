import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';

import { persistWorkMode, readStoredWorkMode } from '../node_modules/.cache/work-mode-storage/features/workspace/workModeStorage.js';

const originalWindow = Object.getOwnPropertyDescriptor(globalThis, 'window');

afterEach(() => {
  if (originalWindow) {
    Object.defineProperty(globalThis, 'window', originalWindow);
  } else {
    delete globalThis.window;
  }
});

function setWindow(windowValue) {
  Object.defineProperty(globalThis, 'window', {
    configurable: true,
    value: windowValue,
  });
}

test('defaults to work and skips persistence without window', () => {
  delete globalThis.window;

  assert.equal(readStoredWorkMode(), 'work');
  assert.doesNotThrow(() => persistWorkMode('code'));
});

test('reads code and falls back to work for unknown stored values', () => {
  setWindow({ localStorage: { getItem: () => 'code' } });
  assert.equal(readStoredWorkMode(), 'code');

  setWindow({ localStorage: { getItem: () => 'invalid' } });
  assert.equal(readStoredWorkMode(), 'work');
});

test('falls back to work when storage access throws', () => {
  const windowValue = {};
  Object.defineProperty(windowValue, 'localStorage', {
    get() {
      throw new DOMException('storage blocked', 'SecurityError');
    },
  });
  setWindow(windowValue);

  assert.equal(readStoredWorkMode(), 'work');
});

test('falls back to work when reading storage throws', () => {
  setWindow({
    localStorage: {
      getItem() {
        throw new DOMException('storage blocked', 'SecurityError');
      },
    },
  });

  assert.equal(readStoredWorkMode(), 'work');
});

test('persists the selected mode when storage is available', () => {
  const writes = [];
  setWindow({
    localStorage: {
      setItem: (...args) => writes.push(args),
    },
  });

  persistWorkMode('code');
  assert.deepEqual(writes, [['jiuwenswarm_work_mode', 'code']]);
});

test('ignores storage access and write failures', () => {
  const windowValue = {};
  Object.defineProperty(windowValue, 'localStorage', {
    get() {
      throw new DOMException('storage blocked', 'SecurityError');
    },
  });
  setWindow(windowValue);
  assert.doesNotThrow(() => persistWorkMode('code'));

  setWindow({
    localStorage: {
      setItem() {
        throw new DOMException('quota exceeded', 'QuotaExceededError');
      },
    },
  });
  assert.doesNotThrow(() => persistWorkMode('code'));
});
