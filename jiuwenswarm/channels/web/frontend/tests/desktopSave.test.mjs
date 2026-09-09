import assert from 'node:assert/strict';
import test from 'node:test';

import {
  executeDesktopSave,
  saveBlob,
  saveBlobWithResult,
} from '../node_modules/.cache/desktop-save/desktopSave.mjs';

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

test('saveBlob reports explicit desktop cancellation and bridge failure', async () => {
  let restore = installDesktopGlobals({
    pywebview: {
      api: {
        begin_blob_save: async () => ({ ok: false, cancelled: true }),
        append_blob_save: async () => true,
        finish_blob_save: async () => ({ ok: true, cancelled: false }),
        abort_blob_save: async () => true,
      },
    },
  }, class FileReaderStub {});
  try {
    assert.deepEqual(
      await saveBlobWithResult(
        new Blob(['{}'], { type: 'application/json;charset=utf-8' }),
        'trajectory.archive.json',
      ),
      { outcome: 'cancelled', transport: 'desktop' },
    );
  } finally {
    restore();
  }

  const errors = [];
  const originalConsoleError = console.error;
  console.error = (...args) => errors.push(args);
  restore = installDesktopGlobals({ pywebview: { api: {} } }, class FileReaderStub {});
  try {
    assert.deepEqual(
      await saveBlobWithResult(new Blob(['{}']), 'trajectory.archive.json'),
      { outcome: 'failed', transport: 'desktop' },
    );
  } finally {
    restore();
    console.error = originalConsoleError;
  }
  assert.equal(errors.length, 1);
});

test('browser blob save dispatches a non-empty named download before revoking its URL', async () => {
  const clicks = [];
  const revoked = [];
  const timers = [];
  const originalDocument = globalThis.document;
  const originalCreateObjectUrl = URL.createObjectURL;
  const originalRevokeObjectUrl = URL.revokeObjectURL;
  const anchor = {
    click: () => clicks.push({ download: anchor.download, href: anchor.href }),
    remove: () => {},
    style: {},
  };
  globalThis.document = {
    body: { appendChild: () => {} },
    createElement: () => anchor,
  };
  URL.createObjectURL = blob => {
    assert.ok(blob.size > 0);
    return 'blob:trajectory-archive';
  };
  URL.revokeObjectURL = url => revoked.push(url);
  const restore = installDesktopGlobals({
    setTimeout: callback => {
      timers.push(callback);
      return 1;
    },
  }, class FileReaderStub {});
  try {
    const result = await saveBlobWithResult(
      new Blob(['{"archive_version":1}'], { type: 'application/json;charset=utf-8' }),
      'trajectory-session.archive.json',
    );
    assert.deepEqual(result, { outcome: 'saved', transport: 'browser-download' });
    assert.deepEqual(clicks, [{
      download: 'trajectory-session.archive.json',
      href: 'blob:trajectory-archive',
    }]);
    assert.deepEqual(revoked, []);
    timers[0]();
    assert.deepEqual(revoked, ['blob:trajectory-archive']);
  } finally {
    restore();
    globalThis.document = originalDocument;
    URL.createObjectURL = originalCreateObjectUrl;
    URL.revokeObjectURL = originalRevokeObjectUrl;
  }
});

test('browser file picker confirms save completion and preserves archive metadata', async () => {
  const writes = [];
  let closed = false;
  let pickerOptions;
  const restore = installDesktopGlobals({
    showSaveFilePicker: async (options) => {
      pickerOptions = options;
      return {
        createWritable: async () => ({
          write: async blob => writes.push(await blob.text()),
          close: async () => { closed = true; },
        }),
      };
    },
  }, class FileReaderStub {});
  try {
    const result = await saveBlobWithResult(
      new Blob(['{"format":"openjiuwen.trajectory.archive"}'], {
        type: 'application/json;charset=utf-8',
      }),
      'trajectory-session.archive.json',
      { preferBrowserFilePicker: true },
    );
    assert.deepEqual(result, { outcome: 'saved', transport: 'browser-file-picker' });
  } finally {
    restore();
  }
  assert.deepEqual(pickerOptions, {
    suggestedName: 'trajectory-session.archive.json',
    types: [{
      description: 'Export file',
      accept: { 'application/json': ['.json'] },
    }],
  });
  assert.deepEqual(writes, ['{"format":"openjiuwen.trajectory.archive"}']);
  assert.equal(closed, true);
});

test('browser file picker reports cancellation and write failure without anchor fallback', async () => {
  let anchorCreated = false;
  const originalDocument = globalThis.document;
  globalThis.document = { createElement: () => { anchorCreated = true; } };
  let restore = installDesktopGlobals({
    showSaveFilePicker: async () => {
      throw new DOMException('cancelled', 'AbortError');
    },
  }, class FileReaderStub {});
  try {
    assert.deepEqual(
      await saveBlobWithResult(
        new Blob(['{}']),
        'trajectory.archive.json',
        { preferBrowserFilePicker: true },
      ),
      { outcome: 'cancelled', transport: 'browser-file-picker' },
    );
  } finally {
    restore();
  }

  const errors = [];
  const originalConsoleError = console.error;
  console.error = (...args) => errors.push(args);
  restore = installDesktopGlobals({
    showSaveFilePicker: async () => ({
      createWritable: async () => ({
        write: async () => { throw new Error('disk full'); },
        close: async () => {},
      }),
    }),
  }, class FileReaderStub {});
  try {
    assert.deepEqual(
      await saveBlobWithResult(
        new Blob(['{}']),
        'trajectory.archive.json',
        { preferBrowserFilePicker: true },
      ),
      { outcome: 'failed', transport: 'browser-file-picker' },
    );
  } finally {
    restore();
    console.error = originalConsoleError;
    globalThis.document = originalDocument;
  }
  assert.equal(errors.length, 1);
  assert.equal(anchorCreated, false);
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
