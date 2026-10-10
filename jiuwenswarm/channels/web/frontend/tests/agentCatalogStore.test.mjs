import assert from 'node:assert/strict';
import test from 'node:test';

import {
  ensureAgentCatalog,
  invalidateAgentCatalog,
  useAgentCatalogStore,
} from '../node_modules/.cache/agent-catalog-store/agentCatalogStore.mjs';

function deferred() {
  let resolve;
  const promise = new Promise((next) => {
    resolve = next;
  });
  return { promise, resolve };
}

test('deduplicates loads and ignores a stale result after invalidation', async () => {
  invalidateAgentCatalog();
  const first = deferred();
  const second = deferred();
  let calls = 0;

  try {
    const firstLoad = ensureAgentCatalog(() => {
      calls += 1;
      return first.promise;
    });
    const duplicateLoad = ensureAgentCatalog(() => {
      calls += 1;
      return second.promise;
    });
    assert.equal(calls, 1);
    assert.strictEqual(firstLoad, duplicateLoad);

    invalidateAgentCatalog();
    const currentLoad = ensureAgentCatalog(() => {
      calls += 1;
      return second.promise;
    });
    assert.equal(calls, 2);

    first.resolve([{ id: 'old-agent' }]);
    await firstLoad;
    assert.equal(useAgentCatalogStore.getState().catalog, null);

    second.resolve([{ id: 'new-agent' }]);
    await currentLoad;
    assert.deepEqual(useAgentCatalogStore.getState().catalog, [{ id: 'new-agent' }]);
  } finally {
    invalidateAgentCatalog();
  }
});
