import assert from 'node:assert/strict';
import test from 'node:test';

import {
  KNOWN_TARGET_KEYS,
  SELECTABLE_TARGET_KEYS,
} from '../node_modules/.cache/cron-target-keys/constants.mjs';

test('SELECTABLE_TARGET_KEYS delists unmaintained channels (#5747)', () => {
  assert.equal(SELECTABLE_TARGET_KEYS.includes('tui'), false);
  assert.equal(SELECTABLE_TARGET_KEYS.includes('wecom'), false);
  assert.equal(SELECTABLE_TARGET_KEYS.includes('wechat'), false);
  assert.equal(SELECTABLE_TARGET_KEYS.includes('xiaoyi'), false);
  assert.deepEqual([...SELECTABLE_TARGET_KEYS], ['web', 'feishu', 'dingtalk', 'whatsapp']);
});

test('KNOWN_TARGET_KEYS still labels historical tui/wecom/wechat jobs', () => {
  for (const id of ['web', 'tui', 'xiaoyi', 'feishu', 'dingtalk', 'whatsapp', 'wecom', 'wechat']) {
    assert.equal(KNOWN_TARGET_KEYS.includes(id), true, `expected known target: ${id}`);
  }
});
