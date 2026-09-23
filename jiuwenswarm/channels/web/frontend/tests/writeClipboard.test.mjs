import assert from 'node:assert/strict';
import test from 'node:test';
import { JSDOM } from 'jsdom';

import { writeClipboard } from '../node_modules/.cache/write-clipboard/writeClipboard.mjs';

function installDom({ clipboard, execCommand } = {}) {
  const dom = new JSDOM('<!doctype html><html><body></body></html>');
  const previous = {
    window: Object.getOwnPropertyDescriptor(globalThis, 'window'),
    document: Object.getOwnPropertyDescriptor(globalThis, 'document'),
    navigator: Object.getOwnPropertyDescriptor(globalThis, 'navigator'),
  };
  Object.defineProperty(globalThis, 'window', { configurable: true, value: dom.window });
  Object.defineProperty(globalThis, 'document', { configurable: true, value: dom.window.document });
  const navigatorValue = clipboard === undefined
    ? { ...dom.window.navigator }
    : { ...dom.window.navigator, clipboard };
  Object.defineProperty(globalThis, 'navigator', { configurable: true, value: navigatorValue });
  if (execCommand !== undefined) {
    dom.window.document.execCommand = execCommand;
  }
  return () => {
    for (const [name, descriptor] of Object.entries(previous)) {
      if (descriptor) Object.defineProperty(globalThis, name, descriptor);
      else delete globalThis[name];
    }
  };
}

test('returns true when the Clipboard API accepts the write', async () => {
  const writes = [];
  const restore = installDom({
    clipboard: { writeText: async (value) => { writes.push(value); } },
  });
  try {
    assert.equal(await writeClipboard('hello'), true);
    assert.deepEqual(writes, ['hello']);
  } finally {
    restore();
  }
});

test('returns false when the Clipboard API rejects, without claiming success', async () => {
  const restore = installDom({
    clipboard: { writeText: async () => { throw new Error('denied'); } },
  });
  try {
    assert.equal(await writeClipboard('secret'), false);
  } finally {
    restore();
  }
});

test('falls back to execCommand when clipboard is missing and honors its boolean', async () => {
  const restore = installDom({
    clipboard: null,
    execCommand: () => false,
  });
  try {
    assert.equal(await writeClipboard('fallback'), false);
  } finally {
    restore();
  }
});
