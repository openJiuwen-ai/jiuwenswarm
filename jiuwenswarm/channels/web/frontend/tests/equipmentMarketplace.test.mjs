import assert from 'node:assert/strict';
import test from 'node:test';

import {
  equipmentListFilter,
  normalizeEquipmentIdentity,
  normalizeEquipmentSource,
  resolvePluginPickerIdentifiers,
} from '../node_modules/.cache/equipment-marketplace/features/equipmentMarketplace.js';
import {
  deriveMcpAvailability,
  nextMcpQuickAction,
} from '../node_modules/.cache/equipment-marketplace/components/ConnectorMarket/mcpState.js';

test('selects the backend-owned marketplace filter for each asset page', () => {
  assert.equal(equipmentListFilter('agent', 'catalog'), 'builtin+hub');
  assert.equal(equipmentListFilter('agent', 'mine'), 'mine');
  assert.equal(equipmentListFilter('plugin', 'catalog'), 'builtin+hub');
  assert.equal(equipmentListFilter('plugin', 'mine'), 'mine');
  assert.equal(equipmentListFilter('mcp', 'catalog'), 'builtin');
  assert.equal(equipmentListFilter('mcp', 'mine'), 'local');
});

test('normalizes Hub and local package identities without using display names', () => {
  assert.deepEqual(
    normalizeEquipmentIdentity({
      id: 'hub-asset-uuid',
      packageName: 'sales-data-analyst',
      name: 'ignored-fallback',
      source: 'hub',
    }),
    {
      id: 'hub-asset-uuid',
      hubAssetId: 'hub-asset-uuid',
      runtimePackageName: 'sales-data-analyst',
    },
  );
  assert.deepEqual(normalizeEquipmentIdentity({ id: 'local-package', source: 'local' }), {
    id: 'local-package',
    runtimePackageName: 'local-package',
  });
  assert.equal(normalizeEquipmentSource('hub', 'local'), 'hub');
  assert.equal(normalizeEquipmentSource('built_in', 'local'), 'builtin');
  assert.equal(normalizeEquipmentSource('customize', 'builtin'), 'local');
});

test('uses the runtime package name for session enablement while retaining the Hub asset id for marketplace operations', () => {
  assert.deepEqual(
    resolvePluginPickerIdentifiers({
      id: '33b04b95dac741728b8f6f8c440627e5',
      runtimePackageName: 'my-plugin',
    }),
    {
      marketplaceId: '33b04b95dac741728b8f6f8c440627e5',
      sessionPluginName: 'my-plugin',
    },
  );
  assert.deepEqual(
    resolvePluginPickerIdentifiers({
      id: 'content-creation',
      runtimePackageName: 'content-creation',
    }),
    {
      marketplaceId: 'content-creation',
      sessionPluginName: 'content-creation',
    },
  );
});

test('uses the explicit MCP installation state independently from connection state', () => {
  assert.deepEqual(deriveMcpAvailability(true, 'idle'), { installed: true, linked: false });
  assert.deepEqual(deriveMcpAvailability(false, 'connected'), { installed: false, linked: true });
  assert.equal(nextMcpQuickAction(false, 'idle'), 'install');
  assert.equal(nextMcpQuickAction(true, 'idle'), 'connect');
  assert.equal(nextMcpQuickAction(true, 'connected'), 'use');
  assert.equal(nextMcpQuickAction(true, 'connecting'), 'busy');
});
