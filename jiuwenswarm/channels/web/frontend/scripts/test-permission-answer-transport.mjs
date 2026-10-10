import { build } from 'esbuild';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('..', import.meta.url));
await build({
  absWorkingDir: root,
  entryPoints: [
    'src/hooks/useWebSocket.ts', 'src/stores/index.ts', 'src/i18n/index.ts',
    'src/components/InteractionSlot/AuthorizationPrompt.tsx', 'src/services/webClient.ts',
    'src/features/planMode/planModeGate.ts',
  ],
  outbase: 'src', outdir: 'node_modules/.cache/permission-answer-transport',
  bundle: true, splitting: true, packages: 'external', platform: 'node', format: 'esm',
  loader: { '.css': 'empty', '.svg': 'dataurl', '.png': 'dataurl' },
  define: { 'import.meta.env': '{"DEV":false}', 'import.meta.glob': '__permissionTestGlob' },
  banner: {
    // Vite-only decorative assets and optional extension discovery are unrelated to transport.
    js: 'const __permissionTestGlob = (pattern) => { if (!["./*.png", "./*.svg", "../../../../../extensions/*/frontend/index.tsx"].includes(pattern)) throw new Error("Unexpected Vite glob: " + pattern); return {}; };',
  },
  plugins: [{
    name: 'permission-test-svg',
    setup(builder) {
      builder.onResolve({ filter: /\.svg\?react$/ }, ({ path }) => ({ path, namespace: 'svg-stub' }));
      builder.onLoad({ filter: /.*/, namespace: 'svg-stub' }, () => ({
        contents: 'export default function SvgStub() { return null; }', loader: 'js',
      }));
    },
  }],
});
const result = spawnSync(process.execPath, ['--test', 'tests/permissionAnswerTransport.test.mjs'], {
  cwd: root, stdio: 'inherit',
});
if (result.error) throw result.error;
process.exitCode = result.status ?? 1;
