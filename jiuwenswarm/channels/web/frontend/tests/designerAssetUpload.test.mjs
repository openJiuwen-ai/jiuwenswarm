import assert from 'node:assert/strict';
import { File } from 'node:buffer';
import test from 'node:test';

import { webClient } from '../node_modules/.cache/designer-asset-upload/services/webClient.js';
import { uploadDesignerAsset } from '../node_modules/.cache/designer-asset-upload/features/designer/designerAssetUrl.js';

test('uploads binary chunks over the authenticated file RPC and uses the resolved destination', async (t) => {
  const content = Buffer.alloc(1024 * 1024 + 37);
  for (let i = 0; i < content.length; i++) content[i] = i % 256;
  const requests = [];
  t.mock.method(webClient, 'request', async (method, params) => {
    assert.equal(method, 'file.upload_chunk');
    requests.push(params);
    return { path: '/agent/uploads/designer/resolved.mp4' };
  });
  t.mock.method(globalThis, 'fetch', async () => new Response('{"error":"missing_token"}', { status: 400 }));
  const result = await uploadDesignerAsset(new File([content], 'clip.mp4', { type: 'video/mp4' }));
  assert.equal(result.path, '/agent/uploads/designer/resolved.mp4');
  assert.equal(result.filename, 'clip.mp4');
  assert.equal(result.mime_type, 'video/mp4');
  assert.ok(requests.length > 1);
  assert.ok(requests.every((item) => Buffer.from(item.data, 'base64').length <= 512 * 1024));
  assert.deepEqual(Buffer.concat(requests.map((item) => Buffer.from(item.data, 'base64'))), content);
  assert.equal(requests[0].resolved_path, undefined);
  assert.ok(requests.slice(1).every((item) => item.resolved_path === result.path));
  assert.ok(requests.slice(0, -1).every((item) => item.final === false));
  assert.equal(requests.at(-1).final, true);
  assert.equal(new Set(requests.map((item) => item.upload_id)).size, 1);
});

test('an upload failure stops subsequent chunks', async (t) => {
  let calls = 0;
  t.mock.method(webClient, 'request', async () => {
    calls++;
    throw new Error('upload destination unavailable');
  });
  t.mock.method(globalThis, 'fetch', async () => new Response('{"error":"missing_token"}', { status: 400 }));
  await assert.rejects(uploadDesignerAsset(new File([Buffer.alloc(1024 * 1024)], 'clip.mp4')), /destination unavailable/);
  assert.equal(calls, 1);
});

test('empty files are rejected before sending', async (t) => {
  const request = t.mock.method(webClient, 'request', async () => assert.fail('unexpected upload'));
  t.mock.method(globalThis, 'fetch', async () => new Response('{"error":"missing_token"}', { status: 400 }));
  await assert.rejects(uploadDesignerAsset(new File([], 'empty.png')), /empty/);
  assert.equal(request.mock.callCount(), 0);
});

test('files exceeding the existing 32 MiB limit are rejected before sending', async (t) => {
  const request = t.mock.method(webClient, 'request', async () => assert.fail('unexpected upload'));
  t.mock.method(globalThis, 'fetch', async () => new Response('{"error":"missing_token"}', { status: 400 }));
  await assert.rejects(uploadDesignerAsset(new File([Buffer.alloc(32 * 1024 * 1024 + 1)], 'large.mp4')), /file_too_large/);
  assert.equal(request.mock.callCount(), 0);
});
