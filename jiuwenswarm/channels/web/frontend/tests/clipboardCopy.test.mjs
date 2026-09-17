import assert from 'node:assert/strict';
import test from 'node:test';
import { JSDOM } from 'jsdom';

import { copyToClipboard } from '../node_modules/.cache/clipboard-copy/copyToClipboard.js';

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

function setupPage({ writeText, execCommand } = {}) {
  const dom = new JSDOM('<!doctype html><html><body></body></html>');
  const document = dom.window.document;
  const calls = { writeText: [], execCommand: [] };
  const clipboard = {
    writeText: async (text) => {
      calls.writeText.push(text);
      if (typeof writeText === 'function') return writeText(text);
      throw new Error('clipboard denied');
    },
  };
  if (execCommand !== undefined) {
    document.execCommand = (command) => {
      calls.execCommand.push(command);
      return execCommand(command, document);
    };
  }
  const restore = installGlobals({ document, navigator: { clipboard } });
  return { dom, document, calls, restore };
}

test('reports success when the host accepts the clipboard write', async () => {
  const expected = '/storage/Users/currentUser/.jiuwenswarm/agent/workspace/work/九问智能体/qiqi-20260916.md';
  const { dom, calls, restore } = setupPage({ writeText: () => undefined });
  try {
    assert.equal(await copyToClipboard(expected), true);
    // 闭环断言：host 收到的就是逐字节原文
    assert.deepEqual(calls.writeText, [expected]);
  } finally {
    restore();
    dom.window.close();
  }
});

test('falls back to execCommand and reports success when the fallback copies', async () => {
  const expected = 'x'.repeat(4096);
  let textareaDuringCopy;
  const { dom, document, calls, restore } = setupPage({
    writeText: () => { throw new Error('denied'); },
    execCommand: (_command, doc) => {
      textareaDuringCopy = doc.querySelector('body > textarea');
      return true;
    },
  });
  try {
    assert.equal(await copyToClipboard(expected), true);
    assert.deepEqual(calls.writeText, [expected]);
    assert.deepEqual(calls.execCommand, ['copy']);
    // 兜底路径必须把完整原文放进待复制的 textarea，而非界面截断文本
    assert.ok(textareaDuringCopy, 'fallback textarea must be attached to the body while copying');
    assert.equal(textareaDuringCopy.value, expected);
    // 复制结束不留 DOM 泄漏
    assert.equal(document.querySelector('body > textarea'), null);
  } finally {
    restore();
    dom.window.close();
  }
});

test('reports failure when the clipboard API rejects and the fallback returns false', async () => {
  const { dom, calls, restore } = setupPage({
    writeText: () => { throw new Error('denied'); },
    execCommand: () => false,
  });
  try {
    assert.equal(await copyToClipboard('will not make it'), false);
    assert.deepEqual(calls.writeText, ['will not make it']);
    assert.deepEqual(calls.execCommand, ['copy']);
  } finally {
    restore();
    dom.window.close();
  }
});

test('reports failure when the fallback throws instead of returning a boolean', async () => {
  const { dom, restore } = setupPage({
    writeText: () => { throw new Error('denied'); },
    execCommand: () => { throw new Error('not implemented'); },
  });
  try {
    assert.equal(await copyToClipboard('still no'), false);
  } finally {
    restore();
    dom.window.close();
  }
});
