// Build TeamPanel plus its service stubs with esbuild, then run the jsdom load-error tests.
import { build } from 'esbuild';
import { spawnSync } from 'node:child_process';
import { resolve } from 'node:path';

const outdir = resolve('node_modules/.cache/team-panel-load-error');

// Resolve TeamPanel's service imports to the controllable test stubs.
function stubServicesPlugin() {
  const stubs = [
    { filter: /services\/webClient$/, path: 'tests/fixtures/team-panel-load-error/webClientStub.ts' },
    { filter: /services\/teamApi$/, path: 'tests/fixtures/team-panel-load-error/teamApiStub.ts' },
    { filter: /(^|\/)edition$/, path: 'tests/fixtures/team-panel-load-error/editionStub.ts' },
  ];
  return {
    name: 'team-panel-stub-services',
    setup(build) {
      for (const stub of stubs) {
        build.onResolve({ filter: stub.filter }, () => ({ path: resolve(stub.path) }));
      }
    },
  };
}

await build({
  entryPoints: [
    'src/components/TeamPanel/index.tsx',
    'tests/fixtures/team-panel-load-error/webClientStub.ts',
  ],
  outbase: '.',
  outdir,
  bundle: true,
  splitting: true,
  format: 'esm',
  platform: 'node',
  packages: 'external',
  define: { 'import.meta.env': '{}' },
  loader: { '.css': 'empty', '.svg': 'dataurl', '.png': 'dataurl' },
  plugins: [stubServicesPlugin()],
  logLevel: 'warning',
});

const result = spawnSync(process.execPath, ['--test', 'tests/teamPanelLoadError.test.mjs'], { stdio: 'inherit' });
process.exitCode = result.status ?? 1;
