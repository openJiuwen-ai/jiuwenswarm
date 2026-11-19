import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import { build } from 'esbuild';

// Execute the actual App callback with only its unrelated UI and RPC boundaries replaced.
const appSource = fs.readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8');
const startMarker = 'const fetchConfig = useCallback(';
const start = appSource.indexOf(startMarker) + startMarker.length;
const end = appSource.indexOf('}, [request, t, setAvailableModelGroups, setAvailableModels]);', start);
assert.ok(start >= startMarker.length && end > start, 'App configuration callback is present');
const callback = appSource.slice(start, end + 1);
assert.match(callback, /getMultiModelCollaborationRevision\(\)/);
assert.match(callback, /applyMultiModelCollaborationSnapshot\(/);
assert.doesNotMatch(callback, /setMultiModelCollaborationEnabled\(/);

const dependencyNames = [
  'request', 'setA2UIFeatureEnabled', 'normalizeA2UIEnabled', 'setRSIFeatureEnabled',
  'normalizeRSIEnabled',
  'normalizeTrajectoryDiagnosisEnabled', 'setTrajectoryDiagnosisEnabled',
  'setTeamOrganizationUiEnabled', 'setComfyuiWorkflowEnabled', 'setTeamScheduledDispatchEnabled', 'setServerConfig', 'setConfigError',
  'modelSetupGuideEvaluatedRef', 'shouldPreviewModelSetupGuide', 'isSetupGuideEnabled',
  'isEnterprise',
  'setActiveNav', 'setModelSetupGuideStep', 'setAvailableModels', 'setAvailableModelGroups',
  'parseModelGroups', 't',
];
const updateAnchor = appSource.indexOf('startupUpdateCheckRef.current = true;');
const updateStart = appSource.lastIndexOf('useEffect(', updateAnchor) + 'useEffect('.length;
const updateEnd = appSource.indexOf('}, [isConnected, request]);', updateStart);
assert.ok(updateAnchor > 0 && updateEnd > updateStart, 'App startup update effect is present');
const updateCallback = appSource.slice(updateStart, updateEnd + 1);
await build({
  stdin: {
    contents: `
      import { applyMultiModelCollaborationSnapshot, getMultiModelCollaborationRevision,
        isMultiModelCollaborationEnabled } from './src/features/multiModelCollaboration/featureConfig';
      export * from './src/features/multiModelCollaboration/featureConfig';
      export function createAppFetchConfig({ ${dependencyNames.join(', ')} }) { return ${callback}; }
      export function createAppStartupUpdateEffect({ isEnterprise, FEATURE_APP_UPDATER_UI, isConnected,
        startupUpdateCheckRef, window, request, CustomEvent }) { return ${updateCallback}; }
    `,
    resolveDir: process.cwd(),
    loader: 'ts',
  },
  bundle: true,
  platform: 'node',
  format: 'esm',
  packages: 'external',
  outfile: 'node_modules/.cache/app-collaboration-snapshot.mjs',
});
const { createAppFetchConfig, createAppStartupUpdateEffect, isMultiModelCollaborationEnabled, setMultiModelCollaborationEnabled } =
  await import('../node_modules/.cache/app-collaboration-snapshot.mjs');

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { resolve, promise };
}
function appFixture(reads, { enterprise = false, preview = false, guideEvaluated = true } = {}) {
  const serverConfigs = [];
  const dispatchSnapshots = [];
  const guideSteps = [];
  const noop = () => {};
  const dependencies = Object.fromEntries(dependencyNames.map((name) => [name, noop]));
  let index = 0;
  Object.assign(dependencies, {
    request: async (method) => {
      if (method === 'config.get') return reads[index++].promise;
      assert.equal(method, 'models.list');
      return { models: [], model_groups: [] };
    },
    setServerConfig: (config) => serverConfigs.push(config),
    setTeamScheduledDispatchEnabled: (enabled) => dispatchSnapshots.push(enabled),
    isEnterprise: () => enterprise,
    modelSetupGuideEvaluatedRef: { current: guideEvaluated },
    shouldPreviewModelSetupGuide: () => preview,
    isSetupGuideEnabled: (value) => value === true,
    setModelSetupGuideStep: (step) => guideSteps.push(step),
    parseModelGroups: (value) => value,
  });
  return { fetchConfig: createAppFetchConfig(dependencies), serverConfigs, dispatchSnapshots, guideSteps };
}

test('late App config read cannot revert a confirmed settings save in either feature store or serverConfig', async () => {
  setMultiModelCollaborationEnabled(false);
  const oldRead = deferred();
  const app = appFixture([oldRead]);
  const pending = app.fetchConfig();
  // The settings save has succeeded while App's earlier read is still in flight.
  setMultiModelCollaborationEnabled(true);
  oldRead.resolve({ multi_model_collaboration_enabled: false });
  await pending;
  assert.equal(isMultiModelCollaborationEnabled(), true);
  assert.equal(app.serverConfigs.at(-1).multi_model_collaboration_enabled, true);
});

test('a fresh App reload after a stale read still accepts the current server value', async () => {
  setMultiModelCollaborationEnabled(false);
  const oldRead = deferred();
  const freshRead = deferred();
  const app = appFixture([oldRead, freshRead]);
  const staleRequest = app.fetchConfig();
  setMultiModelCollaborationEnabled(true);
  oldRead.resolve({ multi_model_collaboration_enabled: false });
  await staleRequest;
  const freshRequest = app.fetchConfig();
  freshRead.resolve({ multi_model_collaboration_enabled: false });
  await freshRequest;
  assert.equal(isMultiModelCollaborationEnabled(), false);
  assert.equal(app.serverConfigs.at(-1).multi_model_collaboration_enabled, false);
});

test('an unchanged-revision App read enables collaboration normally', async () => {
  setMultiModelCollaborationEnabled(false);
  const read = deferred();
  const app = appFixture([read]);
  const pending = app.fetchConfig();
  read.resolve({ multi_model_collaboration_enabled: true });
  await pending;
  assert.equal(isMultiModelCollaborationEnabled(), true);
  assert.equal(app.serverConfigs.at(-1).multi_model_collaboration_enabled, true);
});

test('App config reads retain upstream scheduled dispatch synchronization alongside collaboration', async () => {
  setMultiModelCollaborationEnabled(false);
  const enabledRead = deferred();
  const disabledRead = deferred();
  const app = appFixture([enabledRead, disabledRead]);
  const enabling = app.fetchConfig();
  enabledRead.resolve({ multi_model_collaboration_enabled: true, team_scheduled_dispatch_enabled: true });
  await enabling;
  assert.deepEqual(app.dispatchSnapshots, [true]);
  assert.equal(isMultiModelCollaborationEnabled(), true);
  const disabling = app.fetchConfig();
  disabledRead.resolve({ multi_model_collaboration_enabled: true, team_scheduled_dispatch_enabled: false });
  await disabling;
  assert.deepEqual(app.dispatchSnapshots, [true, false]);
  assert.equal(app.serverConfigs.at(-1).team_scheduled_dispatch_enabled, false);
  assert.equal(isMultiModelCollaborationEnabled(), true);
});

for (const enterprise of [false, true]) {
  for (const preview of [false, true]) {
    test(`${enterprise ? 'enterprise skips' : 'personal retains'} model setup guide with preview=${preview}`, async () => {
      const read = deferred();
      const app = appFixture([read], { enterprise, preview, guideEvaluated: false });
      const pending = app.fetchConfig();
      read.resolve({ setup_guide_enabled: !preview });
      await pending;
      assert.deepEqual(app.guideSteps, enterprise ? [] : [1]);
      assert.equal(app.serverConfigs.length, 1);
    });
  }
}

for (const enterprise of [false, true]) {
  test(`${enterprise ? 'enterprise skips' : 'personal retains'} the startup update check`, async () => {
    const requests = [];
    const events = [];
    const timers = new Map();
    const ref = { current: false };
    const effect = createAppStartupUpdateEffect({
      isEnterprise: () => enterprise,
      FEATURE_APP_UPDATER_UI: true,
      isConnected: true,
      startupUpdateCheckRef: ref,
      window: {
        setTimeout: (callback, delay) => {
          assert.equal(delay, 5000);
          timers.set(1, callback);
          return 1;
        },
        clearTimeout: (id) => timers.delete(id),
        dispatchEvent: (event) => events.push(event),
      },
      request: async (method, params) => {
        requests.push({ method, params });
        return { update_available: false };
      },
      CustomEvent: class {
        constructor(type, { detail }) {
          this.type = type;
          this.detail = detail;
        }
      },
    });
    const cleanup = effect();
    if (enterprise) {
      assert.equal(ref.current, false);
      assert.equal(timers.size, 0);
      assert.equal(cleanup, undefined);
      assert.deepEqual(requests, []);
    } else {
      assert.equal(ref.current, true);
      assert.equal(timers.size, 1);
      timers.get(1)();
      await Promise.resolve();
      assert.deepEqual(requests, [{ method: 'updater.check', params: { manual: false } }]);
      assert.equal(events[0].type, 'jiuwenswarm:updater-status');
      assert.equal(effect(), undefined);
      cleanup();
      assert.equal(timers.size, 0);
    }
  });
}
