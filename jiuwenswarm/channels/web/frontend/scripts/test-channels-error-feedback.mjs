import { build } from 'esbuild';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('..', import.meta.url));
await build({
  absWorkingDir: root,
  entryPoints: [
    'src/features/settings/modules/channels/SettingsChannelsPanel.tsx',
    'src/features/settings/services/SettingsServicesProvider.tsx',
  ],
  outbase: 'src',
  outdir: 'node_modules/.cache/channels-error-feedback',
  bundle: true,
  splitting: true,
  packages: 'external',
  platform: 'node',
  format: 'esm',
  loader: { '.css': 'empty', '.svg': 'dataurl', '.png': 'dataurl' },
  define: { 'import.meta.env': '{"DEV":false}' },
  plugins: [
    {
      name: 'channels-error-feedback-test-assets',
      setup(builder) {
        builder.onResolve({ filter: /\.svg\?react$/ }, ({ path }) => ({ path, namespace: 'svg-react-stub' }));
        builder.onLoad({ filter: /.*/, namespace: 'svg-react-stub' }, () => ({
          contents: 'export default function SvgStub() { return null; }',
          loader: 'js',
        }));
      },
    },
  ],
});
const testArgs = ['--test', ...process.argv.slice(2), 'tests/settingsChannelsErrorFeedback.test.mjs'];
const result = spawnSync(process.execPath, testArgs, { cwd: root, stdio: 'inherit' });
if (result.error) throw result.error;
process.exitCode = result.status ?? 1;
