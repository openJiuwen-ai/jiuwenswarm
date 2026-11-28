import { build } from 'esbuild';
await build({
 entryPoints: { sidebar: 'tests/fixtures/teamSidebarExports.ts' },
 bundle: true, packages: 'external', platform: 'node', format: 'esm', jsx: 'automatic',
 loader: { '.svg': 'dataurl', '.png': 'dataurl', '.css': 'empty' },
 define: { 'import.meta.env': '{}' }, outdir: 'node_modules/.cache/team-sidebar',
 outExtension: { '.js': '.mjs' },
 plugins: [{ name: 'svg-react-test', setup(build) {
   build.onResolve({ filter: /\.css$/ }, args => ({ path: args.path, namespace: 'css-test' }));
   build.onLoad({ filter: /.*/, namespace: 'css-test' }, () => ({ contents: '', loader: 'js' }));
   build.onResolve({ filter: /\.svg\?react$/ }, args => ({ path: args.path, namespace: 'svg-test' }));
   build.onLoad({ filter: /.*/, namespace: 'svg-test' }, () => ({ contents: "import React from 'react'; export default function Icon(props) { return React.createElement('svg',props); }", loader: 'js', resolveDir: process.cwd() }));
 } }],
});
