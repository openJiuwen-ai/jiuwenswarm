import assert from 'node:assert/strict';
import test from 'node:test';

import { executeDesktopSave, saveBlob } from '../node_modules/.cache/desktop-save/desktopSave.mjs';

function installDesktopGlobals(windowValue, fileReaderValue) {
  const hadWindow = Object.hasOwn(globalThis, 'window');
  const previousWindow = globalThis.window;
  const hadFileReader = Object.hasOwn(globalThis, 'FileReader');
  const previousFileReader = globalThis.FileReader;
  globalThis.window = windowValue;
  globalThis.FileReader = fileReaderValue;
  return () => {
    if (hadWindow) globalThis.window = previousWindow;
    else delete globalThis.window;
    if (hadFileReader) globalThis.FileReader = previousFileReader;
    else delete globalThis.FileReader;
  };
}

test('executeDesktopSave distinguishes saved, cancelled, and failed results', async () => {
  assert.equal(await executeDesktopSave(() => true), 'saved');
  assert.equal(await executeDesktopSave(() => ({ ok: true, cancelled: false })), 'saved');
  assert.equal(await executeDesktopSave(() => ({ ok: false, cancelled: true })), 'cancelled');
  assert.equal(await executeDesktopSave(() => ({ ok: false, cancelled: false })), 'failed');
});

test('executeDesktopSave converts rejected desktop API calls into failed results', async () => {
  const originalConsoleError = console.error;
  const errors = [];
  console.error = (...args) => errors.push(args);
  try {
    assert.equal(await executeDesktopSave(() => Promise.reject(new Error('bridge unavailable'))), 'failed');
  } finally {
    console.error = originalConsoleError;
  }
  assert.equal(errors.length, 1);
});

test('saveBlob sends large desktop exports as bounded sequential chunks', async () => {
  const chunkSizes = [];
  const appendedChunks = [];
  class FileReaderStub {
    readAsDataURL(blob) {
      chunkSizes.push(blob.size);
      this.result = 'data:application/octet-stream;base64,QQ==';
      queueMicrotask(() => this.onload());
    }
  }
  const api = {
    begin_blob_save: async () => ({ ok: true, cancelled: false, transfer_id: 'transfer-1' }),
    append_blob_save: async (...args) => {
      appendedChunks.push(args);
      return true;
    },
    finish_blob_save: async () => ({ ok: true, cancelled: false }),
    abort_blob_save: async () => true,
  };
  const restore = installDesktopGlobals({ pywebview: { api } }, FileReaderStub);
  try {
    const blob = new Blob([new Uint8Array(1024 * 1024 + 3)], { type: 'image/png' });
    assert.equal(await saveBlob(blob, 'share.png'), 'saved');
  } finally {
    restore();
  }

  assert.deepEqual(chunkSizes, [1024 * 1024, 3]);
  assert.deepEqual(appendedChunks, [
    ['transfer-1', 'QQ=='],
    ['transfer-1', 'QQ=='],
  ]);
});

test('saveBlob aborts the desktop transaction after a chunk failure', async () => {
  const abortedTransfers = [];
  class FileReaderStub {
    readAsDataURL() {
      this.result = 'data:application/octet-stream;base64,QQ==';
      queueMicrotask(() => this.onload());
    }
  }
  const api = {
    begin_blob_save: async () => ({ ok: true, cancelled: false, transfer_id: 'transfer-2' }),
    append_blob_save: async () => false,
    finish_blob_save: async () => {
      throw new Error('finish must not run');
    },
    abort_blob_save: async transferId => {
      abortedTransfers.push(transferId);
      return true;
    },
  };
  const restore = installDesktopGlobals({ pywebview: { api } }, FileReaderStub);
  try {
    assert.equal(await saveBlob(new Blob(['png'], { type: 'image/png' }), 'share.png'), 'failed');
  } finally {
    restore();
  }

  assert.deepEqual(abortedTransfers, ['transfer-2']);
});

test('saveBlob does not fall back to whole-file data URLs in desktop mode', async () => {
  const errors = [];
  const originalConsoleError = console.error;
  console.error = (...args) => errors.push(args);
  const restore = installDesktopGlobals({ pywebview: { api: { save_data_url: async () => ({ ok: true }) } } }, class FileReaderStub {});
  try {
    assert.equal(await saveBlob(new Blob(['png'], { type: 'image/png' }), 'share.png'), 'failed');
  } finally {
    restore();
    console.error = originalConsoleError;
  }
  assert.equal(errors.length, 1);
});
