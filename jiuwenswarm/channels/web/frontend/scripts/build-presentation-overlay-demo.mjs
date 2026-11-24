import { build } from 'esbuild';

await build({
  entryPoints: ['tests/fixtures/presentationOverlayDemo.tsx'], bundle: true, format: 'esm', platform: 'browser',
  outfile: process.argv[2] || 'node_modules/.cache/presentation-overlay-demo/demo.js',
  define: { 'import.meta.env': '{}', 'process.env.NODE_ENV': '"production"' },
  loader: { '.svg': 'dataurl', '.png': 'dataurl', '.woff2': 'file' },
});
