import assert from 'node:assert/strict';
import test from 'node:test';

import { MAX_TEXT_PREVIEW_BYTES, readLimitedTextPreview } from '../node_modules/.cache/text-preview/filePreviewModel.mjs';

function responseFromChunks(chunks) {
  let index = 0;
  let cancelled = false;
  const stream = new ReadableStream({
    pull(controller) {
      if (cancelled || index >= chunks.length) {
        controller.close();
        return;
      }
      controller.enqueue(chunks[index]);
      index += 1;
    },
    cancel() {
      cancelled = true;
    },
  });
  return {
    response: new Response(stream),
    wasCancelled: () => cancelled,
  };
}

test('keeps a text body that fits inside the preview cap', async () => {
  const result = await readLimitedTextPreview(new Response('hello'), 8);
  assert.equal(result.text, 'hello');
  assert.equal(result.truncated, false);
});

test('stops at the byte cap and cancels the remaining body', async () => {
  const { response, wasCancelled } = responseFromChunks([
    new Uint8Array(4).fill(0x61),
    new Uint8Array(4).fill(0x62),
    new Uint8Array(4).fill(0x63),
    new Uint8Array(4).fill(0x64),
  ]);
  const result = await readLimitedTextPreview(response, 8);
  assert.equal(result.truncated, true);
  assert.equal(result.text, 'aaaabbbb');
  assert.equal(wasCancelled(), true);
});

test('treats a body that ends exactly on the cap as complete', async () => {
  const { response, wasCancelled } = responseFromChunks([new Uint8Array(8).fill(0x61)]);
  const result = await readLimitedTextPreview(response, 8);
  assert.equal(result.text, 'aaaaaaaa');
  assert.equal(result.truncated, false);
  assert.equal(wasCancelled(), false);
});

test('drops a UTF-8 character split by the byte cap', async () => {
  const bytes = new TextEncoder().encode('你你');
  assert.equal(bytes.byteLength, 6);
  const result = await readLimitedTextPreview(new Response(bytes), 4);
  assert.equal(result.truncated, true);
  assert.equal(result.text, '你');
});

test('uses a 1 MiB default cap', () => {
  assert.equal(MAX_TEXT_PREVIEW_BYTES, 1024 * 1024);
});
