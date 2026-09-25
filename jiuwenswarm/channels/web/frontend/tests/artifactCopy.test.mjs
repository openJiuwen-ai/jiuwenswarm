import assert from 'node:assert/strict';
import test from 'node:test';

import {
  formatArtifactPreviewText,
  getArtifactCopyText,
  previewKind,
} from '../node_modules/.cache/artifact-copy/filePreviewModel.mjs';

test('JSON copy uses the pretty-printed artifact preview, not the raw source', () => {
  const raw = '{"b":2,"a":1}';
  const kind = previewKind({ name: 'result.json' });
  const copied = getArtifactCopyText(kind, raw, 'result.json');
  assert.equal(copied, formatArtifactPreviewText(kind, raw));
  assert.notEqual(copied, raw);
  assert.match(copied, /\n/);
});

test('JSONL copy pretty-prints parsed lines as an array, matching the preview', () => {
  const raw = '{"n":1}\n{"n":2}\n';
  const kind = previewKind({ name: 'events.jsonl' });
  const copied = getArtifactCopyText(kind, raw, 'events.jsonl');
  assert.equal(copied, formatArtifactPreviewText('jsonl', raw));
  assert.equal(copied, JSON.stringify([{ n: 1 }, { n: 2 }], null, 2));
});

test('invalid JSON copy keeps the original source', () => {
  const raw = '{not json';
  assert.equal(formatArtifactPreviewText('json', raw), null);
  assert.equal(getArtifactCopyText('json', raw, 'broken.json'), raw);
});

test('markdown and images keep the previous copy contract', () => {
  const markdown = '# notes\n{"looks":"like json"}';
  assert.equal(getArtifactCopyText('markdown', markdown, 'notes.md'), markdown);
  assert.equal(getArtifactCopyText('image', 'binary', 'shot.png'), 'shot.png');
});
