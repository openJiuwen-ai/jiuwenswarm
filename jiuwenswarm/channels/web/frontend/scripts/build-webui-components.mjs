import { build } from 'esbuild';
import { fileURLToPath } from 'node:url';

await build({
  absWorkingDir: fileURLToPath(new URL('..', import.meta.url)),
  entryPoints: ['tests/fixtures/webuiComponents.ts'],
  outfile: 'node_modules/.cache/webui-components/components.mjs',
  bundle: true, packages: 'external', platform: 'node', format: 'esm',
  define: { 'import.meta.env': '{}' },
  loader: { '.css': 'empty', '.svg': 'dataurl' },
  plugins: [{
    name: 'webui-test-svg',
    setup(builder) {
      // SVG artwork is decorative; preserve React component semantics in DOM tests.
      builder.onResolve({ filter: /\.svg\?react$/ }, ({ path }) => ({ path, namespace: 'svg-stub' }));
      builder.onLoad({ filter: /.*/, namespace: 'svg-stub' }, () => ({
        contents: "export default 'svg';",
        loader: 'js',
      }));
    },
  }],
});
