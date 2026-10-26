import assert from 'node:assert/strict';
import test from 'node:test';

import { executeDesktopSave } from '../node_modules/.cache/desktop-save/desktopSave.mjs';

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
