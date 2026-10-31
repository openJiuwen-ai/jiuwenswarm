import * as esbuild from 'esbuild';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

const svgReact = {
  name: 'svg-react',
  setup(build) {
    build.onResolve({ filter: /\.svg\?react$/ }, (args) => ({
      path: path.resolve(args.resolveDir, args.path.replace(/\?react$/, '')),
      namespace: 'svg-react',
    }));
    build.onLoad({ filter: /\.svg$/, namespace: 'svg-react' }, () => ({
      contents: 'export default function Svg() { return null; }',
      loader: 'js',
    }));
  },
};

await esbuild.build({
  absWorkingDir: root,
  entryPoints: [
    'src/features/jiuwenmate/pages/ColleagueProfilePage.tsx',
    'src/features/jiuwenmate/schedules/ScheduleBoard.tsx',
    'src/features/jiuwenmate/components/ColleagueDeleteDialog.tsx',
    'src/features/jiuwenmate/pages/ColleagueWorkspacePage.tsx',
  ],
  bundle: true,
  splitting: true,
  packages: 'external',
  platform: 'node',
  format: 'esm',
  outdir: 'node_modules/.cache/jiuwenmate-profile',
  loader: { '.css': 'empty', '.svg': 'dataurl', '.png': 'dataurl', '.webp': 'dataurl' },
  plugins: [svgReact],
  jsx: 'automatic',
  define: { 'import.meta.env.DEV': 'false', 'import.meta.glob': '__testAssetGlob' },
  banner: { js: 'var __testAssetGlob = () => ({});' },
});
