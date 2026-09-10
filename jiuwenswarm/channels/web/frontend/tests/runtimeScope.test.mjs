import assert from 'node:assert/strict';
import test from 'node:test';

import {
  appendRuntimeScopeQuery,
  buildRuntimeIdentityHeaders,
  parseRuntimeScope,
} from '../node_modules/.cache/runtime-scope/services/runtimeScope.js';

test('runtime scope is parsed and added to websocket query', () => {
  const scope = parseRuntimeScope(
    '?user_id=%20u1%20&group_id=g1&bot_id=b1&gateway_id=gw1&ignored=x'
  );
  // 用户面忽略 URL 中的 gateway_id（遗留参数）。
  assert.deepEqual(scope, { userId: 'u1', groupId: 'g1', botId: 'b1' });
  const query = appendRuntimeScopeQuery(new URLSearchParams('provider=p'), scope);
  assert.equal(query.toString(), 'provider=p&user_id=u1&group_id=g1&bot_id=b1');
});

test('runtime scope takes precedence in HTTP identity headers', () => {
  assert.deepEqual(
    buildRuntimeIdentityHeaders(
      'req-1',
      {
        user_id: 'payload-user',
        group_id: 'payload-group',
        bot_id: 'payload-bot',
        session_id: 'session-1',
      },
      { userId: 'u1', groupId: 'g1', botId: 'b1' }
    ),
    {
      'X-Request-Id': 'req-1',
      'X-User-Id': 'u1',
      'X-Group-Id': 'g1',
      'X-Bot-Id': 'b1',
      'X-Session-Id': 'session-1',
    }
  );
});

test('missing runtime scope keeps legacy payload fallback', () => {
  assert.deepEqual(
    buildRuntimeIdentityHeaders(
      'req-2',
      { user_id: 'u2', group_id: 'g2', bot_id: 'b2' },
      {}
    ),
    {
      'X-Request-Id': 'req-2',
      'X-User-Id': 'u2',
      'X-Group-Id': 'g2',
      'X-Bot-Id': 'b2',
    }
  );
});
