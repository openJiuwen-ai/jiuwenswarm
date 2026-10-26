import assert from 'node:assert/strict';
import test from 'node:test';

import {
  REQUEST_EXT_FIELDS,
  appendRuntimeScopeQuery,
  buildRuntimeIdentityHeaders,
  parseRuntimeScope,
} from '../node_modules/.cache/runtime-scope/services/runtimeScope.js';

test('runtime scope is parsed and added to websocket query', () => {
  const scope = parseRuntimeScope(
    '?user_id=%20u1%20&group_id=g1&bot_id=b1&gateway_id=gw1&ignored=x'
  );
  // 用户面忽略 URL 中的 gateway_id（遗留参数）；出厂注册表为空，ext 恒为 {}。
  assert.deepEqual(scope, {
    userId: 'u1',
    groupId: 'g1',
    botId: 'b1',
    ext: {},
  });
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
      { userId: 'u1', groupId: 'g1', botId: 'b1', ext: {} }
    ),
    {
      'X-Request-Id': 'req-1',
      'X-User-Id': 'u1',
      'X-Group-Id': 'g1',
      'X-Bot-Id': 'b1',
      'X-Session-Id': 'session-1',
      // 路由字段按原始名镜像发一份：request_ext 白名单按头名整名(小写)匹配，
      // X-User-Id 匹配不到白名单里的 user_id，镜像头是 ext 透传路由字段的载体。
      user_id: 'u1',
      group_id: 'g1',
      bot_id: 'b1',
    }
  );
});

test('missing runtime scope keeps legacy payload fallback', () => {
  assert.deepEqual(
    buildRuntimeIdentityHeaders(
      'req-2',
      { user_id: 'u2', group_id: 'g2', bot_id: 'b2' },
      { ext: {} }
    ),
    {
      'X-Request-Id': 'req-2',
      'X-User-Id': 'u2',
      'X-Group-Id': 'g2',
      'X-Bot-Id': 'b2',
      user_id: 'u2',
      group_id: 'g2',
      bot_id: 'b2',
    }
  );
});

test('registered request ext fields flow into query and headers', () => {
  // 模拟接入方按 JSDoc 示例启用字段：注册表追加一行（服务端白名单同步为部署动作）。
  REQUEST_EXT_FIELDS.push({ name: 'orgCode' });
  try {
    // URLSearchParams.get 自动解码：内存里存原始值，出站时才编码。
    const scope = parseRuntimeScope('?user_id=u1&orgCode=%E5%8C%97%E4%BA%AC');
    assert.deepEqual(scope.ext, { orgCode: '北京' });

    const query = appendRuntimeScopeQuery(new URLSearchParams(), scope);
    assert.equal(query.get('orgCode'), '北京');

    // 非 ASCII 值经 encodeURIComponent 编码后放入 header，不进 URL query。
    const headers = buildRuntimeIdentityHeaders('req-3', {}, scope);
    assert.equal(headers.orgCode, encodeURIComponent('北京'));

    // scope 缺失时回退业务参数。
    const payloadHeaders = buildRuntimeIdentityHeaders(
      'req-4',
      { orgCode: 'SH002' },
      { userId: 'u1', ext: {} }
    );
    assert.equal(payloadHeaders.orgCode, 'SH002');

    // 未注册字段不透传（白名单外键值在 pickExt 处丢弃）。
    const unregistered = buildRuntimeIdentityHeaders(
      'req-5',
      {},
      { userId: 'u1', ext: { orgCode: 'x', hacker: 'y' } }
    );
    assert.equal(unregistered.hacker, undefined);
    assert.deepEqual(
      { ...runtimeScopeExt(unregistered) },
      { orgCode: 'x' }
    );
  } finally {
    REQUEST_EXT_FIELDS.pop();
  }
});

function runtimeScopeExt(headers) {
  const { user_id, group_id, bot_id, ...rest } = headers;
  delete rest['X-Request-Id'];
  delete rest['X-User-Id'];
  delete rest['X-Group-Id'];
  delete rest['X-Bot-Id'];
  return rest;
}
