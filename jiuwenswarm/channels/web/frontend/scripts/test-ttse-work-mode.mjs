import { build } from 'esbuild';
import { spawnSync } from 'node:child_process';
import { resolve } from 'node:path';

await build({
  entryPoints: [
    'src/components/ChatPanel/ExtensionPickerPanel.tsx',
    'src/utils/enabledExtensions.ts',
    'src/stores/chatStore.ts',
    'src/stores/sessionStore.ts',
    'src/stores/workspaceStore.ts',
    'src/stores/pluginPackageStore.ts',
    'src/stores/connectorStore.ts',
    'src/components/ui/Toast/toastStore.ts',
    'src/i18n/index.ts',
  ],
  outbase: 'src',
  outdir: 'node_modules/.cache/ttse-work-mode',
  outExtension: { '.js': '.mjs' },
  bundle: true,
  splitting: true,
  packages: 'external',
  platform: 'node',
  format: 'esm',
  jsx: 'automatic',
  loader: { '.css': 'empty', '.svg': 'dataurl', '.png': 'dataurl' },
  define: { 'import.meta.env': '{}' },
  plugins: [{
    name: 'svg-react-test-assets',
    setup(builder) {
      builder.onResolve({ filter: /^react$/, namespace: 'test-svg' }, () => ({ path: 'react', external: true }));
      builder.onResolve({ filter: /^\/logo\.svg$/ }, () => ({ path: resolve('public/logo.svg') }));
      builder.onResolve({ filter: /\.svg\?react$/ }, args => ({ path: args.path, namespace: 'test-svg' }));
      builder.onLoad({ filter: /.*/, namespace: 'test-svg' }, () => ({
        contents: 'import { createElement } from "react"; export default (props) => createElement("svg", props);',
        loader: 'js',
      }));
    },
  }],
});

const result = spawnSync(process.execPath, ['--test', 'tests/ttseWorkMode.test.mjs'], { stdio: 'inherit' });
process.exitCode = result.status ?? 1;
