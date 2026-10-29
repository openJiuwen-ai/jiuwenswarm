import * as esbuild from 'esbuild';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
await esbuild.build({
  absWorkingDir: root,
  entryPoints: [
    './src/features/jiuwenmate/groupSharingApi.ts',
    './src/features/jiuwenmate/useGroupSharing.ts',
    './src/features/jiuwenmate/components/GroupSharingInbox.tsx',
    './src/features/jiuwenmate/jiuwenmateApi.ts',
    './src/features/jiuwenmate/pages/ColleagueWorkspacePage.tsx',
  ],
  bundle: true,
  splitting: true,
  packages: 'external',
  platform: 'node',
  format: 'esm',
  outdir: 'node_modules/.cache/group-sharing',
  loader: { '.css': 'empty', '.svg': 'dataurl', '.png': 'dataurl', '.webp': 'dataurl' },
  plugins: [{
    name: 'svg-react',
    setup(build) {
      build.onResolve({ filter: /\.svg\?react$/ }, (args) => ({
        path: path.resolve(args.resolveDir, args.path.replace(/\?react$/, '')),
        namespace: 'svg-react',
      }));
      build.onLoad({ filter: /.*/, namespace: 'svg-react' }, () => ({
        contents: 'export default function Svg() { return null; }', loader: 'js',
      }));
    },
  }],
  jsx: 'automatic',
  define: { 'import.meta.env': '{}' },
});
