import { build } from 'esbuild';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('..', import.meta.url));
await build({
  absWorkingDir: root,
  entryPoints: ['src/components/ChatPanel/InputArea.tsx', 'src/stores/index.ts', 'src/i18n/index.ts'],
  outbase: 'src',
  outdir: 'node_modules/.cache/input-area-permission-merge',
  bundle: true,
  splitting: true,
  packages: 'external',
  platform: 'node',
  format: 'esm',
  loader: { '.css': 'empty', '.svg': 'dataurl', '.png': 'dataurl' },
  define: { 'import.meta.env': '{"DEV":false}', 'import.meta.glob': '__inputAreaAssetGlob' },
  banner: {
    // Only decorative assets and optional extension discovery need Vite here.
    js: 'const __inputAreaAssetGlob = (pattern) => { if (!["./*.png", "./*.svg", "../../../../../extensions/*/frontend/index.tsx"].includes(pattern)) throw new Error("Unexpected Vite glob: " + pattern); return {}; };',
  },
  plugins: [
    {
      name: 'input-area-test-assets',
      setup(builder) {
        builder.onResolve({ filter: /\.svg\?react$/ }, ({ path }) => ({ path, namespace: 'svg-react-stub' }));
        builder.onLoad({ filter: /.*/, namespace: 'svg-react-stub' }, () => ({
          contents: 'export default function SvgStub() { return null; }',
          loader: 'js',
        }));
        builder.onResolve({ filter: /^\/logo\.svg$/ }, () => ({ path: 'logo.svg', namespace: 'asset-url-stub' }));
        builder.onLoad({ filter: /.*/, namespace: 'asset-url-stub' }, () => ({
          contents: 'export default "logo.svg";',
          loader: 'js',
        }));
      },
    },
  ],
});
const result = spawnSync(process.execPath, ['--test', 'tests/inputAreaPermissionMerge.test.mjs'], {
  cwd: root,
  stdio: 'inherit',
});
if (result.error) throw result.error;
process.exitCode = result.status ?? 1;
