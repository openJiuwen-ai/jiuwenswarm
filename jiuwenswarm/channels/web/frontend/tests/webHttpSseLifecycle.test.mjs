import assert from 'node:assert/strict';
import test from 'node:test';

import {
  isChatSseTerminal,
  isSteerReceiptSseTerminal,
  isSteerSessionInputParams,
} from '../node_modules/.cache/web-http-sse-lifecycle/webHttpSseLifecycle.js';

test('steer session input is detected from input_mode / runtime_mode', () => {
  assert.equal(isSteerSessionInputParams({ input_mode: 'steer' }), true);
  assert.equal(isSteerSessionInputParams({ runtime_mode: 'STEER' }), true);
  assert.equal(isSteerSessionInputParams({ input_mode: 'follow_up' }), false);
  assert.equal(isSteerSessionInputParams({ query: 'hi' }), false);
});

test('steer runtime.accepted with input_boundary=stream ends chat SSE pump', () => {
  globalThis.window = { __JIUWENSWARM_EDITION__: 'enterprise' };
  const steerReceipt = {
    type: 'event',
    event: 'runtime.accepted',
    payload: { input_boundary: 'stream', request_id: 'r1' },
  };
  assert.equal(isSteerReceiptSseTerminal(steerReceipt), true);
  assert.equal(isChatSseTerminal(steerReceipt), true);
  assert.equal(
    isSteerReceiptSseTerminal({
      type: 'event',
      event: 'runtime.accepted',
      payload: { request_id: 'r1' },
    }),
    false
  );
  assert.equal(
    isChatSseTerminal({
      type: 'event',
      event: 'runtime.accepted',
      payload: { request_id: 'r1' },
    }),
    false
  );
});
