import { build } from 'esbuild';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('..', import.meta.url));
await build({
  absWorkingDir: root,
  entryPoints: [
    'src/components/InteractionSlot/index.tsx',
    'src/components/InteractionSlot/promptRouting.ts',
    'src/i18n/index.ts',
    'src/stores/index.ts',
  ],
  outbase: 'src',
  outdir: 'node_modules/.cache/skill-package-prompt',
  bundle: true,
  splitting: true,
  packages: 'external',
  platform: 'node',
  format: 'esm',
  loader: { '.css': 'empty', '.svg': 'dataurl', '.png': 'dataurl' },
  define: { 'import.meta.env': '{"DEV":false}', 'import.meta.glob': '__skillPackagePromptTestGlob' },
  banner: {
    js: 'const __skillPackagePromptTestGlob = (pattern) => { if (!["./*.png", "./*.svg"].includes(pattern)) throw new Error("Unexpected Vite glob: " + pattern); return {}; };',
  },
});

const result = spawnSync(process.execPath, ['--test', 'tests/skillPackagePrompt.test.mjs'], {
  cwd: root,
  stdio: 'inherit',
});
if (result.error) throw result.error;
process.exitCode = result.status ?? 1;
