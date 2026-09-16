import assert from 'node:assert/strict';
import test from 'node:test';

import {
  generateUuidV4,
  prefixedMessageId,
} from '../node_modules/.cache/message-id/utils/uuid.js';

test('prefixedMessageId 保留既有前缀语义', () => {
  assert.match(prefixedMessageId('team-leader-'), /^team-leader-[0-9a-f-]{36}$/);
  assert.match(prefixedMessageId('assistant-'), /^assistant-[0-9a-f-]{36}$/);
  assert.match(prefixedMessageId('msg-final-'), /^msg-final-[0-9a-f-]{36}$/);
  assert.match(prefixedMessageId(''), /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
});

test('同一毫秒内集中生成的多条消息 ID 互不相同（历史恢复后集中释放事件场景）', () => {
  const ids = new Set(
    Array.from({ length: 500 }, () => prefixedMessageId('assistant-'))
  );
  assert.equal(ids.size, 500);
});

test('不同前缀之间不冲突，同前缀与裸 UUID 也不冲突', () => {
  const a = prefixedMessageId('team-leader-');
  const b = prefixedMessageId('msg-final-');
  const bare = generateUuidV4();
  assert.notEqual(a, b);
  assert.notEqual(a, bare);
  assert.notEqual(b, bare);
});

test('randomUUID 不可用时回退到 getRandomValues 仍保持唯一', () => {
  const ids = new Set(
    Array.from({ length: 200 }, () =>
      prefixedMessageId('assistant-', {
        getRandomValues(view) {
          for (let i = 0; i < view.length; i += 1) {
            view[i] = Math.floor(Math.random() * 256);
          }
          return view;
        },
      })
    )
  );
  assert.equal(ids.size, 200);
});
