import assert from 'node:assert/strict';
import test from 'node:test';

import {
  readEquipmentCatalog,
  reconcileEquipmentCatalog,
  writeEquipmentCatalog,
} from '../node_modules/.cache/equipment-catalog/equipmentCatalogCache.js';
import {
  agentSummaryToDetail,
  pluginSummaryToDetail,
} from '../node_modules/.cache/equipment-catalog/equipmentDetailFallback.js';

let marketplaceRefresh = {};
try {
  marketplaceRefresh = await import('../node_modules/.cache/equipment-catalog/marketplaceRefresh.js');
} catch {
  // The regression test below describes the missing sequential refresh behavior.
}

let equipmentListRequest = {};
try {
  equipmentListRequest = await import('../node_modules/.cache/equipment-catalog/equipmentListRequest.js');
} catch {
  // The regression test below describes the missing list timeout policy.
}

class MemoryStorage {
  values = new Map();
  setCount = 0;

  getItem(key) {
    return this.values.get(key) ?? null;
  }

  setItem(key, value) {
    this.setCount += 1;
    this.values.set(key, value);
  }

  removeItem(key) {
    this.values.delete(key);
  }
}

const hubCard = { id: 'hub-note', source: 'hub', displayName: '笔记提取' };
const builtinCard = { id: 'builtin-office', source: 'builtin', displayName: '办公文档' };

test('catalog cache survives reload and ignores corrupt data', () => {
  const storage = new MemoryStorage();
  assert.equal(writeEquipmentCatalog('plugin', [hubCard], storage), true);
  assert.deepEqual(readEquipmentCatalog('plugin', storage), [hubCard]);

  storage.setItem('jiuwenswarm_equipment_catalog_v1_plugin', '{broken');
  assert.deepEqual(readEquipmentCatalog('plugin', storage), []);
});

test('catalog cache rejects oversized card collections instead of growing without bound', () => {
  const storage = new MemoryStorage();
  const tooManyCards = Array.from({ length: 501 }, (_, index) => ({
    id: `hub-${index}`,
    source: 'hub',
    displayName: `Hub ${index}`,
  }));

  assert.equal(writeEquipmentCatalog('mcp', tooManyCards, storage), false);
  assert.deepEqual(readEquipmentCatalog('mcp', storage), []);

  const oversizedCard = { id: 'large', source: 'hub', displayName: 'x'.repeat(513 * 1024) };
  assert.equal(writeEquipmentCatalog('mcp', [oversizedCard], storage), false);
  assert.deepEqual(readEquipmentCatalog('mcp', storage), []);

  storage.setItem(
    'jiuwenswarm_equipment_catalog_v1_mcp',
    JSON.stringify({ version: 1, savedAt: 1, items: [oversizedCard] }),
  );
  assert.deepEqual(readEquipmentCatalog('mcp', storage), []);
});

test('connector cache drops embedded image data instead of rejecting otherwise small summaries', () => {
  const storage = new MemoryStorage();
  const card = {
    id: 'ssh-mcp-server',
    source: 'builtin',
    displayName: 'SSH 远程访问',
    icon: `data:image/png;base64,${'a'.repeat(600 * 1024)}`,
  };

  assert.equal(writeEquipmentCatalog('mcp', [card], storage), true);
  assert.deepEqual(readEquipmentCatalog('mcp', storage), [{ ...card, icon: null }]);
  assert.match(card.icon, /^data:image\/png;base64,/);
});

test('unchanged catalog data does not rewrite local storage during background polling', () => {
  const storage = new MemoryStorage();
  storage.setItem(
    'jiuwenswarm_equipment_catalog_v1_agent',
    JSON.stringify({ version: 1, savedAt: 1, items: [hubCard] }),
  );
  storage.setCount = 0;

  assert.equal(writeEquipmentCatalog('agent', [hubCard], storage), true);
  assert.equal(storage.setCount, 0);
});

test('refresh keeps cached Hub cards when a degraded response only contains local cards', () => {
  assert.deepEqual(
    reconcileEquipmentCatalog([hubCard, builtinCard], [{ ...builtinCard, displayName: '办公文档新版' }]),
    [{ ...builtinCard, displayName: '办公文档新版' }, hubCard],
  );
});

test('refresh replaces cached Hub cards when Hub returns a new authoritative list', () => {
  const freshHubCard = { id: 'hub-summary', source: 'hub', displayName: '内容摘要' };
  assert.deepEqual(reconcileEquipmentCatalog([hubCard, builtinCard], [builtinCard, freshHubCard]), [
    builtinCard,
    freshHubCard,
  ]);
});

test('agent summary provides visible detail content before agent_templates.show completes', () => {
  const detail = agentSummaryToDetail({
    id: 'asset-agent',
    runtimePackageName: 'runtime-agent',
    hubAssetId: 'asset-agent',
    displayName: '项目专家',
    description: '根据项目材料给出建议',
    category: 'Efficiency',
    source: 'hub',
    installed: false,
    connectionState: 'disconnected',
    tags: [{ id: 'project', label: '项目管理' }],
    avatarUrl: 'https://hub.example/agent.png',
    version: '1.2.0',
  });

  assert.equal(detail.displayName, '项目专家');
  assert.equal(detail.details, '根据项目材料给出建议');
  assert.deepEqual(detail.tags, [{ id: 'project', label: '项目管理' }]);
  assert.deepEqual(detail.skills, []);
  assert.deepEqual(detail.pendingConnectors, []);
});

test('plugin summary provides visible detail content before plugin_packages.show completes', () => {
  const detail = pluginSummaryToDetail({
    id: 'asset-plugin',
    runtimePackageName: 'runtime-plugin',
    hubAssetId: 'asset-plugin',
    displayName: { zh: '笔记提取', en: 'Note extractor' },
    displayDescription: { zh: '提取结构化笔记', en: 'Extract structured notes' },
    category: 'Productivity',
    source: 'hub',
    installed: false,
    connectionState: 'disconnected',
    version: '2.0.0',
  });

  assert.deepEqual(detail.displayName, { zh: '笔记提取', en: 'Note extractor' });
  assert.equal(detail.details, '提取结构化笔记');
  assert.deepEqual(detail.tags, []);
  assert.deepEqual(detail.mcps, []);
});

test('slow marketplace refresh schedules the next poll only after the current request settles', async () => {
  assert.equal(typeof marketplaceRefresh.startSequentialRefresh, 'function');

  const pending = [];
  const calls = [];
  const scheduled = [];
  const refresh = (options) => {
    calls.push(options);
    return new Promise((resolve) => pending.push(resolve));
  };
  const schedule = (callback, delayMs) => {
    scheduled.push({ callback, delayMs });
    return scheduled.length;
  };

  const stop = marketplaceRefresh.startSequentialRefresh(refresh, 10_000, { schedule, cancel() {} });
  assert.deepEqual(calls, [undefined]);
  assert.equal(scheduled.length, 0);

  pending.shift()();
  await Promise.resolve();
  assert.equal(scheduled.length, 1);
  assert.equal(scheduled[0].delayMs, 10_000);

  scheduled.shift().callback();
  assert.deepEqual(calls, [undefined, { silent: true }]);
  assert.equal(scheduled.length, 0);

  pending.shift()();
  await Promise.resolve();
  assert.equal(scheduled.length, 1);
  stop();
});

test('equipment list requests wait long enough for the backend Hub fallback response', async () => {
  assert.equal(typeof equipmentListRequest.requestEquipmentList, 'function');

  let requestCall;
  const result = await equipmentListRequest.requestEquipmentList(
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
