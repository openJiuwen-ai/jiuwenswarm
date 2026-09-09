import assert from 'node:assert/strict';
import test from 'node:test';

import { requestEquipmentList } from '../node_modules/.cache/equipment-list-request/equipmentListRequest.js';

test('equipment list requests wait long enough for the backend Hub fallback response', async () => {
  let requestCall;
  const result = await requestEquipmentList(
    (method, params, options) => {
      requestCall = { method, params, options };
      return Promise.resolve({ items: [] });
    },
    'mcp.list',
    { filter: 'builtin' },
  );

  assert.deepEqual(result, { items: [] });
  assert.deepEqual(requestCall, {
    method: 'mcp.list',
    params: { filter: 'builtin' },
    options: { timeoutMs: 75_000 },
  });
});
