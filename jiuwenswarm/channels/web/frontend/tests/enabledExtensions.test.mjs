import assert from 'node:assert/strict';
import test from 'node:test';

import {
  buildExtensionSendPayload,
  restoreSessionEquipment,
} from '../node_modules/.cache/enabled-extensions/enabledExtensions.mjs';

test('unhydrated restored session omits extension fields', () => {
  assert.deepEqual(buildExtensionSendPayload('restored-session'), {});
});

test('server equipment snapshot makes explicit empty and selected states distinguishable', () => {
  restoreSessionEquipment('restored-session', { plugin_names: [], mcp: [] });
  assert.deepEqual(buildExtensionSendPayload('restored-session'), { plugin_names: [], mcp: [] });

  restoreSessionEquipment('restored-session', {
    plugin_names: ['note-extractor'],
    mcp: ['filesystem'],
  });
  assert.deepEqual(buildExtensionSendPayload('restored-session'), {
    plugin_names: ['note-extractor'],
    mcp: ['filesystem'],
  });
});

test('restoring equipment selects the mounted Agent without replacing a local choice', () => {
  const values = new Map();
  const previousStorage = globalThis.localStorage;
  globalThis.localStorage = {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: (key) => values.delete(key),
  };
  try {
    restoreSessionEquipment('fork-agent', { agent_template_name: 'expert-a' });
    assert.equal(JSON.parse(values.get('jiuwenclaw_agent_selection'))['fork-agent'], 'expert-a');

    restoreSessionEquipment('fork-agent', { agent_template_name: 'expert-b' });
    assert.equal(JSON.parse(values.get('jiuwenclaw_agent_selection'))['fork-agent'], 'expert-a');
  } finally {
    globalThis.localStorage = previousStorage;
  }
});
