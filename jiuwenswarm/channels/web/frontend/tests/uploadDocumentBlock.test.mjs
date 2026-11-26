import assert from 'node:assert/strict';
import test from 'node:test';

import {
  stripUploadDocumentBlocks,
  toDisplaySessionTitle,
  toQueueContentPreview,
  toUploadDocumentHints,
  withUploadDocumentBlock,
} from '../node_modules/.cache/upload-document-block/utils/documentMessage.js';

test('withUploadDocumentBlock appends filename and path lines', () => {
  const result = withUploadDocumentBlock('总结这份文档', [
    { filename: 'spec.txt', path: '/uploads/spec.txt' },
  ]);

  assert.equal(result, '总结这份文档\n【上传文档】\n@/uploads/spec.txt');
});

test('withUploadDocumentBlock exposes the original file beside a sidecar path', () => {
  const result = withUploadDocumentBlock('总结这份 PDF', [
    { filename: '需求.pdf', path: '/uploads/需求.txt', originalPath: '/uploads/需求.pdf' },
  ]);

  assert.equal(
    result,
    '总结这份 PDF\n【上传文档】\n@/uploads/需求.pdf',
  );
});

test('withUploadDocumentBlock omits originalPath when it equals path', () => {
  const result = withUploadDocumentBlock('看看', [
    { filename: 'spec.md', path: '/uploads/spec.md', originalPath: '/uploads/spec.md' },
  ]);

  assert.equal(result, '看看\n【上传文档】\n@/uploads/spec.md');
});

test('stripUploadDocumentBlocks removes lines carrying an original file', () => {
  const withBlock = withUploadDocumentBlock('总结这份 PDF', [
    { filename: '需求.pdf', path: '/uploads/需求.txt', originalPath: '/uploads/需求.pdf' },
  ]);

  assert.equal(stripUploadDocumentBlocks(withBlock), '总结这份 PDF');
});

test('withUploadDocumentBlock lists documents without a path', () => {
  const result = withUploadDocumentBlock('看看', [{ filename: 'spec.txt' }]);

  assert.equal(result, '看看');
});

test('withUploadDocumentBlock replaces an existing block instead of stacking', () => {
  const first = withUploadDocumentBlock('看看', [{ filename: 'spec.txt' }]);
  const second = withUploadDocumentBlock(first, [
    { filename: 'spec.txt', path: '/uploads/spec.txt' },
  ]);

  assert.equal(second, '看看\n【上传文档】\n@/uploads/spec.txt');
  assert.equal(second.match(/【上传文档】/g).length, 1);
});

test('withUploadDocumentBlock without documents strips a stale block', () => {
  const withBlock = withUploadDocumentBlock('看看', [{ filename: 'spec.txt' }]);

  assert.equal(withUploadDocumentBlock(withBlock, []), '看看');
});

test('stripUploadDocumentBlocks removes the compact block', () => {
  const content = '总结这份文档\n【上传文档】\n- spec.txt: /uploads/spec.txt';

  assert.equal(stripUploadDocumentBlocks(content), '总结这份文档');
});

test('stripUploadDocumentBlocks preserves an inline upload marker in the query', () => {
  const content = '请解释“【上传文档】”这个词在普通文本中的含义';

  assert.equal(stripUploadDocumentBlocks(content), content);
});

test('stripUploadDocumentBlocks removes a legacy block with a named marker', () => {
  const content = '总结这份文档\n【上传文档：需求.pdf】\n/uploads/需求.pdf';

  assert.equal(stripUploadDocumentBlocks(content), '总结这份文档');
});

test('stripUploadDocumentBlocks removes a collapsed auto-title upload block', () => {
  assert.equal(stripUploadDocumentBlocks('【上传文档】 @/.../todo.json'), '');
  assert.equal(
    stripUploadDocumentBlocks('总结这份文档 【上传文档】 @/uploads/spec.txt'),
    '总结这份文档',
  );
});

test('toDisplaySessionTitle strips collapsed bad titles for the sidebar', () => {
  assert.equal(toDisplaySessionTitle('【上传文档】 @/.../todo.json'), '');
  assert.equal(
    toDisplaySessionTitle('总结\n【上传文档】\n@/uploads/spec.txt'),
    '总结',
  );
  assert.equal(
    toDisplaySessionTitle('总结 【上传文档】 @/uploads/spec.txt'),
    '总结',
  );
});

test('toQueueContentPreview prefers stripped query text over the protocol block', () => {
  assert.equal(
    toQueueContentPreview('总结这份文档\n【上传文档】\n@/uploads/spec.txt'),
    '总结这份文档',
  );
  assert.equal(
    toQueueContentPreview('总结 【上传文档】 @/uploads/spec.txt'),
    '总结',
  );
});

test('toQueueContentPreview falls back to media filename for pure uploads', () => {
  assert.equal(
    toQueueContentPreview('【上传文档】\n@/uploads/todo.json', {
      mediaItems: [{ filename: 'todo.json' }],
      fallbackLabel: 'Uploaded document',
    }),
    'todo.json',
  );
});

test('toQueueContentPreview falls back to @path basename then localized label', () => {
  assert.equal(
    toQueueContentPreview('【上传文档】\n@/uploads/todo.json'),
    'todo.json',
  );
  assert.equal(
    toQueueContentPreview('【上传文档】\n@"/u/my brief.pdf"'),
    'my brief.pdf',
  );
  assert.equal(
    toQueueContentPreview('【上传文档】', { fallbackLabel: 'Uploaded document' }),
    'Uploaded document',
  );
  assert.equal(toQueueContentPreview('【上传文档】'), '');
});

test('toUploadDocumentHints keeps only document records with a filename', () => {
  const hints = toUploadDocumentHints([
    { type: 'document', filename: 'spec.txt', path: '/uploads/spec.txt' },
    { type: 'image', filename: 'shot.png', path: '/uploads/shot.png' },
    { type: 'document', path: '/uploads/report.pdf' },
    { type: 'document' },
    'not-an-object',
  ]);

  assert.deepEqual(hints, [
    { filename: 'spec.txt', path: '/uploads/spec.txt', originalPath: '/uploads/spec.txt' },
    { filename: 'report.pdf', path: '/uploads/report.pdf', originalPath: '/uploads/report.pdf' },
  ]);
});

test('toUploadDocumentHints carries original_path from persisted records', () => {
  const hints = toUploadDocumentHints([
    {
      type: 'document',
      filename: '需求.pdf',
      path: '/uploads/需求.txt',
      original_path: '/uploads/需求.pdf',
    },
  ]);

  assert.deepEqual(hints, [
    { filename: '需求.pdf', path: '/uploads/需求.txt', originalPath: '/uploads/需求.pdf' },
  ]);
});

test('toUploadDocumentHints tolerates a non-array payload', () => {
  assert.deepEqual(toUploadDocumentHints(undefined), []);
});
