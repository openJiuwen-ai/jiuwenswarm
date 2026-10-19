import assert from 'node:assert/strict';
import test from 'node:test';

import { coordinateSymphonyEnabledChange } from '../node_modules/.cache/symphony-graph-action/components/SkillPanel/symphonyGraphAction.js';

function graphActionInput(overrides = {}) {
  return {
    enabled: true,
    save: async () => true,
    getGraphPanel: () => null,
    request: async () => ({ success: true }),
    refreshFailedMessage: 'refresh failed',
    cancelFailedMessage: 'cancel failed',
    ...overrides,
  };
}

test('does not start a graph action when saving the switch rejects', async () => {
  let requests = 0;
  let builds = 0;
  const result = await coordinateSymphonyEnabledChange(graphActionInput({
    save: async () => { throw new Error('save failed'); },
    getGraphPanel: () => ({ startIncrementalBuild: async () => { builds += 1; }, cancelActiveBuild: async () => {} }),
    request: async () => { requests += 1; return { success: true }; },
  }));

  assert.equal(result.configSaveFailed, true);
  assert.equal(builds, 0);
  assert.equal(requests, 0);
});

test('does not start a graph action when restart is required', async () => {
  let requests = 0;
  const result = await coordinateSymphonyEnabledChange(graphActionInput({
    save: async () => false,
    request: async () => { requests += 1; return { success: true }; },
  }));

  assert.equal(result.appliedWithoutRestart, false);
  assert.equal(requests, 0);
  assert.equal(result.graphActionError, undefined);
});

test('uses a mounted graph panel exactly once without fallback', async () => {
  let builds = 0;
  let requests = 0;
  const result = await coordinateSymphonyEnabledChange(graphActionInput({
    getGraphPanel: () => ({ startIncrementalBuild: async () => { builds += 1; }, cancelActiveBuild: async () => {} }),
    request: async () => { requests += 1; return { success: true }; },
  }));

  assert.equal(result.graphActionError, undefined);
  assert.equal(builds, 1);
  assert.equal(requests, 0);
});

test('falls back to one incremental build request when the graph panel is unmounted', async () => {
  const requests = [];
  const result = await coordinateSymphonyEnabledChange(graphActionInput({
    request: async (method, params) => {
      requests.push([method, params]);
      return { success: true };
    },
  }));

  assert.equal(result.graphActionError, undefined);
  assert.deepEqual(requests, [['skills.graph.build', { force: false }]]);
});

test('re-reads the graph panel after saving so an unmounted panel uses the fallback error path', async () => {
  let currentPanel = { startIncrementalBuild: async () => { throw new Error('stale handle used'); }, cancelActiveBuild: async () => {} };
  let resolveSave;
  const save = new Promise((resolve) => { resolveSave = resolve; });
  const requests = [];
  const operation = coordinateSymphonyEnabledChange(graphActionInput({
    save: async () => save,
    getGraphPanel: () => currentPanel,
    request: async (method, params) => {
      requests.push([method, params]);
      return { success: false, detail: 'fallback failed' };
    },
  }));

  currentPanel = null;
  resolveSave(true);
  const result = await operation;

  assert.deepEqual(requests, [['skills.graph.build', { force: false }]]);
  assert.equal(result.graphActionError, 'fallback failed');
});

test('treats an idle fallback cancellation as a silent success', async () => {
  const result = await coordinateSymphonyEnabledChange(graphActionInput({
    enabled: false,
    request: async () => ({ success: false, build_status: 'idle' }),
  }));

  assert.equal(result.graphActionError, undefined);
});

test('keeps graph action failures separate from configuration save failures', async () => {
  const responseFailure = await coordinateSymphonyEnabledChange(graphActionInput({
    request: async () => ({ success: false, detail: 'backend rejected build' }),
  }));
  const exceptionFailure = await coordinateSymphonyEnabledChange(graphActionInput({
    request: async () => { throw new Error('network down'); },
  }));

  assert.equal(responseFailure.configSaveFailed, false);
  assert.equal(responseFailure.graphActionError, 'backend rejected build');
  assert.equal(exceptionFailure.configSaveFailed, false);
  assert.equal(exceptionFailure.graphActionError, 'network down');
});
