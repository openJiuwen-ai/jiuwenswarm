import { buildSync } from 'esbuild';
buildSync({
 entryPoints: { shared: 'src/components/teamArea/shared.tsx', teamEventUtils: 'src/components/ChatPanel/teamEventUtils.ts' },
 bundle: true, packages: 'external', platform: 'node', format: 'esm', jsx: 'automatic',
 loader: { '.svg': 'dataurl', '.png': 'dataurl', '.css': 'empty' },
 define: { 'import.meta.env': '{}' }, outdir: 'node_modules/.cache/member-process',
 outExtension: { '.js': '.mjs' },
});
