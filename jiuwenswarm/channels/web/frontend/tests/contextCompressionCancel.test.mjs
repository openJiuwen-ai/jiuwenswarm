import test from 'node:test';
import assert from 'node:assert/strict';

import { contextCompressionInterruptedText } from '../node_modules/.cache/context-compression-cancel/contextCompression.mjs';

/** i18n 桩：原样回显 key，断言只关心命中的词条。 */
const t = (key) => `i18n:${key}`;

test('cancelled and paused map to interrupted copy', () => {
  assert.equal(
    contextCompressionInterruptedText(t, 'cancelled'),
    'i18n:chat.contextCompressionCancelled',
  );
  assert.equal(
    contextCompressionInterruptedText(t, 'paused'),
    'i18n:chat.contextCompressionPaused',
  );
});

test('other statuses fall back to null so callers keep the backend summary', () => {
  for (const status of ['completed', 'failed', 'running', 'unchanged', 'skipped', '', undefined]) {
    assert.equal(contextCompressionInterruptedText(t, status), null, String(status));
  }
});
