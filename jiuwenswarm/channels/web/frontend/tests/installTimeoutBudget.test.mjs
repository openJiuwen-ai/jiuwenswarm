import test from 'node:test';
import assert from 'node:assert/strict';
import { build } from 'esbuild';

// #6426: every equipment install walks two serial Hub requests plus a package
// download, so none of them may fall back to the web client's 15s default
// timeout, and none may use a budget below the server's 220s cap (a smaller
// client budget makes the request look failed while the server keeps
// installing). The expert install shares the same constant but its module
// graph pulls UI components, so it is not bundled here.
await build({
  entryPoints: [
    'src/services/installTimeouts.ts',
    'src/features/agentManagement/groupClient.ts',
    'src/services/pluginPackagesApi.ts',
  ],
  bundle: true,
  packages: 'external',
  platform: 'node',
  format: 'esm',
  outdir: 'node_modules/.cache/install-timeout-budget',
  outExtension: { '.js': '.mjs' },
  define: { 'import.meta.env': '{}' },
  plugins: [
    {
      name: 'install-timeout-budget',
      setup(builder) {
        const mocks = [
          { filter: /(^|\/)webClient$/, path: 'webClient' },
          { filter: /agentManagement\/adapter$/, path: 'adapter' },
          { filter: /features\/catalogCache$/, path: 'catalogCache' },
          { filter: /features\/equipmentMarketplace$/, path: 'equipmentMarketplace' },
          { filter: /features\/equipmentListRequest$/, path: 'equipmentListRequest' },
          { filter: /(^|\/)i18n$/, path: 'i18n' },
        ];
        for (const mock of mocks) {
          builder.onResolve({ filter: mock.filter }, () => ({ path: mock.path, namespace: 'mock' }));
        }
        const contents = {
          webClient: `export const webRequest = (method, params, options) => {
            globalThis.installRequests.push([method, params, options]);
            return Promise.resolve({});
          };`,
          adapter: `export const normalizeAgentGroupDetail = value => value;
            export const normalizeAgentGroupListItem = value => value;
            export const normalizeAgentFileContent = value => value;
            export const normalizeAgentFileTree = value => value;`,
          catalogCache: 'export const withCatalogCache = items => items;',
          equipmentMarketplace: `export const normalizeEquipmentSource = value => value;
            export const normalizeEquipmentIdentity = value => value;`,
          equipmentListRequest: 'export const requestEquipmentList = async () => ({ items: [] });',
          i18n: 'export default { language: "zh" };',
        };
        builder.onLoad({ filter: /.*/, namespace: 'mock' }, ({ path }) => ({
          contents: contents[path],
          loader: 'js',
        }));
      },
    },
  ],
});

const { INSTALL_REQUEST_TIMEOUT_MS } = await import(
  '../node_modules/.cache/install-timeout-budget/services/installTimeouts.mjs'
);
const { createLiveAgentGroupManagementClient } = await import(
  '../node_modules/.cache/install-timeout-budget/features/agentManagement/groupClient.mjs'
);
const { pluginPackagesApi } = await import(
  '../node_modules/.cache/install-timeout-budget/services/pluginPackagesApi.mjs'
);

test('install timeout budget is generous enough for a Hub install', () => {
  assert.equal(INSTALL_REQUEST_TIMEOUT_MS, 240000);
});

test('group install passes the install budget instead of the 15s default', async () => {
  globalThis.installRequests = [];
  await createLiveAgentGroupManagementClient().installGroup('team-a');
  assert.deepEqual(globalThis.installRequests, [
    ['agent_groups.install', { id: 'team-a' }, { timeoutMs: 240000 }],
  ]);
});

test('plugin install uses the same budget so the server answers first', async () => {
  globalThis.installRequests = [];
  await pluginPackagesApi.install('plugin-a');
  assert.deepEqual(globalThis.installRequests, [
    ['plugin_packages.install', { id: 'plugin-a' }, { timeoutMs: 240000 }],
  ]);
});
