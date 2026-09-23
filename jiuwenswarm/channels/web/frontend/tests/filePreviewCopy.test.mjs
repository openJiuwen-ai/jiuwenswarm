import assert from 'node:assert/strict';
import test from 'node:test';

import { formatJsonContent, getPreviewCopyText } from '../node_modules/.cache/file-preview-copy/filePreviewShared.mjs';

test('JSON copy uses the pretty-printed preview text, not the raw source', () => {
  const raw = '{"b":2,"a":1}';
  const copied = getPreviewCopyText('config.json', raw);
  assert.equal(copied, formatJsonContent(raw));
  assert.notEqual(copied, raw);
  assert.match(copied, /\n/);
});

test('JSON copy is case-insensitive on the file extension', () => {
  const raw = '{"ok":true}';
  assert.equal(getPreviewCopyText('Manifest.JSON', raw), formatJsonContent(raw));
});

test('invalid JSON copy keeps the original source, matching the preview fallback', () => {
  const raw = '{not json';
  assert.equal(getPreviewCopyText('broken.json', raw), raw);
});

test('non-JSON files copy the source unchanged', () => {
  const python = 'print({"a":1})';
  const markdown = '{"looks":"like json"}';
  assert.equal(getPreviewCopyText('script.py', python), python);
  assert.equal(getPreviewCopyText('notes.md', markdown), markdown);
});
