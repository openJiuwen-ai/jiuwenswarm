import { build } from 'esbuild';

const assetStubPlugin = {
  name: 'topic-starter-model-handoff-assets',
  setup(builder) {
    builder.onResolve({ filter: /\.svg\?react$/ }, ({ path }) => ({
      path,
      namespace: 'svg-react-stub',
    }));
    builder.onLoad({ filter: /.*/, namespace: 'svg-react-stub' }, () => ({
      contents: 'export default function SvgStub() { return null; }',
      loader: 'js',
    }));
    // A package stylesheet (the app shell imports some) is left out of the Node bundle.
    builder.onResolve({ filter: /^[^./].*\.css$/ }, ({ path }) => ({
      path,
      namespace: 'package-css-stub',
    }));
    builder.onLoad({ filter: /.*/, namespace: 'package-css-stub' }, () => ({
      contents: '',
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
  entryPoints: ['src/App.tsx', 'src/components/ChatPanel/index.tsx', 'src/stores/index.ts', 'src/services/webClient.ts', 'src/hooks/usePromptSuggestionBox.ts'],
  bundle: true,
  splitting: true,
  packages: 'external',
  platform: 'node',
  format: 'esm',
  outdir: 'node_modules/.cache/topic-starter-model-handoff',
  loader: {
    '.svg': 'dataurl',
    '.png': 'dataurl',
    '.webp': 'dataurl',
    '.css': 'text',
  },
  define: {
    'import.meta.env.DEV': 'false',
    'import.meta.env.MODE': '"test"',
    'import.meta.env.VITE_WS_BASE': '""',
    'import.meta.env.VITE_API_BASE': '""',
    'import.meta.env.VITE_PLATFORM': '""',
    'import.meta.glob': 'globalThis.__viteGlob',
  },
  plugins: [assetStubPlugin],
});
