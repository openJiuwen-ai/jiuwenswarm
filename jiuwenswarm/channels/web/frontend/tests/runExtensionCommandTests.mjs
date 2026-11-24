import { build } from 'esbuild';
import { spawnSync } from 'node:child_process';

await build({
  entryPoints: ['tests/fixtures/extensionCommandExports.ts'],
  bundle: true,
  platform: 'node',
  format: 'esm',
  outfile: 'node_modules/.cache/extension-command/commands.mjs',
  define: { 'import.meta.env.DEV': 'false' },
});

const result = spawnSync(process.execPath, ['--test', 'tests/extensionCommand.test.mjs'], { stdio: 'inherit' });
if (result.error) throw result.error;
process.exitCode = result.status ?? 1;
