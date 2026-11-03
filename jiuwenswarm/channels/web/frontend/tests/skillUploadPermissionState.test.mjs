import assert from 'node:assert/strict';
import { test } from 'node:test';

// 由 npm run test:skill-upload-permission 先用 esbuild 打包到该路径。
const mod = await import(
  new URL('../node_modules/.cache/skill-upload-state/state.mjs', import.meta.url).href,
);
const { resolveUploadEntry, uploadEntryAction } = mod;

const state = (overrides = {}) => ({
  allowed: false,
  source: 'none',
  application_status: null,
  order_num: null,
  reject_comment: null,
  checked_at: null,
  ...overrides,
});

test('未授权且无申请时展示申请入口', () => {
  assert.equal(resolveUploadEntry(state(), 'idle'), 'apply');
  assert.equal(uploadEntryAction('apply'), 'apply');
});

test('已授权时展示上传入口', () => {
  const allowed = state({ allowed: true, source: 'user' });
  assert.equal(resolveUploadEntry(allowed, 'idle'), 'upload');
  assert.equal(uploadEntryAction('upload'), 'upload');
});

test('审批中仍是申请入口，不误放行为上传', () => {
  const pending = state({ application_status: 'pending', order_num: 'apr_1' });
  assert.equal(resolveUploadEntry(pending, 'idle'), 'pending');
  assert.equal(uploadEntryAction('pending'), 'apply');
});

test('被驳回后回到申请入口', () => {
  const rejected = state({ application_status: 'rejected', reject_comment: '理由不足' });
  assert.equal(resolveUploadEntry(rejected, 'idle'), 'apply');
});

test('资格服务不可用时失败关闭', () => {
  assert.equal(resolveUploadEntry(null, 'error'), 'unavailable');
  assert.equal(uploadEntryAction('unavailable'), 'apply');
  assert.equal(resolveUploadEntry(null, 'loading'), 'checking');
  assert.equal(uploadEntryAction('checking'), 'apply');
});
