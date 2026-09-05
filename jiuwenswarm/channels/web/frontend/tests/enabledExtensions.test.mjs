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
