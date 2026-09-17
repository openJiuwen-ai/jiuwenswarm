import assert from 'node:assert/strict';
import test from 'node:test';

import {
  stripUploadDocumentBlocks,
  toUploadDocumentHints,
  withUploadDocumentBlock,
} from '../node_modules/.cache/upload-document-block/utils/documentMessage.js';

test('withUploadDocumentBlock appends filename and path lines', () => {
  const result = withUploadDocumentBlock('总结这份文档', [
    { filename: 'spec.txt', path: '/uploads/spec.txt' },
  ]);

  assert.equal(result, '总结这份文档\n【上传文档】\n- spec.txt: /uploads/spec.txt');
});

test('withUploadDocumentBlock exposes the original file beside a sidecar path', () => {
  const result = withUploadDocumentBlock('总结这份 PDF', [
    { filename: '需求.pdf', path: '/uploads/需求.txt', originalPath: '/uploads/需求.pdf' },
  ]);

  assert.equal(
    result,
    '总结这份 PDF\n【上传文档】\n- 需求.pdf: /uploads/需求.txt (original file: /uploads/需求.pdf)',
  );
});

test('withUploadDocumentBlock omits originalPath when it equals path', () => {
  const result = withUploadDocumentBlock('看看', [
    { filename: 'spec.md', path: '/uploads/spec.md', originalPath: '/uploads/spec.md' },
  ]);

  assert.equal(result, '看看\n【上传文档】\n- spec.md: /uploads/spec.md');
});

test('stripUploadDocumentBlocks removes lines carrying an original file', () => {
  const withBlock = withUploadDocumentBlock('总结这份 PDF', [
    { filename: '需求.pdf', path: '/uploads/需求.txt', originalPath: '/uploads/需求.pdf' },
  ]);

  assert.equal(stripUploadDocumentBlocks(withBlock), '总结这份 PDF');
});

test('withUploadDocumentBlock lists documents without a path', () => {
  const result = withUploadDocumentBlock('看看', [{ filename: 'spec.txt' }]);

  assert.equal(result, '看看\n【上传文档】\n- spec.txt');
});

test('withUploadDocumentBlock replaces an existing block instead of stacking', () => {
  const first = withUploadDocumentBlock('看看', [{ filename: 'spec.txt' }]);
  const second = withUploadDocumentBlock(first, [
    { filename: 'spec.txt', path: '/uploads/spec.txt' },
  ]);

  assert.equal(second, '看看\n【上传文档】\n- spec.txt: /uploads/spec.txt');
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

test('toUploadDocumentHints keeps only document records with a filename', () => {
  const hints = toUploadDocumentHints([
    { type: 'document', filename: 'spec.txt', path: '/uploads/spec.txt' },
    { type: 'image', filename: 'shot.png', path: '/uploads/shot.png' },
    { type: 'document', path: '/uploads/report.pdf' },
    { type: 'document' },
    'not-an-object',
  ]);

  assert.deepEqual(hints, [
    { filename: 'spec.txt', path: '/uploads/spec.txt', originalPath: undefined },
    { filename: 'report.pdf', path: '/uploads/report.pdf', originalPath: undefined },
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
