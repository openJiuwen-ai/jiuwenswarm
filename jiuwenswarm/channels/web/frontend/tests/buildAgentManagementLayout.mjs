import { build } from 'esbuild';

const assetStubPlugin = {
  name: 'agent-management-test-assets',
  setup(builder) {
    builder.onResolve({ filter: /\.svg\?react$/ }, ({ path }) => ({
      path,
      namespace: 'svg-react-stub',
    }));
    builder.onLoad({ filter: /.*/, namespace: 'svg-react-stub' }, () => ({
      contents: 'export default function SvgStub() { return null; }',
      loader: 'js',
    }));
    builder.onResolve({ filter: /^\/logo\.svg$/ }, () => ({
      path: 'logo.svg',
      namespace: 'asset-url-stub',
    }));
    builder.onLoad({ filter: /.*/, namespace: 'asset-url-stub' }, () => ({
      contents: 'export default "logo.svg";',
      loader: 'js',
    }));
  },
};

await build({
  entryPoints: ['src/components/AgentManagementPanel/index.tsx'],
  bundle: true,
  packages: 'external',
  platform: 'node',
  format: 'esm',
  outfile: 'node_modules/.cache/agent-management-layout/AgentManagementPanel.mjs',
  loader: {
    '.css': 'empty',
    '.png': 'dataurl',
    '.svg': 'dataurl',
  },
  define: {
    'import.meta.env.DEV': 'false',
  },
  plugins: [assetStubPlugin],
});
